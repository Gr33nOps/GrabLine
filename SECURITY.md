# Security Policy

## Reporting a vulnerability

If you find something that lets a malicious server, web page, archive, or
browser message act outside GrabLine's trust boundaries: for example writing
outside the download folder, running shell commands from remote input, or
exfiltrating secrets: please report it **privately** through GitHub Security
Advisories:

**Security → Report a vulnerability** on
https://github.com/Gr33nOps/GrabLine

Do not open a public issue for exploitable findings until a fix has shipped.

## Scope

GrabLine is a single-user desktop download manager. Content checks (virus
scan, Safe Browsing, HTTP warnings) are advisory by design. Integrity checks
(archive path traversal, native-messaging allow-lists, subprocess argv-only
spawns, TLS verification) are enforced.

See [docs/security-model.md](docs/security-model.md) for the full map of
trust boundaries and past hardening work.

## Supported versions

Please report issues against the latest released tag on
https://github.com/Gr33nOps/GrabLine/releases. Older tags are not patched
separately.
