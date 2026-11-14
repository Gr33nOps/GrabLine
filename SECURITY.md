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

See [docs/security-model.md](docs/security-model.md) for the full map of
trust boundaries and past hardening work.

## Supported versions

The [latest published version](https://github.com/GrabLine/GrabLine/releases/latest)
is the supported release. Older versions are not patched separately; the owner
currently retains only the latest release/tag. Include the exact commit if the
finding affects unreleased source. Historical release notes remain in
[docs/releases](docs/releases/README.md).
