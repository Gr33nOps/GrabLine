# Testing and coverage

Tests are reproducible evidence for a specific revision and environment.
Different levels answer different questions: a unit test, local service transfer,
real browser, native GUI, public provider and disposable VM are separate results.

## Python checks

Install `.[dev]` as described in [Contributing](../CONTRIBUTING.md), then run:

```sh
python -m pip check
python -m ruff check .
python -m ruff format --check .
python -m mypy app --platform linux
```

Keep FFmpeg/FFprobe and 7-Zip on PATH for their integration cases. Missing tools
may cause skips; inspect them rather than counting them as passes. Tests marked
`network` contact public services and are excluded from ordinary CI. The remaining
suite still uses real loopback HTTP and local protocol services.

### Windows PowerShell

This creates a fresh test profile and restores the process environment afterwards:

```powershell
$qaProfile = Join-Path ([System.IO.Path]::GetTempPath()) ('grabline-tests-' + [guid]::NewGuid())
$savedLocalAppData = $env:LOCALAPPDATA
$savedQtPlatform = $env:QT_QPA_PLATFORM
try {
    $env:LOCALAPPDATA = $qaProfile
    $env:QT_QPA_PLATFORM = 'offscreen'
    .\.venv\Scripts\python.exe -m pytest -m 'not network' --timeout=90 --timeout-method=thread -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw 'Python tests failed; inspect the output.' }
} finally {
    $env:LOCALAPPDATA = $savedLocalAppData
    $env:QT_QPA_PLATFORM = $savedQtPlatform
}
```

The test profile remains available for inspection; remove only your test-owned
directory once evidence is saved. Never use a personal database for QA.

### Linux / macOS

With the environment active, isolate the home/data directories for the child:

```sh
qa_profile=$(mktemp -d)
HOME="$qa_profile" XDG_DATA_HOME="$qa_profile/data" QT_QPA_PLATFORM=offscreen \
  python -m pytest -m 'not network' --timeout=90 --timeout-method=thread -p no:cacheprovider
```

For a focused reproduction, replace the suite arguments with the relevant test
file/node. Run `python -m pytest -m network` only when you intend public service
traffic and can inspect each failure's cause.

## Extension and real application checks

```sh
deno test --allow-read extension/test
```

The Deno suite checks JavaScript logic in isolation. It does not install an
extension, open its actual toolbar popup or certify a live website.

The [Windows audit harness](../scripts/windows_qa/README.md) adds authenticated
local cloud services, hashed file transfers, media inspection, settings,
Playwright/browser sessions, native Windows UI checks, packaging and a one-hour
load run. Start from preflight; keep separate databases, download directories,
browser profiles and disposable credentials. Read actual dynamic fixture URLs
from the run manifest. Save per-case evidence and preserve failed attempts.

Use [disposable guest checks](../scripts/windows_qa/guest/README.md) for
installation/uninstallation, upgrade, startup/login/reboot and power actions.
Windows Sandbox supports a subset and cannot certify persistent reboot/login.

## CI scope

[CI](../.github/workflows/ci.yml) runs the full non-public-network Python suite,
lint, formatting and type analysis on Ubuntu 22.04 / Python 3.12. A second matrix
runs selected platform-sensitive cases on Windows and macOS. The extension Deno
suite and opt-in Windows audit are separate commands. Native release builds
run on each target OS; a successful build is distinct from a complete feature audit.

## Recorded 1.32.15 baseline

Verified on **14 November 2026**, before these documentation changes:

| Check | Recorded result |
| --- | --- |
| Full Windows suite | 1,021 passed; 13 platform skips; 15 public-network cases deselected |
| Full Linux CI suite | 1,032 passed; 2 skipped; 15 public-network cases deselected |
| Extension logic | 42 passed |
| Windows/macOS CI subset | Passed |
| Lint, format, dependency and CI-target type checks | Passed |
| Native release artifacts | Windows, macOS and Linux builds succeeded; 12 assets verified against published hashes |
| Published Windows portable | Frozen GUI/native-host/HTTP transfer completed with the expected SHA-256 in an isolated profile |
| Settings inventory | 150 editable controls across 19 sections, including 38 shortcut bindings; persistence and effects retain separate evidence |

Release source commit: `28170759ceba7915bbaa40a9846647978ddf8ea8`.
Corresponding development commit: `1d36bc9c24f68f5e24caf18c000ce8ba2fcd7a18`.
Both had source tree `979e261326a385753d5f207de26d434f923379c6`.

Public evidence: [CI run](https://github.com/Gr33nOps/GrabLine/actions/runs/43605774944),
[native builds](https://github.com/Gr33nOps/GrabLine/actions/runs/43606317150),
and [release notes](releases/1.32.15.md). Detailed local browser/settings/guest
ledgers and sanitized logs are produced by the harness, not bundled into the app.
These are historical results for that revision, not fresh test claims for later edits.

## Limits that remain explicit

- Local authenticated services do not certify external AWS/Drive/Dropbox/OneDrive/
  Nextcloud/Box accounts or every yt-dlp website. Controlled site DOM fixtures
  do not substitute for a successful live-site download.
- Reputation-service error handling is covered separately from valid-key API
  success. No dedicated live keys were supplied for certification.
- Persistent login/reboot and previous-version upgrades need a persistent VM and
  verified archived packages. Earlier public release assets/tags were removed
  during repository cleanup; do not substitute a fabricated previous installer.
- Physical battery, router mappings, VPN/ISP behaviour, visible Windows toasts
  and human-confirmed audibility require the corresponding environment.
- A popup opened in a browser tab is distinct from the actual toolbar popup.
  A captured OS callback is distinct from observing its physical effect.

Each audit case records `PASS`, `FAIL`, `BLOCKED` or `NOT APPLICABLE`, a testing
level and evidence. A missing prerequisite remains blocked; a prepared script,
skip or unsupported guest state must never count as a passing feature.
