# skm

A local secret broker for AI coding agents, with a human approval step.

An agent can run a command that needs an API key without seeing the key. You read the command in a window, type your passphrase, and `skm` injects the secrets into that one process. Its output is scrubbed before it reaches the agent.

```console
$ skm add openai OPENAI_API_KEY          # masked input, no passphrase needed
$ skm run openai -- python my_script.py  # asks for approval, then runs
```

> **Status: personal project, not independently audited.** It runs on one Linux workstation. Read [What it does not protect](#what-it-does-not-protect) before trusting it with anything.

## Using it with an AI agent

Add this line to your `CLAUDE.md`, `AGENTS.md` or equivalent:

```markdown
For anything that needs an API key, run `skm guide` first and follow it.
```

`skm guide` prints [the agent guide](docs/agent-guide.md): how to find a profile, what to tell you before a window opens, what never to do, and what each exit code means. It ships inside the script, so it works without this repository. `skm status --json` and `skm ls --json` give agents the state of the vault and the variable names, with no passphrase and no values.

## Why

Agents read their own tool output, and that output ends up in logs and model context. Keys in `.env` files or shell variables can leak that way, or get committed by accident.

The agent brokers checked when this project started, in August 2026, did not ask a person to approve each use. That may have changed since. The approval is the point of `skm`.

## How it works

- **Storage:** each secret is a separate [`age`](https://github.com/FiloSottile/age) file. The private identity is itself encrypted with your passphrase.
- **Storing needs no passphrase:** `skm add` encrypts to the public key. A person still types the value into a masked window, and it cannot be read back.
- **Reading needs approval:** `skm run` shows the requesting process, the secret names, the command, the program it resolves to and the folder, then asks for the passphrase. The command is displayed literally, so markup or control characters in an argument cannot disguise it. The window also warns about inline code, piped input, disabled scrubbing, an unsafe `PATH`, a non-default vault, an unwritable log and commands too long to show in full.
- **Deleting needs approval:** so does `skm rm`, because it cannot be undone.
- **Output scrubbing:** secret values are replaced in stdout and stderr, including values split across two reads and their plain base64, hex and URL encodings.
- **Hijacking variables:** names such as `LD_PRELOAD`, `PYTHONPATH`, `NODE_OPTIONS` and `HTTPS_PROXY` cannot be stored, and the caller's own values are removed before the command starts, because they could make it run other code. `skm` names what it removed, except the `LESSOPEN` and `LESSCLOSE` defaults that distributions set. `--keep-env` keeps them, after approval and with a warning. `PATH`, `HOME` and similar variables are passed on, so the window shows the resolved program.
- **Its own tools:** `skm` finds `age` and `zenity` next to itself, in `~/.local/bin` or in the system directories, never through the caller's `PATH`, and gives them a minimal environment. The script runs with `python3 -I`, which ignores `PYTHONPATH`.
- **Audit log:** requests, grants and refusals are written to a local log with the full command. If `skm` cannot write there, it warns you and the time window below stops serving secrets. It does not log secret values, but a secret typed into a command line would be logged as part of that command.
- **Cleanup:** when the command exits, or `skm run` is terminated by a tool timeout for example, `skm` stops the command and anything it started in the background, so `skm run` is not a way to start a daemon. With `--no-scrub` it only stops the command itself. Core dumps are disabled.

### Optional time window

`skm agent --ttl 1h --profile openai` asks for the passphrase once, then serves `skm run` calls for that profile without a prompt until the window ends or you run `skm lock`. Other profiles still prompt. The private identity stays inside that process. The socket is `0600`, the kernel-reported caller UID is checked on each connection, and the time limit keeps counting while the machine is suspended.

**This weakens the model on purpose.** While the window is open, any process in your session can get the secrets it serves without a prompt. Limit it with `--profile`, and use `skm run --no-agent` for commands that should still need your approval. `--no-scrub` and `--keep-env` always ask for the passphrase.

## What it does not protect

`skm` reduces accidental exposure through agent context, logs and files. It does not contain a hostile process running as your user:

- **Same-user code can defeat it.** Without a sandbox, a process can replace the `skm` script or a tool in `~/.local/bin`, inject code through `LD_PRELOAD` when starting `skm` itself, or show a fake prompt to phish the passphrase. A sandboxed agent that cannot write to those directories is in a much weaker position.
- **The terminal prompt can be read by whoever owns the terminal.** Without a graphical session, `skm` asks in the terminal, with a warning. An agent can force this by removing `DISPLAY`, and if it owns that terminal it sees what you type. Answer only in a terminal you opened yourself.
- **The approved program runs as given.** The window shows the folder and the program, but it cannot show what a script in that folder does, or what a directory in `PATH` provides to the programs it starts.
- **A running command's environment is readable.** Another process with your UID can read `/proc/<pid>/environ` while the command runs. This was confirmed on a machine with Yama `ptrace_scope=1`, which does not block it.
- **An approved command gets the real secret.** It can write it to a file or send it over the network. Your only check is reading the command before you approve.
- **Scrubbing has limits.** Split, encrypted, line-wrapped or otherwise transformed secrets get through. `--no-scrub` turns it off.
- **The vault files are not tamper-proof.** Any process with your UID can edit the audit log, and could replace `recipient.txt` so that later `skm add` calls encrypt to another key. The `skm add` window shows the key in use.
- **Injected input is possible on some setups.** If `/dev/uinput` is writable, a process can simulate keystrokes in your open windows. Streaming and gamepad software often grants this. `skm doctor` reports it.
- **X11 offers less isolation.** Clients on the same X server can generally observe keystrokes. Prefer Wayland.

To contain an untrusted agent, run it in a sandbox as well: a separate user, a container, or `bwrap`.

## Quick start

`skm` needs Linux with a graphical session, Python 3 at `/usr/bin/python3` and [`age`](https://github.com/FiloSottile/age) 1.3 or later with its `age-plugin-batchpass` plugin. There are no Python dependencies. See [What has been tested](#what-has-been-tested).

**1. Install `age` and `zenity`.** Distribution packages of `age` can be too old, so take the release. This example is for x86-64 Linux; see the [`age` releases](https://github.com/FiloSottile/age/releases) for other platforms.

```console
$ sudo apt install zenity python3 curl
$ mkdir -p ~/.local/bin
$ curl -fsSLO https://github.com/FiloSottile/age/releases/download/v1.3.1/age-v1.3.1-linux-amd64.tar.gz
$ echo "bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377  age-v1.3.1-linux-amd64.tar.gz" | sha256sum -c -
$ tar -xzf age-v1.3.1-linux-amd64.tar.gz
$ install -m 755 age/age age/age-keygen age/age-plugin-batchpass ~/.local/bin/
```

**2. Install `skm`.** It is one file. Run these from a clone of this repository. If `~/.local/bin` is not in your `PATH`, add it first.

```console
$ install -m 755 skm ~/.local/bin/skm
$ skm --version
```

**3. Create the vault and check the system.** Choose a passphrase of at least 12 characters and keep it in a password manager. If you lose it, the vault cannot be recovered.

```console
$ skm init
$ skm doctor
```

`skm doctor` exits with an error when it finds a point to review. A writable `/dev/uinput` is a warning about your system, not an installation failure.

**4. Store a key and use it.** The value goes into a masked window, not your shell history.

```console
$ skm add openai OPENAI_API_KEY
$ skm run openai -- python3 -c "import os; print(len(os.environ['OPENAI_API_KEY']))"
```

The window warns about the inline code in this example. Type your passphrase to approve it, and the command prints the length of the key.

**5. Tell your agents.** See [Using it with an AI agent](#using-it-with-an-ai-agent).

## Backup and recovery

The vault is the `~/.secretvault` directory. Its files are encrypted, but the audit log inside it holds your commands in clear text. Treat a backup as sensitive.

```console
$ skm lock
$ tar -czf skm-vault-backup.tar.gz -C ~ .secretvault
```

To restore it on this or another machine, install `skm` and `age` first:

```console
$ tar -xzf skm-vault-backup.tar.gz -C ~
$ skm doctor
```

Keep these points in mind:

- **The passphrase is not in the backup.** Store it separately. A backup without it is useless, and nobody can recover it for you.
- **A backup keeps its old passphrase.** `skm passwd` changes the current vault only. If a passphrase leaks, make a new backup after changing it, then delete the old ones. An old backup opened with the leaked passphrase gives access to the current keys, because the private identity does not change. If an old copy may have been taken, revoke the keys at each provider.
- **Keys added later are not in an old backup.**

## Upgrade and uninstall

To upgrade, replace the file. Vaults stay compatible, and a running time window keeps the old code until it ends.

```console
$ skm lock
$ install -m 755 skm ~/.local/bin/skm
$ skm doctor
```

To uninstall, remove the script. The second command deletes your stored keys for good, so check that you can replace them first. `age` is a separate tool that you may want to keep.

```console
$ skm lock
$ rm ~/.local/bin/skm
$ rm -rf ~/.secretvault
```

## Configuration

Most people need none of this. Each variable is read from the caller's environment, so a process can set them too. The approval window warns about a non-default vault.

| Variable | Default | Purpose |
|---|---|---|
| `SKM_VAULT` | `~/.secretvault` | Vault directory |
| `SKM_AGENT_SOCK` | `$XDG_RUNTIME_DIR/skm-agent.sock` | Socket of the time window |

## Using it over SSH

Without a graphical session, `skm` asks in the terminal and prints a warning. Answer only in a terminal you opened yourself, because whoever owns that terminal can read what you type. If an agent runs `skm` for you over SSH, do not type your passphrase into its session: run the command yourself in your own one. `skm show` needs a graphical session and is refused otherwise.

## Commands

| Command | Purpose |
|---|---|
| `skm init` | Create the vault |
| `skm add <profile> <VAR>` | Store a secret from masked input |
| `skm ls [profile…] [--json]` | List names, never values |
| `skm rm <profile> [VAR]` | Delete a secret or a profile, after approval |
| `skm show <profile> <VAR>` | Show a value in a window, never on stdout |
| `skm run <profile…> -- <cmd>` | Run a command with secrets as environment variables |
| `skm agent [--ttl <duration>] [--profile <name>]…` | Open the time window (60 s to 8 h, default 15 min) |
| `skm lock` | Close the window |
| `skm passwd` | Change the passphrase; old copies of `identity.age` still open with the old one |
| `skm status [--json]` | Show the state of the vault and the time window |
| `skm audit [n]` | Show the access log |
| `skm doctor` | Check the installation and system hardening |
| `skm guide` | Print the guide for AI agents |

## Exit codes

| Code | Meaning |
|---|---|
| 2 | Bad usage or refused variable name |
| 3 | No vault, profile or variable of that name |
| 77 | Refused by the person, or the window timed out |
| 78 | No graphical session and no terminal to ask in |
| 79 | Wrong passphrase |
| 126 | The command cannot be executed |
| 127 | Command or required tool not found |

Otherwise `skm run` returns the command's own exit code, or 128+n if a signal killed it. Errors from `skm` itself start with `skm:` on stderr.

## Tests

```console
$ python3 -m unittest discover -s tests
```

The tests use the real `age` binary, a fake `zenity` and a temporary vault. They never touch your own vault. Real `zenity` windows are not covered by them. They need `age` in `PATH`.

## What has been tested

- **The setup and maintenance steps in this README:** followed step by step on clean Ubuntu 24.04 (Python 3.12) and Debian 12 (Python 3.11) containers, with prompts answered in the terminal.
- **Real `zenity` windows and daily use:** one Ubuntu desktop on GNOME with Wayland and Python 3.14.
- **The test suite:** the three Python versions above, and on GitHub Actions for each push.
- **Not tested:** other distributions, X11 desktops and other architectures.

## Security reports

See [SECURITY.md](SECURITY.md).

## License

MIT. `age` is a separate project with its own license.
