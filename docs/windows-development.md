# Windows development and QA

Use Python 3.12 and PowerShell. On the configured development PC the checkout is
`D:\Projects\app dev\GrabLine`, branch `dev`; its `.venv` is the application
environment. On another machine choose any writable checkout path.

## Set up and run

For a new checkout, follow [Contributing](../CONTRIBUTING.md). For the existing
PC checkout:

```powershell
Set-Location 'D:\Projects\app dev\GrabLine'
git status --short --branch
git fetch origin
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m app
```

Only create the environment when it is missing:

```powershell
py -3.12 -m venv .venv
```

No activation or PowerShell execution-policy change is required. Do not run
`git reset --hard` or overwrite another contributor's work to update a checkout.

## Verify a change

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy app --platform linux
deno test --allow-read extension/test
```

The mypy platform is the existing CI target. It does not imply execution of
Linux-only branches on Windows. [Testing](testing.md) contains the isolated
full-suite command and explains platform skips and public-network exclusions.

## Tools and profiles

| Tool | Needed for |
| --- | --- |
| FFmpeg and FFprobe | Media generation, merging/conversion and output inspection |
| Deno | Extension tests and applicable yt-dlp JavaScript extraction |
| 7-Zip (`7z`) | External archive extraction checks, including RAR/7Z |
| PyInstaller and Pillow | Local native packaging and icons |
| Playwright, pywinauto and pywin32 | Opt-in real browser / Windows GUI audit |

Check installed tools before dependent cases:

```powershell
Get-Command ffmpeg, ffprobe, deno, 7z -ErrorAction SilentlyContinue
```

Keep QA databases, downloads and browser profiles outside the source checkout.
The normal database lives under `%LOCALAPPDATA%\Grabline`; never point a test
at personal history. Both app and native host must use the same isolated data
directory and source revision. The `.venv` launcher PID may differ from the
actual Python child owning the app window.

## Browser development

Use [GrabLine Connect's pairing guide](../extension/README.md). Register a source
host only when intentionally pairing this development checkout; doing so changes
the browser's normal host registration. Reload the unpacked extension after code
changes and check the loaded source/version. Browser restart alone may retain
cached installed scripts.

The [Windows QA harness](../scripts/windows_qa/README.md) uses disposable profiles
and explicit child-process overrides. Start its fixture server on a fresh dynamic
port and read the generated manifest; old localhost addresses are not reusable.
Allow only one test to control desktop input at a time.

## Review, commit and publish

Review `git diff`, run the affected checks, then commit and push the topic branch
or reviewed `dev` change. Changes on `main` need the normal CI workflow.
The website deploys from its [separate repository](website.md).
Installer publication is a separate versioned action described in
[RELEASING.md](../RELEASING.md); do not create a tag for a documentation update.

## Evidence and known limits

The [testing guide](testing.md) records the 1.32.15 release baseline and links
its CI/build runs. Settings coverage distinguishes all 150 editable controls in
19 sections from physical OS/provider effects. Save/reopen/Cancel assertions do
not prove a power transition, audible sound, visible toast or authenticated
external service.

Windows crash logging retains the faulting thread's native trace, avoiding the
all-thread dumper crash reproduced during the audit. This corrects that observed
path, not every possible native failure. Use the harness's individual results and
keep guest-only actions in a [disposable VM](../scripts/windows_qa/guest/README.md).
