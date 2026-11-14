# Windows development and QA

The development checkout is `D:\Projects\app dev\GrabLine`, on branch `dev`.
Its `.venv` is the application environment. Keep synthetic QA history and browser
profiles outside that checkout; the normal user database is under
`%LOCALAPPDATA%\Grabline`.

From PowerShell in the checkout:

```powershell
.\.venv\Scripts\python.exe -m app
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy app --platform linux
```

The explicit mypy platform matches the existing CI target. It does not claim
that Linux-only branches were executed on Windows.

For the Windows suite, first set a dedicated data directory and supply verified
FFmpeg, FFprobe and 7-Zip executables on PATH:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:LOCALAPPDATA = Join-Path $env:TEMP 'grabline-unit-profile'
.\.venv\Scripts\python.exe -m pytest -m 'not network' --timeout=90 --timeout-method=thread -p no:cacheprovider
```

Use a fresh terminal to return to the normal desktop profile, or remove the two
environment overrides before launching the app normally. Do not test against a
personal database. Network-marked tests and external service accounts require
their own explicit evidence and are not certified by this local suite.

The extension tests use the installed Deno executable:

```powershell
deno test --allow-read extension/test
```

See [the Windows audit harness](../scripts/windows_qa/README.md) for real browser,
native GUI, local authenticated services, media validation, resumable cases,
packaging and the one-hour soak. The disposable guest package is separate.

Before editing, inspect `git status` and fetch `origin`. Review a change with
`git diff`; run the affected reproduction and required checks before committing.
Push the reviewed commit to `origin dev`. Publishing a release or tag is a
separate action.

Native-host registration points to the development launcher and its source.
When testing an isolated source revision, the harness uses a startup path override
for its own child processes. It does not replace normal browser registration.
For normal extension development, reload the unpacked extension after a change;
browser restart alone can retain installed script caches. Audit profiles are
keyed to an extension source hash and verify the running handoff function.

The November 2026 Windows audit adds reproducible local-service/browser/Qt QA
commands and regression fixes. Its full suite reached 990 passing tests, with
14 POSIX-only skips and 15 public-network tests excluded; the extension suite
reached 42 passes. Separate public samples, local authenticated transfers and
packaged smoke tests are recorded by the audit harness.

A native 0xC0000005 crash during accessibility inspection remains unresolved.
Do not interpret the test counts as complete native-GUI or external-provider
certification. Disruptive Windows integration remains gated on a disposable VM;
Hyper-V management was denied to the current account during the audit. Current
blockers and exact build/source identities belong in the generated audit ledger.


For an exhaustive settings pass, use the settings commands in the audit harness.
They inventory every exposed field, click Save, reopen against a fresh database
connection, check Cancel, exercise maintenance actions and verify runtime effects.
Do not equate a stored setting with its physical effect: startup/power actions
need the disposable VM, API integrations need test keys, and battery/router
behaviour needs the corresponding hardware. Logging is applied on next launch.
Settings imports now reload the visible fields and live consumers immediately;
Reset also reloads shortcut editors, and RSS/watch intervals update on Save.

The follow-up settings pass reached 993 passing Windows tests (14 POSIX-only
skips, 15 public-network cases excluded) and 42 extension passes. All 150 exposed
controls passed changed-value Save/reopen/Cancel checks. This verifies the stated
Qt/local scopes; unavailable physical/VM/provider effects remain separate.
