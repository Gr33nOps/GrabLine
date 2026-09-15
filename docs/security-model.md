# GrabLine security model

GrabLine takes hostile input by design: arbitrary URLs, whatever a server
sends back, stranger-chosen filenames, stranger-built archives, and messages
from web pages through the browser extension.

This document maps where untrusted data crosses into the app, what stops it
doing harm, and what is *not* defended because it is outside the threat model.
For how to report a finding, see [SECURITY.md](../SECURITY.md).

## The one principle

**Content checks are advisory; app-integrity checks are enforced.**

- *Advisory*: is this file malware? is this URL flagged? is it plain HTTP?
  These warn and never block. A flagged file is still your file. This is a
  download manager, not an antivirus, and it does not decide what you may keep.
- *Enforced*: can this archive member escape the download folder? can this
  message queue a `file://` URL? can this manifest make FFmpeg read your disk?
  These are not opinions about content; they are the app refusing to be turned
  against you, and they always trigger.

The threat is a malicious **server or web page**, never the user. GrabLine does
not defend the user from their own machine: a command you typed into the
completion-script box runs, a URL you pasted resolves. It defends the app from
what comes back over the wire.

## Trust boundaries

| # | Boundary | What crosses | Attacker's goal | What stops it |
|---|---|---|---|---|
| B1 | Remote server → downloader | body, `Content-Disposition` filename, redirects, sizes, content-type | arbitrary file write, crash | `sanitize_filename` on every derived name; sizes are advisory; `Content-Range` validated on every 206; credentials re-scoped per redirect hop (B7) |
| B2 | Web page → extension → native host → handoffs table → app | URLs, page titles, cookies/referer, quality | queue hostile URL, header injection, UI/filename poisoning, flooding | scheme allow-list, CRLF stripping, length caps, 1 MB message cap, JSON-object enforcement |
| B3 | Pasted URL → resolver → engines | the URL itself | drive an engine at a bad target (SSRF, local read) | scheme routing; FFmpeg protocol allow-list; TLS verified by default |
| B4 | Downloaded archive → extractor | member paths, symlinks, declared sizes | write outside the folder, fill the disk | `_is_within` guard (zip/tar/external), tar `data` filter, decompression-bomb cap |
| B5 | Subprocess → FFmpeg / 7-Zip / script / power | the command line | shell injection, argument injection | argument lists only, never a shell string; paths passed as discrete args |
| B6 | Secrets at rest → DB, exports, logs, keychain | proxy creds, cookies, API keys | credential theft | keychain for cloud logins; API keys and cookies stripped from exports; DB 0600 on POSIX; no secrets logged |
| B7 | Network → redirects, TLS, decompression | redirect targets, certificates, gzip | MITM, SSRF, decompression bomb | TLS verification on by default, off only on an explicit user opt-in, never silently |

## What is enforced (and where)

- **Filenames**: `app/core/naming.py:sanitize_filename` replaces path
  separators, `:` (NTFS ADS), control characters and null bytes, refuses
  Windows reserved names, and strips trailing dots/spaces. Every write path :
  direct downloads, HLS output, yt-dlp `outtmpl`, torrent layouts, archive
  destinations: routes a remote-derived name through it or through
  `filename_from_url`, which does the same.
- **Archive extraction**: `app/core/archive.py` refuses any member whose
  resolved path leaves the destination (`_is_within`), for zip, tar **and** the
  external-tool formats; tar additionally uses Python 3.12's `data` filter,
  which strips setuid bits, device nodes, and symlinks pointing outside the
  tree. Declared and streamed output is capped for the formats extracted
  in-process, so a small archive can't
  expand to fill the disk.
- **Native messaging**: `app/native_host/` validates every field before it
  reaches the handoffs table: URLs must be `http(s)` (or a `magnet:` carrying
  `xt=`), header values have CR/LF stripped, everything is length-capped, the
  wire message is capped at 1 MB, and a non-object message is rejected.
- **Subprocesses**: every spawn in the tree is an argument list (the `S1`
  convention), never a shell string. The user completion script receives the
  finished file's path as one discrete final argument, so a filename like
  `; rm -rf ~` is one literal string, never a shell token.
- **FFmpeg**: the HLS/DASH engine restricts input protocols to
  `http,https,tcp,tls,crypto,data`, so a remote manifest cannot reference
  `file://` (local read), `concat:`/`subfile:` or `gopher:`: the protocols
  behind FFmpeg's known local-file-disclosure and SSRF issues: regardless of
  how old the system FFmpeg on `PATH` happens to be. When GrabLine fetches the
  segments itself (the default path, so a gated CDN that rejects FFmpeg's own
  requests still works), FFmpeg only remuxes local temp files the app wrote: the
  manifest's remote URIs are rewritten to local filenames before FFmpeg sees
  them, and the protocol list is narrowed to `file,crypto,data`, so `file` can
  only reach the app's own temp directory, never a path the remote manifest
  chose.
- **Credential scope**: the browser handoff's `Cookie`, `Authorization` and
  `Proxy-Authorization` are the user's session, and remote documents get to
  choose URLs. An HLS playlist can name an absolute segment, key or init-map
  URL on any host it likes, so every such fetch is re-scoped through
  `net.scoped_headers`: those three headers travel only to the origin the user
  approved (same host or a subdomain, same port, never downgraded to http).
  Redirects are followed by `net.stream_scoped` rather than by httpx, so the
  decision is remade at every hop instead of the first one. Identifying
  headers (`Referer`, `User-Agent`) still travel everywhere, which is what
  keeps hotlink-protected CDNs working. FFmpeg takes one header block for a
  whole input and resolves the playlist itself, so it is given the credentials
  only once a manifest has been read and every URI in it proved on-origin.
- **Resume identity**: a partial file is only a valid head of the resource it
  was started against. Direct downloads send `If-Range` and restart when the
  validator they began with has changed or disappeared; cloud downloads record
  a per-protocol object identity (FTP `SIZE`+`MDTM`, SFTP size+mtime, S3
  ETag/VersionId/LastModified) and restart when it moves. A WebDAV resume that
  is answered with `200` (server ignored `Range`) restarts rather than
  appending a whole file onto a partial one, and a `416` only finalises when
  `Content-Range: bytes */TOTAL` actually equals the bytes on disk.
- **Automatically-followed URLs**: a URL GrabLine reaches because a *remote
  document* named it (an HLS segment, key or init map, a redirect target) is
  not the same as one the user typed. Those are refused when they point at
  link-local space - 169.254.0.0/16 and fe80::/10, which is where cloud
  instance-metadata services live. Ordinary private addresses (localhost, a
  NAS, a LAN server) stay allowed: GrabLine is a desktop download manager and
  self-hosted downloads are a normal thing to want. The user's own URL is
  never restricted.
- **SSH**: sftp/scp verify host keys. The user's own `known_hosts` is honoured,
  otherwise the key is trusted on first use and **written down**; a later
  mismatch fails the download with the fingerprints rather than being accepted
  silently.
- **TLS**: certificate verification is on for every HTTP client by default, and
  a self-signed host fails closed. It is turned off **only** where the user
  explicitly asked for it - Settings → Security ("Allow invalid/self-signed
  HTTPS certificates", off by default, shown with a warning) or the Add
  Download dialog's per-download tick. The effective policy for a job is
  `global OR per-download`, decided once in `DownloadManager.insecure_for` and
  handed to the engine that runs it. Nothing in the tree turns verification off
  as a *consequence* of anything: a certificate failure is reported and never
  retried unverified, and a per-download tick never writes to Settings. When
  the policy is on it is applied to the transport as well as the client, so a
  proxy or the IPv4-bind path cannot silently re-enable or re-disable it, and
  it is pinned to the approved host: a redirect off that host is verified
  normally, so the exemption cannot be carried somewhere the user never chose.
  Every TLS-bearing engine is covered, not just httpx: yt-dlp
  (`nocheckcertificate`), FFmpeg (`-tls_verify`, with certifi's roots passed as
  `-ca_file` so the verifying case does not depend on which ffmpeg build is
  installed), FTPS, WebDAV, S3/botocore, the `.torrent` fetch, and libtorrent's
  HTTPS trackers and web seeds. sftp/scp are deliberately untouched: SSH trust
  is host keys, not certificates. The analysis caches are keyed on the policy,
  so metadata fetched unverified can never be handed to a verified download.
- **Provisioning**: the fetched FFmpeg and Deno binaries are verified against
  hardcoded SHA-256 pins and downloaded over HTTPS from their expected hosts.

## What is advisory (and stays that way)

VirusTotal / local virus scan, Safe Browsing, the HTTP-vs-HTTPS notice, and the
checksum panel all **warn** and never block, quarantine, or delete. This is a
deliberate product decision, not an oversight.

## What is deliberately not defended

- The user against their own machine: a typed completion command, a pasted
  `file://`/`user:pass@` URL, a huge download the user chose.
- The engines are not sandboxed in containers (out of scope; would break the
  product). yt-dlp, FFmpeg, and libtorrent run with the app's own privileges.
- No app-level authentication: GrabLine is a single-user desktop app.
- Attacks that require local admin the attacker would already have.

## Reporting a vulnerability

Found something that lets a malicious server, web page, or archive act outside
these boundaries? Please report it privately via the repository
[SECURITY.md](../SECURITY.md) policy (**Security → Report a vulnerability** on
GitHub) rather than a public issue, so a fix can ship before the details are
public.

---

## Findings from the 1.23.0 security pass

Each fix ships with an attack test that fails against the old code and passes
against the new. Severity is the realistic worst case for a single-user
desktop app, not a server.

| # | CWE | Severity | Finding | Fix | Proof test |
|---|---|---|---|---|---|
| F1 | CWE-409 | Medium | A small archive could expand without bound and fill the disk (decompression bomb). | Cap declared and streamed uncompressed output; refuse absurd archives. | `test_archive_security:test_zip_bomb_refused` |
| F2 | CWE-22 | Medium | `.rar`/`.7z` extraction trusted the external tool with no in-app traversal guard (zip/tar had one). | List entries first, refuse any that escape the target: parity with zip/tar. | `test_archive_security:test_external_traversal_refused` |
| F3 | CWE-668 | Medium | The HLS engine let a remote manifest pick any FFmpeg input protocol (`file:`, `concat:`, …), a local-file-read/SSRF path on old system FFmpeg. | Restrict input protocols to what HLS needs. | `test_hls:test_ffmpeg_protocol_whitelist` |
| F4 | CWE-312 | Medium | Exporting the download list wrote extension **session cookies** (`http_headers`) to plaintext JSON. | Strip sensitive headers from the export. | `test_listio:test_export_strips_session_cookies` |
| F5 | CWE-522 | Low | Exporting settings kept a `proxy` value that may embed `user:pass`. | Redact credentials from the exported proxy URL. | `test_settings:test_export_redacts_proxy_credentials` |
| F6 | CWE-732 | Low | The data folder and SQLite DB (which hold API keys and cookies) were world-readable on POSIX. | Create the data dir `0700` and the DB `0600` on POSIX. | `test_paths:test_data_dir_is_private` |

### Audited, already safe (no change)

- **Native messaging**: scheme allow-list, CRLF header stripping, length caps,
  1 MB message cap, JSON-object enforcement. (B2)
- **Zip/tar traversal**: `_is_within` guard plus stdlib sanitization plus the
  tar `data` filter (symlinks/setuid/devices stripped). (B4)
- **Filename sanitization**: `sanitize_filename` neutralizes `../`, absolute
  paths, null bytes, `:`/ADS, backslashes, reserved names and trailing dots;
  every write routes through it. (B1)
- **Subprocesses**: argument lists throughout; the completion script appends
  the path as a discrete argument. (B5)
- **TLS**: verified by default; self-signed fails closed. Only a deliberate,
  per-user opt-in (global setting or one download's own tick) relaxes it, and a
  failure is never auto-retried unverified. (B7)
- **Provisioning**: FFmpeg and Deno fetched over HTTPS against hardcoded
  SHA-256 pins. (B3)
- **No dangerous eval**: no `eval`/`exec`/`pickle`/`yaml.load`/`marshal`/
  remote import of untrusted data. (whole tree)
- **Dependencies**: `pip-audit` reports no known vulnerabilities.
- **Log hygiene**: no credentials, cookies, keys or tokens in log calls. (B6)
- **Cloud logins**: kept in the system keychain, never in the DB. (B6)
