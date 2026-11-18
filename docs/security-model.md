# GrabLine security model

Reference reviewed against 1.32.15 on 14 November 2026. The historical findings
at the end describe earlier audit results; they are not a new independent
security certification. See [Testing](testing.md) for verification scope.

GrabLine takes hostile input by design: arbitrary URLs, whatever a server
sends back, stranger-chosen filenames, stranger-built archives, and messages
from web pages through the browser extension. If you pair a phone, it also
takes messages from that phone over your local network.

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
| B8 | Local network → phone-remote listener → handoffs table, job controls | WebSocket frames from a paired phone, or from anyone on the LAN who tries | send commands, impersonate a phone or the PC, write outside the download folders, expose the PC to the internet | [Phone remote listener](#phone-remote-listener): off by default, private binds only, pinned TLS, device keys, ids instead of paths |

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
  approved (exact host, same port, never downgraded to http). A flattened
  browser Cookie cannot authorize subdomains because its original host-only
  attributes are no longer available.
  Direct downloads and resolver probes enforce scope in the HTTP client's
  final request hook, including redirects, cached final URLs, retries and
  resumes. Native HLS uses `net.stream_scoped` and the same final hook.
  Response cookies retain their normal domain-aware jar behavior, so a CDN
  can establish its own session without receiving the source's literal cookie.
  URL rewrites, fallback streams and mirror changes strip credentials before
  rebinding them to another host. Identifying
  headers (`Referer`, `User-Agent`) still travel everywhere, which is what
  keeps hotlink-protected CDNs working. FFmpeg takes one header block for a
  whole input and resolves the playlist itself, so remote FFmpeg inputs never
  receive browser credentials. Authenticated HLS is fetched natively and
  FFmpeg combines local files. Credentialed Smart sessions use the policy-aware
  Urllib transport, a scoped cookie jar, and native downloaders. Adapters that
  discard cookie policy are excluded for these sessions; cookies are withheld
  from lossy metadata and external-downloader exports. FFmpeg-only remote
  inputs that require cookies may fail safely rather than expose the session.
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
  instance-metadata services live. The hostname is resolved before the check,
  so a name pointed at a metadata address is caught too (not a complete
  DNS-rebinding defence: the address could still change between the lookup and
  the connection). Ordinary private addresses (localhost, a
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
  The certificate that host presented is also recorded, and a later *change* is
  refused with both fingerprints - trust-on-first-use, the same shape as the
  SSH host keys, because with verification off the accepted certificate is the
  only thing left vouching for the server. Settings -> Security lists what is
  remembered and clears it.
  Every TLS-bearing engine is covered, not just httpx: yt-dlp
  (`nocheckcertificate`), FFmpeg (`-tls_verify`, with certifi's roots passed as
  `-ca_file` so the verifying case does not depend on which ffmpeg build is
  installed), FTPS, WebDAV, S3/botocore, the `.torrent` fetch, and libtorrent's
  HTTPS trackers and web seeds. sftp/scp are deliberately untouched: SSH trust
  is host keys, not certificates. The analysis caches are keyed on the policy,
  so metadata fetched unverified can never be handed to a verified download.
- **Provisioning**: the fetched FFmpeg and Deno binaries are verified against
  hardcoded SHA-256 pins and downloaded over HTTPS from their expected hosts.

## Phone remote listener

GrabLine has exactly one network listener: the optional phone-remote listener
in `app/remote/`. It lets GrabLine for Android, paired with this PC, add
downloads and control the queue over the local network. The wire format is in
[protocol/PROTOCOL.md](../protocol/PROTOCOL.md) (section numbers below refer
to it). The browser extension still uses Native Messaging stdio and no port.

It is the only new way into the computer, so it has the strictest rules in
the app. Each rule has a QA test (ids from the phone project's QA table).

### Rules

| Area | Rule | QA |
|---|---|---|
| When it exists | Off by default. No socket exists until you open **Pair a phone**. | QA-11 |
| | On only while a phone is paired or a pairing code is showing. | QA-11 |
| | Off again within 1 s of the last phone being removed, and when an unused pairing code expires or is closed. | QA-11 |
| | The Settings switch "Listener on or off" closes every connection; pairings are kept. | - |
| Where it listens | One socket per local address in these ranges only: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `fc00::/7`, `fe80::/10`. | QA-11 |
| | Never `0.0.0.0` or `::`. Never a public address. | QA-11 |
| | Never UPnP or any router port mapping. | QA-11 |
| | A connection from a peer outside those ranges is closed before the TLS handshake. | QA-11 |
| | Addresses are re-read on every network change and at least every 5 s. | QA-12 |
| | A hidden test setting (`remote_loopback_test`, off by default, not in the UI) also binds `127.0.0.1` for the Android emulator. | - |
| Identity of the PC | TLS 1.2 or newer with a self-signed P-256 certificate made on first use. The phone pins the SHA-256 of its public key (SPKI) and refuses any other key. There is no "connect anyway". | QA-07 |
| | The pin travels in the pairing QR code. In manual entry, the pairing proofs bind the code to the key that was shown. | QA-07 |
| Pairing | A code is 12 characters (60 bits), single-use, valid for 2 minutes. | QA-02 |
| | The fifth wrong proof locks pairing. A new code needs a new action from you. | QA-03 |
| | The code is never sent. The phone sends an HMAC proof bound to this connection, the PC key and the phone key. The PC proves itself back. | QA-01 |
| | Every new phone needs your **Allow** on the PC. Deny stores nothing. | QA-04 |
| Identity of the phone | The phone's key is a P-256 key in Android Keystore and cannot be exported. The PC stores only the public key. | QA-06 |
| | Every connection starts with a signed transcript (nonce, PC pin, device id). A signature made for another PC never verifies here. | QA-05 |
| | Before login only `pair` and `auth` are accepted, in frames of 4 KB or less, within 10 s. Anything else gets one generic `unauthorized` and no state is read. | QA-05 |
| | Unknown device, removed device and bad signature look the same to the sender. | QA-05 |
| Revocation | Removing a phone on the PC deletes its key and closes its connections at once. Its next request fails within 1 s. | QA-06 |
| | Resetting phone connections deletes the certificate, so every phone refuses the PC until it pairs again. | QA-07 |
| Limits | Frames of 1 MB at most; a larger one closes that connection and the listener keeps running. | QA-09 |
| | Per-device rate limit (20 requests/s, burst 40; costly requests cost 5) and a flood close. The listener never runs on the UI thread. | QA-10 |
| | At most 8 paired phones, 2 connections per phone, 8 unauthenticated connections. | QA-10 |
| Input | Strict JSON schema: wrong types and bad patterns are refused, never coerced. | QA-09 |
| | Fields that name a location (`path`, `folder`, `dir`, `destDir`, `dest_dir`, `filename`, `file`, `saveAs`) are refused with `forbidden`. | QA-08, QA-40 |
| | Browser fields (`cookie`, `referer`, `userAgent`) are ignored and never stored. | - |
| What a phone can reach | Folders and queues only by ids the PC issued; the PC turns them into paths itself. | QA-08 |
| | Jobs only by job id, never by path. Files can be copied only from finished jobs. | QA-40 |
| | Nothing the PC sends back contains a filesystem path, and links lose any `user:password@`. | - |
| Your screen | A phone link never opens a dialog and never carries cookies. It uses the unattended add path with the quality you chose, or `best`. | QA-13 |
| Downloaded files | Unchanged. Phone links use the normal pipeline and the same advisory checks. Nothing is opened automatically. | - |
| Browser pages | A handshake with an `Origin` header is refused, so a web page cannot open a connection to it. | Listener tests (P1-D11) |
| Logs | Pairing codes, nonces, proofs, signatures and keys are never logged. | - |

### CI gate

`scripts/check_no_listen.py` fails the build if any code outside `app/remote/`
opens a listening socket or server. It allows listening only in `app/remote/`,
which must stay off by default.

### Threats and the rules that stop them

| Threat | Rule | QA |
|---|---|---|
| Someone on the same Wi-Fi sends commands | Off until pairing; only `pair`/`auth` before login; signed login bound to this PC; one generic `unauthorized`; pre-login limits | QA-05, QA-11 |
| A fake PC impersonates yours | SPKI pin from the QR code; custom trust check with no bypass; proofs tie pairing to the PC key, including manual entry | QA-07 |
| Pairing code guessed or reused | 60-bit code, never sent; single use; 2 minutes; lock after 5 wrong proofs; Allow prompt | QA-01 to QA-04 |
| A stolen phone keeps access | Removal closes its connections at once and deletes its key; the key is non-exportable; the PC holds no secret that can act as the phone | QA-06 |
| The phone writes anywhere on the PC | Folders and queues by PC-issued id only; jobs by id; path-bearing fields refused; no paths in replies | QA-08, QA-40 |
| Oversized or malformed messages | 1 MB cap (4 KB before login); strict schema; unknown type `unsupported`; per-device rate limit; flood close | QA-09, QA-10 |
| The listener exposed to the internet | Private and link-local binds only; peer address check; no UPnP; `Origin` refused; off after the last unpairing | QA-11 |
| Downloaded files are dangerous | Same advisory checks as the desktop: warn, never quarantine, never auto-open; `fetch` only copies bytes | - |

### Not defended

- A phone you paired and allowed. Every paired phone may use every phone
  message, the same as someone sitting at the PC's queue. There are no roles.
- Anyone who can already read your GrabLine data folder as you. The
  certificate key is readable by your account only, but that is no defence
  against your own account.
- Traffic analysis on the local network: a nearby device can see that a PC
  accepts connections on the port, and the unauthenticated `hello` shows only
  the PC's id and name.
- A router or network that forwards the port to the internet by hand. GrabLine
  never asks for that and binds no public address.

## What is advisory (and stays that way)

VirusTotal / local virus scan, Safe Browsing, the HTTP-vs-HTTPS notice, and the
checksum panel all **warn** and never block, quarantine, or delete. This is a
deliberate product decision, not an oversight.

## What is deliberately not defended

- The user against their own machine: a typed completion command, a pasted
  `file://`/`user:pass@` URL, a huge download the user chose.
- The engines are not sandboxed in containers (out of scope; would break the
  product). yt-dlp, FFmpeg, and libtorrent run with the app's own privileges.
- No app-level authentication for the desktop window: GrabLine is a
  single-user desktop app. The phone listener is the exception: it
  authenticates every phone (see above).
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
- **Dependencies**: the 1.23.0 pass reported no known vulnerabilities from its
  then-current `pip-audit` run. Repeat a dependency audit for the actual installed
  versions; this historical result does not certify today's dependency graph.
- **Log hygiene**: no credentials, cookies, keys or tokens in log calls. (B6)
- **Cloud logins**: kept in the system keychain, never in the DB. (B6)
