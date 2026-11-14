# GrabLine Windows audit harness

For standard suite commands and the recorded release baseline, see
[Testing and coverage](../../docs/testing.md). These scripts are opt-in audit
tools; some report builders retain curated run paths from the original audit.
Review and adapt those paths in an isolated copy before assembling a new report.

These opt-in integration checks use real engines, local services and isolated
Qt/browser profiles. They are not part of application startup. Read each case's
testing level: offscreen Qt, controlled site DOM, actual toolbar popup, external
media service and VM are different claims.

## Setup

Use a writable isolated checkout of the revision under test. Install the project
and development requirements there. Extra QA tools are Pillow, PyInstaller,
Playwright, pywinauto and pywin32. A Playwright Chromium build that supports
unpacked extensions is required; normal Chrome automation is not assumed to work.
Keep FFmpeg, FFprobe, Deno and 7-Zip on PATH. Do not weaken browser security or
install extensions in a personal profile to work around automation limits.

```powershell
$env:PYTHONPATH = (Get-Location).Path
$env:GRABLINE_QA_ROOT = Join-Path $env:USERPROFILE 'Documents\Codex\GrabLine-QA'
$env:GRABLINE_QA_PYTHON = (Resolve-Path '.\.venv\Scripts\python.exe').Path
# Set this to the verified Chrome for Testing executable, not a guessed path.
$env:GRABLINE_QA_CHROME = 'C:\path\to\chrome.exe'
# Optional, only when pywinauto/pywin32 live in a separate QA module directory:
# $env:GRABLINE_QA_GUI_MODULES = 'C:\path\to\gui-tools'
```

Use fresh ROOT for an independent run. Keep the same ROOT to resume. The browser
and host child environment must select the same profile and source checkout.
Do not interpret the Windows venv launcher PID as the actual GUI owner.

## Run order and recovery

1. Run `preflight.py` and record source/dependency/tool identities. Verify scoped
   access before writing changes back to another checkout.
2. Run `fixture_server.py` in a dedicated terminal. It publishes actual loopback
   URLs, title, resource lengths and SHA-256 hashes in `browser-manifest.json`.
   Verify its health endpoint and fixture identity before browser actions.
3. Run `browser_session.py` using a Python environment with Playwright, with
   GRABLINE_QA_PYTHON pointing to the real app Python. It owns one disposable
   browser/app session. Use `command.py` from a separate serial controller.
4. Use native Computer Use if available; then supported browser/CDP controls;
   then persistent Chromium; then the scoped `ui_driver.py` GUI fallback. One
   readiness check plus one recovery attempt per unavailable backend. Only one
   test controls physical desktop input at a time.
5. Run the independent commands below and inspect every case JSON, not just the
   Python process exit code. A `case()` failure is recorded and later cases
   continue. Infrastructure failures must be classified from their evidence.
6. Run the ordinary full suite and packaging sequentially. Only `soak.py` stage 2
   deliberately overlaps a Windows build with torrent transfers.

Actual toolbar popup targets are inspected with CDP after a native toolbar
click. The `popup` session command opens the popup **as a tab** and is a separate
testing level. It must never be reported as a toolbar-popup pass.

## Commands

Run these with the application Python, from the source root:

```powershell
python scripts/windows_qa/qt_checks.py
python scripts/windows_qa/settings_matrix.py
python scripts/windows_qa/settings_extra_checks.py
python scripts/windows_qa/control_inventory.py
python scripts/windows_qa/visual_checks.py
python scripts/windows_qa/scaling_checks.py
python scripts/windows_qa/prepare_rar.py
python scripts/windows_qa/archive_checks.py
python scripts/windows_qa/media_checks.py
python scripts/windows_qa/media_security_checks.py
python scripts/windows_qa/cloud_ftp_webdav.py
python scripts/windows_qa/cloud_s3_authenticated.py
python scripts/windows_qa/cloud_ssh_authenticated.py
python scripts/windows_qa/network_checks.py
python scripts/windows_qa/data_queue_checks.py
python scripts/windows_qa/queue_effect_checks.py
python scripts/windows_qa/wall_clock_checks.py
python scripts/windows_qa/playlist_transfer_checks.py
python scripts/windows_qa/desktop_watch_checks.py
python scripts/windows_qa/hidden_window_checks.py
python scripts/windows_qa/site_selector_checks.py
python scripts/windows_qa/browser_extended.py
python scripts/windows_qa/browser_final_actions.py
python scripts/windows_qa/browser_edge_checks.py
python scripts/windows_qa/browser_native_flows.py
python scripts/windows_qa/browser_failure_checks.py
python scripts/windows_qa/native_windows_checks.py
python scripts/windows_qa/public_media.py
python scripts/windows_qa/packaged_checks.py
python scripts/windows_qa/soak.py --seconds 3600
```

`browser_collections.py` runs the gallery's genuine context-menu/selection sequence,
link cancellation and selected-text cases. Each transfer receives fresh fixture
URLs so old completed jobs cannot satisfy a rerun. Individual cases can resume:

```powershell
python scripts/windows_qa/recheck.py browser_extended BROWSER-PROGRESS-REAL --url-tag fresh-transfer
python scripts/windows_qa/recheck.py browser_native_flows BROWSER-OPEN-SETTINGS
python scripts/windows_qa/recheck.py data_queue_checks QT-DATA-SETTINGS-BACKUP-RESET-RESTORE
```

`browser_checks.py` contains the original restart-preference baseline and some
stateful setup assertions. Use named rechecks for independent cases rather than
blindly rerunning it against arbitrary current preferences. Choose a fresh URL
tag for a transfer recheck; old completed jobs must not satisfy a new attempt.

`missing_host_check.py` runs with the Playwright environment. It changes only the
host namespace in a disposable extension copy to exercise Chromium's real
unregistered-host error. It never unregisters the user's normal host.

## Evidence and limits

`cases/<ID>.json` is the latest result. Reruns preserve the previous result under
`attempts/<ID>`. Logs, screenshots, inspection JSON, job IDs and hashes are
separate artifacts. Never distribute raw profiles, cookie-bearing databases,
private keys, vault dumps or unrelated user history. Cloud credentials are
disposable and cleanup is verified against Windows Credential Manager.

PASS means that case's assertions succeeded at its stated level. A unit or
controlled-DOM pass does not certify a live site. File transfers require hashes;
transformed media requires decoded streams, duration and content inspection.
Generated sound/API success is separate from human-confirmed audibility.

The one-hour run defaults to four simultaneous jobs, validates every output,
rotates successful temporary files, and stops on corruption or insufficient free
space. Its shorter rehearsal mode is not a substitute for 3600 measured seconds.

Installation, upgrades, associations, reboot/login, uninstallation and power
actions belong in [the disposable VM package](guest/README.md). A guest that was
not provisioned remains BLOCKED even when host packaging succeeds. External
private accounts, live API keys, physical battery and VPN/ISP behaviour also
require their actual prerequisites.

## Current audit limits and report assembly

The control inventory contains 150 editable controls across 19 sections, including
38 shortcut bindings. A control/persistence assertion is not certification of a
physical OS action or external provider. `cloud_transfer_controls.py` drives real
authenticated transports and calls genuine pause/cancel methods at a received
chunk boundary. Its results are distinct from clicking the native app toolbar.

A fatal Windows 0xC0000005 in python312.dll was observed during repeated Qt
accessibility inspection of a live quality dialog. The remaining-effects probe
subsequently reproduced the same fault offset in the all-thread traceback dumper;
Windows now uses faulting-thread logging. Three cold native sessions passed with
that production setting. Preserve the failing baseline separately; this bounded
verification does not certify every native path. `fault_dump_probe.py` remains an
opt-in diagnostic. Do not operate Windows Security Alert or
other protected permission dialogs; preserve the blocker and let the user act.

The report builder consumes explicitly selected run directories and keeps earlier
red attempts. Update its curated run paths and verified suite-log identity for a
new audit; do not silently merge every rehearsal as current coverage. Output must
be under this workspace's `outputs` directory:

```powershell
python scripts/windows_qa/build_report.py --output 'C:\path\to\workspace\outputs\grabline-audit'
```

All remaining VM, external account/API, live-site and physical-hardware results
must remain explicit. `NOT APPLICABLE` is not a pass. A prepared guest package is
not an executed VM test. No release or tag is part of these commands.


## Exhaustive settings checks

Use an isolated `LOCALAPPDATA`, set `GRABLINE_QA_ROOT` to a disposable run directory,
and put the verified FFmpeg/Deno/7-Zip locations on PATH. From the source root:

```powershell
python scripts/windows_qa/control_inventory.py
python scripts/windows_qa/settings_matrix.py
python scripts/windows_qa/settings_extra_checks.py
python scripts/windows_qa/settings_reopen_checks.py
python scripts/windows_qa/settings_action_checks.py
python scripts/windows_qa/settings_effect_checks.py
python scripts/windows_qa/visual_checks.py
python scripts/windows_qa/scaling_checks.py
```

The inventory has 150 controls across 19 sections (38 shortcut editors included).
`settings_reopen_checks.py` clicks the embedded Save button for a changed value,
reopens each field against a fresh SQLite connection, checks Cancel preserves
stored preferences, and restores the original value. It isolates the autostart
provider; this is not evidence of actual login registration. A disposable queue
provides an alternate default-queue choice. No personal profile is read or changed.

The action runner records section/button inventories, renders all sections and
checks import/export, invalid/cancelled import, Reset, statistics, database
maintenance, path selection and cancellation, diagnostics, shortcut help,
account/setup dialog opening and About/update dispatch. File-picker results and
OS dispatch are explicitly test boundaries. The effect runner separately checks
live timers, density/columns, manager rates, engine options, job defaults, stats,
libtorrent configuration and the production notification/power callbacks.
Captured OS callbacks are unit checks, not physical notifications or power tests.

Run the genuine pinned FFmpeg installer separately (uses HTTPS and disposable
app storage, downloads an upstream archive and executes the verified binaries):

```powershell
python scripts/windows_qa/settings_ffmpeg_check.py
```

A dated upstream release can disappear under upstream retention policies. If the
installer reports HTTP 404, refresh the relevant platform pin from the official
release asset digest, verify the actual downloaded checksum, and rerun the real
installer. Never remove checksum verification or replace a pin with an unverified
rolling download. Logging changes are documented to apply on the next launch;
`settings_action_checks.py` verifies both enabled/debug and disabled/error states
in fresh processes.

Generate the settings-only report after verification and a separate package build:

```powershell
python scripts/windows_qa/settings_report.py --output 'C:\path\to\workspace\outputs\grabline-settings-audit'
```

All 150 control results are separate from provider/hardware/VM effects in the
report. User-owned credentials, browser registration, physical display settings
and disruptive OS actions are not modified by these settings commands.

## Remaining Windows effects

Use an isolated checkout and a dedicated `GRABLINE_QA_ROOT`. Reuse the verified
runtime/tools, and set `PYTHONPATH` to that checkout. Run one native desktop test
at a time; these opt-in probes can show app windows and play a synthetic tone.

```powershell
python scripts/windows_qa/native_crash_repeat.py --all-threads -1
python scripts/windows_qa/remaining_network_checks.py
python scripts/windows_qa/remaining_audio_check.py
python scripts/windows_qa/remaining_notification_check.py
python scripts/windows_qa/remaining_notification_check.py --foreground
python scripts/windows_qa/remaining_effects_report.py --output 'C:\path\to\workspace\outputs\grabline-remaining-audit'
```

The crash runner's `-1` uses the actual production configuration; `0` forces
faulting-thread logging and `1` reproduces the historical all-thread setting.
The latter may deliberately crash its disposable child. UIA reads verify the
owned process/window/profile before access. Raw crash dumps can contain process
memory; keep them local and exclude profiles, databases and dumps from reports.

The network runner makes real public DHT and invalid-key reputation requests,
using only synthetic hashes/URLs, and probes the current gateway without changing
its configuration. No valid-key certification follows from an error response.
The audio runner requires optional PyAudioWPatch in an isolated tool directory;
it retains aggregate tone energies, not captured system audio. Notification
observation can remain blocked by Windows shell state; do not change system
notification/privacy settings to force a pass.

`remaining_effects_report.py` assembles the explicit host/guest ledger and existing
suite/package evidence; it does not execute missing cases or convert blockers
into passes. Preserve previous attempts on rerun. Use the guest-specific guide
for disruptive integration checks, never run those actions against the host.
