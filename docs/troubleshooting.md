# Troubleshooting GrabLine

Check the app version under Settings → About and the extension version in your
browser's extensions page. Use the [latest release](https://github.com/GrabLine/GrabLine/releases/latest)
when reproducing a problem, and keep desktop/extension versions aligned.

## The extension cannot connect

1. Launch GrabLine from the installation or source checkout you want to use.
2. Open Settings → Browser Integration and use **Pair browsers** or Browser Setup.
3. Reload GrabLine Connect in the browser's extensions manager, reload the page,
   and open the popup again. A browser restart may be needed after registration.
4. For unpacked development installs, select the repo's `extension/` folder.
   Store ZIPs have different manifest handling; an arbitrary unpacked copy can
   have an extension ID the native-host allow-list does not accept.

**Not paired** indicates host discovery/registration failed. **App not running**
means the host can answer but does not see a running app. Explicit URL handoffs
may queue for next launch; automatic takeover requires the app to be running.
Open GrabLine/Open Settings focuses a running app, so launch it first.

Do not use an old `127.0.0.1` test URL to diagnose pairing. The app's production
browser bridge uses Native Messaging, not a local web server. (Phone pairing is
separate and uses the optional phone listener on your local network.) QA fixture URLs
exist only while their specific test server is running.

## A browser download stays in Chrome or Firefox

Check that takeover is enabled in the extension popup and the app is running.
Browser-generated blobs, POST exports and supported verification-session
exclusions intentionally stay in the browser. Chromium keeps a paused fallback
while GrabLine works; Firefox's interception path differs. See
[the extension guide](../extension/README.md#download-takeover).

Hover buttons also require the master/per-site switches to be enabled. Images
are opt-in. Browser-internal pages and restricted store pages cannot host ordinary
content scripts. On a changed media site, try its canonical page URL and record
whether the failure is discovery, handoff or the actual media download.

## A transfer does not start or resume

| Symptom | Check |
| --- | --- |
| Queued indefinitely | Concurrency, queue schedule/dependency, paused state, network/battery conditions and free disk space |
| Authentication or 403 error | Expired URL, required account, matching saved cloud account and access from the source browser |
| Repeated retries | Server availability, proxy credentials, source limits and the actual error message |
| Resume restarts | The resource identity/range support may have changed; preserve logs and verify the final file |
| Filename already exists | GrabLine reserves a numbered destination; inspect the job's destination rather than an older file |
| HTTPS certificate error | Check the certificate, host and system clock; inspect any intentionally configured self-signed exception |
| SFTP host-key mismatch | Verify the server fingerprint through a trusted channel before changing remembered trust |
| Torrent has no peers | Seeder/tracker/DHT availability, proxy/listen configuration and the peer status |

A browser being able to play a page does not guarantee a downloadable media
format. DRM, login/region restrictions, expiring URLs and upstream extractor
changes can prevent a download. Include a short public sample when reporting
a site issue; do not share private session cookies.

## Missing FFmpeg, Deno or archive tools

Use Video Downloader settings to inspect/install FFmpeg. Set a deliberate FFmpeg
path override in Advanced if needed. FFmpeg and Deno downloads use pinned hashes;
an upstream 404 needs a verified pin update, not disabled integrity checks.

RAR/7Z extraction requires an installed 7-Zip executable. Wrong passwords,
unsupported encryption and invalid/traversing archive paths are separate errors.
Keep the original archive until extraction has succeeded and you have checked
the output. Do not enable delete-after-success while investigating extraction.

For source installations, [Contributing](../CONTRIBUTING.md) lists dependencies.
Check tools in PowerShell with `Get-Command ffmpeg, ffprobe, deno, 7z`.

## Settings, notifications or startup look wrong

Click Save, reopen the section and check the stored value. Language and logging
changes need a restart. Quiet hours suppress notices/sounds without pausing
downloads. A tray-resident app may still be running after its window closes;
use its Quit action when an actual restart is needed.

Windows notification delivery, audible sound, login startup, VPN detection and
power actions also depend on the OS/session/hardware. Report their observed
effect separately from whether a setting saved. Do not run disruptive power
tests on a working personal PC; use the disposable guest harness.

## Logs, diagnostics and reporting

Settings → About → **Copy diagnostics** collects version/tool information.
Settings → Advanced shows the data folder and file-logging option; enable it,
restart, reproduce once and inspect `grabline.log`. `native-host.log` records
host-side errors; `crash.log` records startup/native/Python exceptions when
available. Locations are in the [install guide](install.md#data-and-backups).

Use the repository's bug-report form with:

- App/extension version and OS/architecture; installed, portable or source build.
- Exact steps, expected behaviour, actual behaviour and the error text.
- A public/synthetic URL and a small screenshot or sanitized log excerpt.
- Whether the result reproduces in the app alone or only through the extension.

Remove credentials, cookies, token-bearing URLs, private paths and unrelated
history before posting. Never attach a personal database, browser profile,
keyring dump or raw crash dump. Follow [SECURITY.md](../SECURITY.md) for an
exploitable issue instead of posting it publicly.
