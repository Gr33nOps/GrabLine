# Security Policy

## Reporting a vulnerability

If you find something that lets a malicious server, web page, archive, or
browser message act outside GrabLine's trust boundaries: for example writing
outside the download folder, running shell commands from remote input, or
exfiltrating secrets: please report it **privately** through GitHub Security
Advisories:

[Open a private vulnerability report](https://github.com/GrabLine/GrabLine/security/advisories/new)
while signed in to GitHub (Security → Report a vulnerability).

Include affected version/OS, a minimal reproduction, expected security boundary,
impact and sanitized evidence. Use synthetic inputs; do not submit credentials,
personal downloads or unrelated user data. There is no guaranteed response SLA.

We credit reporters who request attribution when we publish a fix or advisory.

Do not open a public issue for exploitable findings until a fix has shipped.

## Scope

GrabLine is a single-user desktop download manager. Content checks (virus
scan, Safe Browsing, HTTP warnings) are advisory by design. Integrity checks
(archive path traversal, native-messaging allow-lists, subprocess argv-only
spawns, TLS verification) are enforced.

The optional **phone-remote listener** (`app/remote/`) is in scope. It is the
only network listener in GrabLine. It is off until you pair a phone, listens
only on private and link-local addresses, and lets a paired phone add
downloads and control the queue. Report anything that lets an unpaired or
revoked client reach it, bypasses the certificate pin, pairing code limits or
device-key login, makes it listen on a public address or outside `app/remote/`,
names a path or folder the PC did not issue, opens a dialog on the PC, or lets a
web page connect. Use the same private process above, and include the protocol
step (for example `pair` or `auth`) and the PC's OS. Never post pairing codes,
keys or certificates from a real setup.

See [docs/security-model.md](docs/security-model.md) for the full map of
trust boundaries, the listener's rules and past hardening work.

## Supported versions

The [latest published version](https://github.com/GrabLine/GrabLine/releases/latest)
is the supported release. Older versions are not patched separately; the owner
currently retains only the latest release/tag. Include the exact commit if the
finding affects unreleased source. Historical release notes remain in
[docs/releases](docs/releases/README.md).
