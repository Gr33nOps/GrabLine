# Using GrabLine

For installation and pairing, start with the [install guide](install.md).
This guide describes the 1.32.15 interface; translated labels may differ.

## Add and manage downloads

Paste a file URL into Add Download, drop a URL onto the window, or use the
browser extension. Review the destination, filename and queue before starting.
Existing filenames receive a numbered alternative rather than being overwritten.

Use the Downloads view to pause, resume, cancel, retry or inspect a job. The
details drawer exposes transfer information and the destination. Search and
filters narrow the list; use the row actions for file management and Explorer
reveal. A completed label is a transfer result, not a malware guarantee. Check
the checksum against a trusted publisher when one is available.

Resume depends on the source. If the resource changed or no longer supports
the required ranges, GrabLine may restart it. Keep partial files and the app's
database together when preserving resumable work.

## Video and audio

Send the original media page URL when possible. The app resolves it and opens
Download Info with the formats offered by that source. Pick video or audio,
then select available subtitle, chapter, thumbnail, metadata, trim or conversion
options. Playlist choices and caps apply to that batch.

FFmpeg is needed for merging and conversions; JavaScript-dependent extraction
can need Deno. Settings → Video Downloader reports tool availability. A site's
login, bot checks, region, source formats and yt-dlp support affect the result.
The app may use a local browser session for applicable downloads; see
[Privacy](../PRIVACY.md). DRM and account restrictions are not bypassed.

## Browser actions

The popup shows connection status, detected media and recent downloads. Use its
quick actions to send a URL or current tab. Hover buttons send the intended media
or canonical page URL to the app; image hover is opt-in. Right-click actions can
send a link, image, media element, selected URLs or a page collection.

For a collection, review selection in the app before Download All. Cancel leaves
the collection unqueued. Disable takeover in the popup to keep ordinary browser
downloads in the browser. See the [extension guide](../extension/README.md) for
preferences, recovery and browser differences.

## Torrents and cloud files

Add a magnet, open a `.torrent`, or add a batch. Configure seeding, limits, RSS
and watch folders in Torrent settings. Peer availability determines progress;
local completion tests do not demonstrate public tracker availability.

For FTP/FTPS, SFTP/SCP, WebDAV and S3-compatible downloads, add the appropriate
account under Cloud Downloads and use the matching URL. S3-compatible accounts
can use a custom endpoint. Cloud secrets are stored in the OS keyring; account
labels and connection details are stored locally. An exported settings file
does not migrate those keyring secrets. SSH host-key changes require inspection.

## Queues and scheduling

Use Queue Manager for named queues, concurrency and dependencies. Select a queue
when adding work. A queued job may be waiting for a slot, dependency, time window,
network condition or explicit start. Check both job and queue state before
assuming it is stuck. Bandwidth limits can apply globally, per host and per job.

Queue-finished actions can quit the app or request an OS power/session action.
Choose **nothing** for ordinary use unless you intentionally want that action.
Quiet hours suppress notifications and sounds while transfers continue.

## Settings reference

Use **Save** to persist edits; Cancel discards unsaved field changes. Action
buttons such as Import, Reset, clearing data or pairing can take effect during
the action itself. Language changes ask for a restart; logging changes apply on
the next launch. Import and Reset refresh the visible controls.

| Section | What to configure or inspect |
| --- | --- |
| General | Language, startup/update checks, tray/close behaviour, exit confirmation and new-download confirmation/start policy |
| Downloads | Destination, categories, save prompts, simultaneous jobs and disk-space warning |
| Download Engine | Connections per file, speed limits, sharing, host limits and retry/transfer policy |
| Browser Integration | Browser setup/pairing, clipboard monitoring and browser-related preferences |
| Video Downloader | FFmpeg, playlist preselection cap, preferred/default quality, browser quality confirmation, MP3 bitrate and optional cookies-file fallback; additional options are per download |
| Torrent | Peer/listen settings, seeding, proxy-related behaviour, RSS and watch-folder options |
| Cloud Downloads | Saved accounts, credentials and service connection details |
| Archive Manager | Extraction preferences, external archive tools and post-extraction behaviour |
| File Management | Naming, duplicate/file handling and management preferences |
| Queue Manager | Default queue and queue behaviour |
| Scheduler | Time/network/battery conditions and queue-finished actions |
| Network | Proxy, network conditions and VPN-related configuration/status |
| Security | TLS policy, trusted self-signed certificates, scanner and reputation checks |
| Notifications | Completion/failure/queue notices, sound, folder opening, duration and quiet hours |
| Statistics | Local collection, retention, dashboard interval, CSV export and clearing statistics |
| Appearance | System/light/dark theme, accent, density and visible columns |
| Advanced | Completion command, FFmpeg override, logging, settings import/export/reset and database maintenance |
| Shortcuts | Key bindings, resets and conflict information |
| About | Version, updates, project/support links and Copy diagnostics |

The 1.32.15 audit inventories 150 editable controls, including 38 shortcut
bindings. This is an inventory for that revision, not a promise that later
versions have the same count. See [Testing](testing.md) for what was verified.

## Back up or move computers

Use Advanced → Export settings for preferences, and the download-list export
for a shareable history. Exports remove selected credentials; treat URLs, account
metadata and notes as potentially private and inspect before sharing.

For a full local backup, close GrabLine and its browser/native-host connections,
then copy the [data folder](install.md#data-and-backups) along with any wanted
download/partial files. OS keyring credentials must be configured separately on
the destination. Update absolute paths after moving between Windows and Linux.
