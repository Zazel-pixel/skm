# Security policy

`skm` is a personal project maintained on a best-effort basis. It has not been independently audited. Read [What it does not protect](README.md#what-it-does-not-protect) before relying on it.

## Reporting a vulnerability

Please do not open a public issue. Use **Report a vulnerability** in this repository's **Security** tab, which starts a private discussion on GitHub.

Include the `skm --version` output, your distribution, and the steps that reproduce the problem. Use fake secrets. Never send a real key, a passphrase or your audit log.

There is no response-time guarantee and no bug bounty. Only the latest version receives fixes.

## In scope

- Getting a secret or the passphrase without approval, beyond the limits the README states
- Making the approval window misrepresent what will run
- Secret values reaching stdout, stderr, the audit log or disk
- Leaving the vault directory through a profile or variable name
- A time window that outlives its limit, or serves a profile outside its scope

## Out of scope

- Limits the README already states, such as same-user code replacing the script
- Vulnerabilities in `age`, `zenity` or the operating system, which belong to those projects

## If your passphrase or a key leaks

1. Run `skm lock`.
2. Run `skm passwd`.
3. Delete old backups of `~/.secretvault`. They still open with the old passphrase.
4. Revoke the affected keys at each provider and store new ones with `skm add`, if a copy of the vault may have been taken or a key itself leaked.
