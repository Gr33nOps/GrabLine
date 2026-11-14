# Privacy policy: GrabLine and GrabLine Connect

Last reviewed: **14 November 2026**, against version **1.32.15**.

GrabLine does not run ads, collect usage analytics or require a GrabLine account.
There is no author-operated service receiving your download history. The app
and extension run locally; downloads and enabled integrations contact the
services described below.

## Local data

The app stores queue/history, statistics and settings in `grabline.db` under
the [platform data folder](docs/install.md#data-and-backups). This can include
URLs, filenames, destination paths, notes, API keys and request headers needed
for a download. Full database copies and crash dumps can therefore be sensitive.

Cloud account passwords and key passphrases use the OS keyring, such as Windows
Credential Manager, macOS Keychain or a Linux keyring backend. Account labels,
hosts, usernames, ports, endpoints and key-file paths are stored in the local
settings database. Migrating that database does not migrate keyring secrets.

Application, native-host and crash logs stay local. File logging is configurable;
logs and diagnostics may reveal URLs, local paths and error context. Inspect
and sanitize them before submitting a report. No automatic crash-report upload
is configured by GrabLine.

## Network contacts

| Activity | What leaves the device |
| --- | --- |
| File and cloud transfers | Requests to the selected server/provider, including credentials needed for that transfer |
| Media extraction | Requests yt-dlp needs to resolve the selected media page and retrieve its available content |
| Torrents | Peer/tracker/DHT traffic according to torrent/session configuration; peers can observe your network address |
| Update discovery | Requests to GitHub's releases API; startup checks can be disabled in General settings |
| Tool installation | FFmpeg/Deno downloads from configured upstream HTTPS hosts, checked against pinned SHA-256 values; may occur when a media operation needs a missing tool |
| RSS and watched sources | Requests required to refresh configured feeds or sources |
| Wait for network | When enabled, connectivity probes to public DNS endpoints `1.1.1.1` and `8.8.8.8`; no application payload is sent, but the endpoint sees a connection |
| VirusTotal, optional | File SHA-256 lookup using your API key; GrabLine does not upload file contents through this lookup |
| Google Safe Browsing, optional | URL lookup using your API key |

The service or proxy receiving a request can see normal connection metadata.
Third-party services apply their own policies. Opening project, support or
store links also contacts those sites in your browser.

## Browser sessions and handoffs

GrabLine may read an available local browser cookie store through yt-dlp for
applicable media downloads, including YouTube/session-dependent paths and login
retries. The selected/session browser and extractor path affect when this occurs.
The old “Use my browser session” switch is deprecated; it is not an effective
global opt-in/off control in this version. Cookies can be sent to their matching
source services as part of authenticated downloads; they are not sent to the
GrabLine authors.

The extension may attach request context such as `Referer`, `User-Agent` or
cookies when sending a chosen URL to the app. That local handoff uses Native
Messaging stdio, not an author-operated server or listening localhost port.
Required headers can be retained with a job in the local database. Sensitive
credentials are origin-scoped by the transfer pipeline; see
[the security model](docs/security-model.md).

## Extension storage and permissions

The extension stores preferences and per-tab detected-media state locally in
browser extension storage. Detected state is removed on tab cleanup and session
storage is cleared with the browser session. Recent-download/progress views read
the local app's job state through the native host.

Host, tab, request, cookie and download permissions support discovery, handoff
and takeover. These allow access to sensitive browsing context; only enable the
extension in profiles where you want those features. GrabLine Connect has no
remote analytics or remote-code loader. Its own app communication uses Native
Messaging; browser fallback downloads still make normal network requests.

## Export, removal and reporting

Settings export omits reputation API keys and redacts proxy credentials.
Download-list export strips request headers. Neither is a blanket guarantee that
every field is anonymous: URLs, notes, paths and account metadata can still be
sensitive. Full backups preserve the underlying local data.

Use the app to remove saved cloud accounts if their keyring entries should be
deleted. Uninstalling the program does not necessarily remove history, downloads,
browser profiles or OS keyring records. See [uninstall instructions](docs/install.md#uninstall).

Ask a general question through [GitHub issues](https://github.com/Gr33nOps/GrabLine/issues).
Use [SECURITY.md](SECURITY.md) for a private security report. Do not attach
personal databases, browser profiles, credentials or raw crash dumps to public issues.
