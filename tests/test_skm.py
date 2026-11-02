"""End-to-end tests: real `age`, fake `zenity`, temporary vault.

No test touches the user's vault: HOME, SKM_VAULT, the socket and the runtime
directory all point to a temporary folder.
"""

import base64
import importlib.machinery
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SKM = Path(os.environ.get("SKM_BIN", Path(__file__).resolve().parents[1] / "skm"))
PASSPHRASE = "fake-test-passphrase"  # gitleaks:allow
SECRET = "fake-value-0123456789"  # gitleaks:allow

# skm gives its tools a minimal environment, so the fake reads files, not variables.
FAKE_ZENITY = """#!/bin/sh
FAKE_DIR=@DIR@
for a in "$@"; do printf '%s\\n' "$a" >> "$FAKE_DIR/zenity-argv"; done
case " $* " in *" --text-info "*) cat > "$FAKE_DIR/zenity-stdin"; exit 0;; esac
[ -e "$FAKE_DIR/refuse" ] && exit 1
FAKE_VALUE=$(cat "$FAKE_DIR/value")
if [ -s "$FAKE_DIR/values" ]; then
  FAKE_VALUE=$(head -n 1 "$FAKE_DIR/values"); sed -i 1d "$FAKE_DIR/values"
fi
sep='|'
for a in "$@"; do case "$a" in --separator=*) sep="${a#--separator=}";; esac; done
n=0
for a in "$@"; do case "$a" in --add-password=*) n=$((n+1));; esac; done
i=0; out=""
while [ "$i" -lt "$n" ]; do
  [ "$i" -gt 0 ] && out="$out$sep"
  out="$out$FAKE_VALUE"; i=$((i+1))
done
printf '%s\\n' "$out"
"""


def load_module():
    loader = importlib.machinery.SourceFileLoader("skm_module", str(SKM))
    spec = importlib.util.spec_from_loader("skm_module", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@unittest.skipUnless(shutil.which("age"), "age not in PATH")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skm-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "zenity").write_text(FAKE_ZENITY.replace("@DIR@", str(self.tmp)))
        (bin_dir / "notify-send").write_text("#!/bin/sh\nexit 0\n")
        for f in bin_dir.iterdir():
            f.chmod(0o755)
        age_dir = Path(shutil.which("age")).resolve().parent
        # skm looks for its tools next to itself, so the copy finds the fake zenity.
        self.script = bin_dir / "skm"
        shutil.copy(SKM, self.script)
        for tool in ("age", "age-keygen", "age-plugin-batchpass"):
            (bin_dir / tool).symlink_to(age_dir / tool)
        runtime = self.tmp / "run"
        runtime.mkdir(mode=0o700)
        self.vault = self.tmp / "vault"
        self.sock = runtime / "agent.sock"
        self.env = {
            "PATH": os.pathsep.join([str(bin_dir), str(age_dir), os.environ["PATH"]]),
            "HOME": str(self.tmp),
            "DISPLAY": ":test",
            "WAYLAND_DISPLAY": "test",
            "SKM_VAULT": str(self.vault),
            "SKM_AGENT_SOCK": str(self.sock),
            "XDG_RUNTIME_DIR": str(runtime),
        }

    def skm(self, *args, FAKE_VALUE=PASSPHRASE, FAKE_REFUSE=None, **extra):
        (self.tmp / "value").write_text(FAKE_VALUE)
        refuse = self.tmp / "refuse"
        refuse.touch() if FAKE_REFUSE else refuse.unlink(missing_ok=True)
        return subprocess.run(
            [sys.executable, "-I", str(self.script), *args],
            env={**self.env, **extra},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def init_vault(self, value=SECRET):
        self.assertEqual(self.skm("init").returncode, 0)
        added = self.skm("add", "demo", "DEMO_KEY", FAKE_VALUE=value)
        self.assertEqual(added.returncode, 0, added.stderr)

    def queue(self, *values):
        (self.tmp / "values").write_text("".join(v + "\n" for v in values))

    def prompt_text(self):
        return (self.tmp / "zenity-argv").read_text()


class TestRun(Base):
    def test_injects_and_scrubs(self):
        self.init_vault()
        probe = "import os; print(len(os.environ['DEMO_KEY'])); print(os.environ['DEMO_KEY'])"
        r = self.skm("run", "demo", "--", sys.executable, "-c", probe)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(str(len(SECRET)), r.stdout)
        self.assertNotIn(SECRET, r.stdout)
        self.assertIn("[SKM:REDACTED]", r.stdout)

    def test_scrubs_encoded_forms(self):
        self.init_vault()
        probe = (
            "import base64, os, urllib.parse as u; v = os.environ['DEMO_KEY']; "
            "print(base64.b64encode(v.encode()).decode()); print(v.encode().hex()); "
            "print(u.quote(v, safe=''))"
        )
        r = self.skm("run", "demo", "--", sys.executable, "-c", probe)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(base64.b64encode(SECRET.encode()).decode(), r.stdout)
        self.assertNotIn(SECRET.encode().hex(), r.stdout)
        self.assertEqual(r.stdout.count("[SKM:REDACTED]"), 3)

    def test_returns_the_commands_exit_code(self):
        self.init_vault()
        r = self.skm("run", "demo", "--", sys.executable, "-c", "raise SystemExit(42)")
        self.assertEqual(r.returncode, 42)

    def test_wrong_passphrase_runs_nothing(self):
        self.init_vault()
        witness = self.tmp / "ran"
        r = self.skm("run", "demo", "--", "touch", str(witness), FAKE_VALUE="wrong-phrase")
        self.assertEqual(r.returncode, 79)
        self.assertFalse(witness.exists())

    def test_refusal_runs_nothing(self):
        self.init_vault()
        witness = self.tmp / "ran"
        r = self.skm("run", "demo", "--", "touch", str(witness), FAKE_REFUSE="1")
        self.assertEqual(r.returncode, 77)
        self.assertFalse(witness.exists())

    def test_no_ui_and_no_terminal(self):
        self.init_vault()
        witness = self.tmp / "ran"
        env = {k: v for k, v in self.env.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        r = subprocess.run(
            [sys.executable, "-I", str(self.script), "run", "demo", "--", "touch", str(witness)],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(r.returncode, 78)
        self.assertFalse(witness.exists())

    def test_usage_errors_are_distinct(self):
        self.init_vault()
        self.assertEqual(self.skm("run", "demo", "true").returncode, 2)
        self.assertEqual(self.skm("run", "--bad", "demo", "--", "true").returncode, 2)
        self.assertEqual(self.skm("run", "nope", "--", "true").returncode, 3)

    def test_value_with_pipe_stays_whole(self):
        value = "before|after-fake"
        self.init_vault(value)
        probe = "import os; print(len(os.environ['DEMO_KEY']))"
        r = self.skm("run", "demo", "--", sys.executable, "-c", probe)
        self.assertEqual(r.stdout.strip(), str(len(value)))

    def test_shown_command_has_no_newline(self):
        self.init_vault()
        self.skm("run", "demo", "--", "true", "a\nSecrets: none")
        self.assertNotIn("\nSecrets: none", self.prompt_text())
        self.assertIn("\\x0a", self.prompt_text())

    def test_shown_command_has_no_markup(self):
        self.init_vault()
        self.skm("run", "demo", "--", "true", "<span alpha='1'>hidden</span>", "a&b", "x\\ny")
        argv = self.prompt_text()
        self.assertNotIn("<span", argv)
        self.assertIn("&lt;span", argv)
        self.assertIn("a&amp;b", argv)
        self.assertIn("x\\\\ny", argv)

    def test_prompt_warns_about_inline_code_and_other_vault(self):
        self.init_vault()
        self.skm("run", "demo", "--", "bash", "-c", "true")
        self.assertIn("Runs inline code", self.prompt_text())
        self.assertIn("Not the default vault", self.prompt_text())

    def test_sigterm_stops_the_child(self):
        self.init_vault()
        pid_file = self.tmp / "child-pid"
        code = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
        (self.tmp / "value").write_text(PASSPHRASE)
        proc = subprocess.Popen(
            [sys.executable, "-I", str(self.script), "run", "demo", "--", sys.executable, "-c", code],
            env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text():
                break
            time.sleep(0.1)
        child = int(pid_file.read_text())
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=8)
        self.assertFalse(Path(f"/proc/{child}").exists())


class TestCallerEnvironment(Base):
    def test_fake_age_in_path_gets_no_passphrase(self):
        self.init_vault()
        evil = self.tmp / "evil"
        evil.mkdir()
        witness = self.tmp / "stolen"
        fake = evil / "age"
        fake.write_text(f"#!/bin/sh\ntouch {witness}\nexit 1\n")
        fake.chmod(0o755)
        path = f"{evil}:{self.env['PATH']}"
        r = self.skm("run", "demo", "--", sys.executable, "-c", "pass", PATH=path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(witness.exists())

    def test_tools_get_a_minimal_environment(self):
        self.init_vault()
        probe = self.tmp / "bin/zenity"
        probe.write_text(f'#!/bin/sh\nenv > {self.tmp}/tool-env\n' + probe.read_text())
        self.skm("run", "demo", "--", "true", LD_PRELOAD="", GTK_MODULES="evil")
        seen = (self.tmp / "tool-env").read_text()
        self.assertNotIn("GTK_MODULES", seen)
        self.assertNotIn("LD_PRELOAD", seen)

    def test_pythonpath_does_not_reach_the_command(self):
        self.init_vault()
        evil = self.tmp / "evil"
        evil.mkdir()
        witness = self.tmp / "hijacked"
        (evil / "sitecustomize.py").write_text(f"open({str(witness)!r}, 'w')\n")
        r = self.skm("run", "demo", "--", sys.executable, "-c", "pass", PYTHONPATH=str(evil))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(witness.exists())
        self.assertIn("not passed to the command: PYTHONPATH", r.stderr)

    def test_less_defaults_are_removed_quietly(self):
        self.init_vault()
        probe = "import os; print('LESSOPEN' in os.environ, 'DOCKER_HOST' in os.environ)"
        r = self.skm("run", "demo", "--", sys.executable, "-c", probe,
                     LESSOPEN="| lesspipe %s", LESSCLOSE="lesspipe %s %s", DOCKER_HOST="unix:///x")
        self.assertEqual(r.stdout.strip(), "False False")
        self.assertIn("not passed to the command: DOCKER_HOST (", r.stderr)
        self.assertNotIn("LESS", r.stderr)

    def test_less_defaults_alone_print_no_note(self):
        self.init_vault()
        r = self.skm("run", "demo", "--", "true", LESSOPEN="| lesspipe %s")
        self.assertNotIn("not passed", r.stderr)

    def test_keep_env_still_names_less_defaults_in_the_prompt(self):
        self.init_vault()
        self.skm("run", "--keep-env", "demo", "--", "true", LESSOPEN="| lesspipe %s")
        self.assertIn("Keeps variables that can change what runs: LESSOPEN", self.prompt_text())

    def test_keep_env_is_explicit_and_shown(self):
        self.init_vault()
        probe = "import os; print(os.environ['PYTHONPATH'])"
        r = self.skm("run", "--keep-env", "demo", "--", sys.executable, "-c", probe, PYTHONPATH="/kept")
        self.assertIn("/kept", r.stdout)
        self.assertIn("Keeps variables that can change what runs: PYTHONPATH", self.prompt_text())

    def test_prompt_shows_program_and_folder(self):
        self.init_vault()
        self.skm("run", "demo", "--", "true")
        self.assertIn("Program: " + shutil.which("true"), self.prompt_text())
        self.assertIn("Folder: " + os.getcwd(), self.prompt_text())

    def test_unknown_program_stops_before_the_prompt(self):
        self.init_vault()
        (self.tmp / "zenity-argv").unlink()
        self.assertEqual(self.skm("run", "demo", "--", "no-such-program-x").returncode, 127)
        self.assertFalse((self.tmp / "zenity-argv").exists())

    def test_inline_code_is_detected(self):
        inline = load_module().runs_inline_code
        for cmd in (["python3", "-cprint(1)"], ["python3", "-"], ["python3"], ["node", "-p", "1"],
                    ["perl", "-E", "1"], ["php", "-r", "1"], ["env", "-S", "x"], ["bash", "x.sh"]):
            self.assertTrue(inline(cmd), cmd)
        for cmd in (["python3", "report.py"], ["gcc", "-c", "a.c"], ["grep", "-e", "x", "f"]):
            self.assertFalse(inline(cmd), cmd)

    def test_piped_input_is_flagged(self):
        self.init_vault()
        (self.tmp / "value").write_text(PASSPHRASE)
        subprocess.run(
            [sys.executable, "-I", str(self.script), "run", "demo", "--", "cat"],
            env=self.env, input=b"data", capture_output=True, timeout=30,
        )
        self.assertIn("Reads piped input", self.prompt_text())


class TestChildLifetime(Base):
    def setUp(self):
        super().setUp()
        # Unique per test: a sleep left over from an earlier run must not be counted.
        self.seconds = str(100000 + (time.time_ns() // 1000) % 800000)
        self.marker = f"^sleep {self.seconds}$"

    def alive(self, _=None):
        return subprocess.run(["pgrep", "-f", self.marker], capture_output=True).returncode == 0

    def test_grandchild_stops_with_skm(self):
        self.init_vault()
        (self.tmp / "value").write_text(PASSPHRASE)
        proc = subprocess.Popen(
            [sys.executable, "-I", str(self.script), "run", "demo", "--", "sh", "-c", f"sleep {self.seconds}; echo x"],
            env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        for _ in range(50):
            if self.alive():
                break
            time.sleep(0.1)
        self.assertTrue(self.alive())
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=8)
        time.sleep(0.3)
        self.assertFalse(self.alive())

    def test_background_child_does_not_hang_skm(self):
        self.init_vault()
        r = self.skm("run", "demo", "--", "sh", "-c", f"sleep {self.seconds} & echo done")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("done", r.stdout)
        time.sleep(0.3)
        self.assertFalse(self.alive())

    def test_no_scrub_child_stops_with_skm(self):
        self.init_vault()
        (self.tmp / "value").write_text(PASSPHRASE)
        proc = subprocess.Popen(
            [sys.executable, "-I", str(self.script), "run", "--no-scrub", "demo", "--", "sleep", self.seconds],
            env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        for _ in range(50):
            if self.alive():
                break
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=8)
        time.sleep(0.3)
        self.assertFalse(self.alive())

    def test_signal_exit_uses_the_shell_convention(self):
        self.init_vault()
        r = self.skm("run", "demo", "--", "sh", "-c", "kill -TERM $$")
        self.assertEqual(r.returncode, 143)


class TestReservedNames(Base):
    def test_add_refuses_hijacking_names(self):
        self.init_vault()
        for name in ("LD_PRELOAD", "PATH", "PYTHONPATH", "NODE_OPTIONS", "ld_preload",
                     "GIT_SSH_COMMAND", "HTTPS_PROXY", "SKM_VAULT"):
            r = self.skm("add", "demo", name, FAKE_VALUE=SECRET)
            self.assertEqual(r.returncode, 2, name)
            self.assertFalse((self.vault / f"profiles/demo/{name}.age").exists())

    def test_bash_function_name_is_refused(self):
        self.init_vault()
        src = self.vault / "profiles/demo/DEMO_KEY.age"
        shutil.copy(src, self.vault / "profiles/demo/BASH_FUNC_id%%.age")
        witness = self.tmp / "ran"
        r = self.skm("run", "demo", "--", "touch", str(witness))
        self.assertEqual(r.returncode, 2)
        self.assertFalse(witness.exists())

    def test_run_refuses_a_planted_name(self):
        self.init_vault()
        src = self.vault / "profiles/demo/DEMO_KEY.age"
        shutil.copy(src, self.vault / "profiles/demo/LD_PRELOAD.age")
        witness = self.tmp / "ran"
        r = self.skm("run", "demo", "--", "touch", str(witness))
        self.assertEqual(r.returncode, 2)
        self.assertFalse(witness.exists())


class TestVault(Base):
    def test_no_clear_value_on_disk(self):
        self.init_vault()
        self.skm("run", "demo", "--", "true")
        for f in self.vault.rglob("*"):
            if f.is_file():
                self.assertNotIn(SECRET.encode(), f.read_bytes(), f.name)
                self.assertNotIn(b"AGE-SECRET-KEY", f.read_bytes(), f.name)

    def test_file_permissions(self):
        self.init_vault()
        mode = lambda p: p.stat().st_mode & 0o777
        self.assertEqual(mode(self.vault), 0o700)
        self.assertEqual(mode(self.vault / "identity.age"), 0o400)
        self.assertEqual(mode(self.vault / "profiles/demo/DEMO_KEY.age"), 0o600)
        self.assertEqual(mode(self.vault / "audit.log"), 0o600)

    def test_child_keeps_the_callers_umask(self):
        self.init_vault()
        target = self.tmp / "made-by-child"
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.skm("run", "demo", "--", "touch", str(target))
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_ls_shows_only_names(self):
        self.init_vault()
        r = self.skm("ls")
        self.assertIn("DEMO_KEY", r.stdout)
        self.assertNotIn(SECRET, r.stdout)
        self.assertEqual(json.loads(self.skm("ls", "--json").stdout), {"demo": ["DEMO_KEY"]})

    def test_status_needs_no_passphrase(self):
        self.assertFalse(json.loads(self.skm("status", "--json").stdout)["initialized"])
        self.init_vault()
        (self.tmp / "zenity-argv").unlink()
        info = json.loads(self.skm("status", "--json").stdout)
        self.assertTrue(info["initialized"])
        self.assertEqual(info["variables"], 1)
        self.assertFalse(info["agent_running"])
        self.assertFalse((self.tmp / "zenity-argv").exists())

    def test_show_stays_out_of_argv_and_stdout(self):
        self.init_vault()
        (self.tmp / "zenity-argv").unlink()
        r = self.skm("show", "demo", "DEMO_KEY")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(SECRET, r.stdout + r.stderr)
        self.assertNotIn(SECRET, self.prompt_text())
        self.assertEqual((self.tmp / "zenity-stdin").read_text(), SECRET)

    def test_vault_config_does_not_choose_the_binary(self):
        self.init_vault()
        trap = self.tmp / "fake-age"
        witness = self.tmp / "trap-fired"
        trap.write_text(f"#!/bin/sh\ntouch {witness}\nexit 1\n")
        trap.chmod(0o755)
        (self.vault / "config.json").write_text(json.dumps({"tools": {"age": str(trap)}}))
        self.skm("run", "demo", "--", "true")
        self.assertFalse(witness.exists())

    def test_rm_cannot_leave_the_vault(self):
        self.init_vault()
        victim = self.tmp / "victim.age"
        victim.write_text("keep me")
        r = self.skm("rm", "demo", "../../../victim")
        self.assertEqual(r.returncode, 2)
        self.assertTrue(victim.exists())
        r = self.skm("show", "demo", "../../../victim")
        self.assertEqual(r.returncode, 2)

    def test_rm_needs_approval(self):
        self.init_vault()
        key = self.vault / "profiles/demo/DEMO_KEY.age"
        self.assertEqual(self.skm("rm", "demo", "DEMO_KEY", FAKE_REFUSE="1").returncode, 77)
        self.assertTrue(key.exists())
        self.assertEqual(self.skm("rm", "demo", "DEMO_KEY", FAKE_VALUE="wrong").returncode, 79)
        self.assertTrue(key.exists())
        self.assertEqual(self.skm("rm", "demo", "DEMO_KEY").returncode, 0)
        self.assertFalse(key.exists())

    def test_short_value_is_refused(self):
        self.init_vault()
        self.assertEqual(self.skm("add", "demo", "SHORT", FAKE_VALUE="abc").returncode, 2)
        self.assertFalse((self.vault / "profiles/demo/SHORT.age").exists())

    def test_short_passphrase_is_refused_at_init(self):
        self.assertEqual(self.skm("init", FAKE_VALUE="short").returncode, 1)
        self.assertFalse((self.vault / "identity.age").exists())

    def test_refusal_is_logged(self):
        self.init_vault()
        self.skm("run", "demo", "--", "true", FAKE_REFUSE="1")
        self.assertIn("prompt_refused", self.skm("audit").stdout)

    def test_missing_plugin_is_not_called_a_wrong_passphrase(self):
        # Direct: a real installation may have the plugin in ~/.local/bin.
        mod = load_module()
        lone_dir = self.tmp / "lone"
        lone_dir.mkdir()
        (lone_dir / "age").write_text("#!/bin/sh\nexit 1\n")
        mod.find_tool = lambda name: str(lone_dir / name)
        mod.which_tool = lambda name: None
        with self.assertRaises(SystemExit) as caught:
            mod.check_age_setup()
        self.assertEqual(caught.exception.code, 127)

    def test_damaged_identity_is_not_called_a_wrong_passphrase(self):
        self.init_vault()
        identity = self.vault / "identity.age"
        identity.chmod(0o600)
        identity.write_text("damaged\n")
        r = self.skm("run", "demo", "--", "true")
        self.assertEqual(r.returncode, 1)
        self.assertIn("damaged", r.stderr)

    def test_broken_audit_log_warns(self):
        self.init_vault()
        (self.vault / "audit.log").unlink()
        (self.vault / "audit.log").mkdir()
        r = self.skm("run", "demo", "--", "true")
        self.assertIn("cannot write the audit log", r.stderr)
        self.assertIn("will leave no trace", self.prompt_text())
        self.assertEqual(r.stderr.count("cannot write the audit log"), 1)


class TestPasswd(Base):
    NEW = "new-fake-test-passphrase"  # gitleaks:allow

    def can_open(self, passphrase):
        return self.skm("run", "demo", "--", "true", FAKE_VALUE=passphrase).returncode == 0

    def test_changes_passphrase_and_keeps_secrets(self):
        self.init_vault()
        recipient = (self.vault / "recipient.txt").read_bytes()
        secret_file = (self.vault / "profiles/demo/DEMO_KEY.age").read_bytes()
        self.queue(PASSPHRASE, self.NEW)
        r = self.skm("passwd")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.can_open(self.NEW))
        self.assertFalse(self.can_open(PASSPHRASE))
        self.assertEqual((self.vault / "recipient.txt").read_bytes(), recipient)
        self.assertEqual((self.vault / "profiles/demo/DEMO_KEY.age").read_bytes(), secret_file)
        self.assertEqual((self.vault / "identity.age").stat().st_mode & 0o777, 0o400)
        self.assertEqual(list(self.vault.glob(".identity*")), [])
        self.assertNotIn(self.NEW, r.stdout + r.stderr)
        self.assertIn(" passwd ", self.skm("audit").stdout)

    def assert_unchanged(self, *values, code):
        self.init_vault()
        before = (self.vault / "identity.age").read_bytes()
        self.queue(*values)
        r = self.skm("passwd")
        self.assertEqual(r.returncode, code, r.stderr)
        self.assertEqual((self.vault / "identity.age").read_bytes(), before)

    def test_wrong_passphrase_changes_nothing(self):
        self.assert_unchanged("wrong-phrase", self.NEW, code=79)

    def test_same_passphrase_is_refused(self):
        self.assert_unchanged(PASSPHRASE, PASSPHRASE, code=1)

    def test_too_short_is_refused(self):
        self.assert_unchanged(PASSPHRASE, "short", code=1)


class TestScrubber(unittest.TestCase):
    def setUp(self):
        self.Scrubber = load_module().Scrubber

    def test_split_across_two_reads(self):
        s = self.Scrubber(["fake-secret"])
        out = s.feed(b"before fake-") + s.feed(b"secret after") + s.flush()
        self.assertNotIn(b"fake-secret", out)
        self.assertIn(b"before", out)
        self.assertIn(b"after", out)

    def test_secret_inside_another(self):
        s = self.Scrubber(["abcd", "abcd-rest-fake"])
        out = s.feed(b"x abcd-rest-fake y") + s.flush()
        self.assertNotIn(b"rest-fake", out)

    def test_clean_output_is_untouched(self):
        s = self.Scrubber(["fake-secret"])
        self.assertEqual(s.feed(b"nothing to see\n") + s.flush(), b"nothing to see\n")


class TestAgent(Base):
    def start(self, ttl, allowed=None):
        """Start the service loop with a throwaway identity, without a passphrase."""
        kg = subprocess.run(["age-keygen"], capture_output=True, text=True, env=self.env)
        lines = kg.stdout.splitlines()
        identity = next(l for l in lines if l.startswith("AGE-SECRET-KEY"))
        recipient = next(l for l in lines if "public key:" in l).split(": ", 1)[1]
        self.vault.mkdir(mode=0o700)
        (self.vault / "recipient.txt").write_text(recipient + "\n")
        (self.vault / "identity.age").write_text("fake\n")
        self.assertEqual(self.skm("add", "demo", "DEMO_KEY", FAKE_VALUE=SECRET).returncode, 0)
        self.assertEqual(self.skm("add", "other", "OTHER_KEY", FAKE_VALUE=SECRET).returncode, 0)
        code = (
            "import importlib.machinery as m, importlib.util as u, sys\n"
            f"l = m.SourceFileLoader('s', {str(self.script)!r})\n"
            "x = u.module_from_spec(u.spec_from_loader('s', l)); l.exec_module(x)\n"
            f"x.agent_serve(sys.stdin.read().strip(), {ttl}, {allowed!r})\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code], env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        proc.stdin.write(identity.encode())
        proc.stdin.close()
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        for _ in range(100):
            if self.sock.exists():
                return proc
            time.sleep(0.05)
        self.fail("the agent socket did not appear")

    def connect(self):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(8)
        c.connect(str(self.sock))
        self.addCleanup(c.close)
        return c

    def ask(self, request):
        c = self.connect()
        c.sendall(request)
        return c.recv(65536)

    def test_private_socket_and_secret_served(self):
        self.start(30)
        self.assertEqual(self.sock.stat().st_mode & 0o777, 0o600)
        self.assertIn(SECRET.encode(), self.ask(b"GET demo\n"))

    def test_profile_scope_is_enforced(self):
        self.start(30, ["demo"])
        self.assertIn(SECRET.encode(), self.ask(b"GET demo\n"))
        self.assertEqual(self.ask(b"GET other\n"), b"ERR scope\n")
        self.assertEqual(self.ask(b"GET demo other\n"), b"ERR scope\n")
        info = json.loads(self.skm("status", "--json").stdout)
        self.assertTrue(info["agent_running"])
        self.assertEqual(info["agent_profiles"], ["demo"])

    def test_out_of_scope_run_falls_back_to_the_passphrase(self):
        self.start(30, ["demo"])
        (self.tmp / "zenity-argv").unlink(missing_ok=True)
        r = self.skm("run", "other", "--", "true", FAKE_REFUSE="1")
        self.assertEqual(r.returncode, 77)

    def test_run_served_by_agent_opens_no_window(self):
        self.start(30)
        (self.tmp / "zenity-argv").unlink(missing_ok=True)
        r = self.skm("run", "demo", "--", "true", FAKE_REFUSE="1")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_broken_audit_log_blocks_the_agent_path(self):
        self.start(30)
        (self.vault / "audit.log").unlink(missing_ok=True)
        (self.vault / "audit.log").mkdir()
        r = self.skm("run", "demo", "--", "true", FAKE_REFUSE="1")
        self.assertEqual(r.returncode, 77)

    def test_damaged_file_does_not_end_the_window(self):
        self.start(30)
        bad = self.vault / "profiles/other/OTHER_KEY.age"
        bad.write_text("damaged")
        self.assertEqual(self.ask(b"GET other\n"), b"ERR internal\n")
        self.assertIn(SECRET.encode(), self.ask(b"GET demo\n"))

    def test_lock_stops_an_agent_whose_socket_is_gone(self):
        proc = self.start(30)
        self.sock.unlink()
        r = self.skm("lock")
        self.assertIn("through its pid", r.stdout)
        self.assertEqual(proc.wait(timeout=6), 0)

    def test_busy_agent_keeps_its_socket(self):
        mod = load_module()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = self.tmp / "busy.sock"
        srv.bind(str(path))
        srv.listen(0)
        self.addCleanup(srv.close)
        mod.AGENT_SOCK = path
        keepers = []
        for _ in range(3):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.setblocking(False)
            try:
                c.connect(str(path))
            except OSError:
                pass
            keepers.append(c)
            self.addCleanup(c.close)
        self.assertIsNone(mod._agent_talk("STATUS", timeout=0.2))
        self.assertTrue(path.exists())

    def test_no_scrub_ignores_the_agent(self):
        self.start(30)
        r = self.skm("run", "--no-scrub", "demo", "--", "true", FAKE_REFUSE="1")
        self.assertEqual(r.returncode, 77)

    def test_nothing_after_expiry_on_a_connection_opened_before(self):
        self.start(1)
        c = self.connect()
        time.sleep(1.5)
        c.sendall(b"GET demo\n")
        try:
            answer = c.recv(65536)
        except OSError:
            answer = b""
        self.assertNotIn(SECRET.encode(), answer)

    def test_silent_client_does_not_hold_the_agent(self):
        proc = self.start(1)
        self.connect()
        self.assertEqual(proc.wait(timeout=6), 0)
        self.assertFalse(self.sock.exists())

    def test_foreign_file_at_socket_path_is_left_alone(self):
        self.vault.mkdir(mode=0o700)
        (self.vault / "recipient.txt").write_text("fake\n")
        (self.vault / "identity.age").write_text("fake\n")
        self.sock.write_text("not a socket")
        r = self.skm("agent", "--ttl", "1m")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.sock.read_text(), "not a socket")


class TestScript(unittest.TestCase):
    def test_interpreter_ignores_the_callers_python_settings(self):
        self.assertEqual(SKM.read_text().splitlines()[0], "#!/usr/bin/python3 -I")


class TestDocs(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_agent_guide_copy_matches_the_script(self):
        guide = load_module().AGENT_GUIDE
        self.assertEqual((self.ROOT / "docs/agent-guide.md").read_text(), guide)

    def test_durations(self):
        parse = load_module().parse_duration
        self.assertEqual(parse("90"), 90)
        self.assertEqual(parse("15m"), 900)
        self.assertEqual(parse("1h"), 3600)
        with self.assertRaises(SystemExit):
            parse("soon")


if __name__ == "__main__":
    unittest.main()
