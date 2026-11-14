# Contributing to GrabLine

Bug reports, documentation corrections, translations and focused code changes
are welcome. Start with the [documentation index](docs/README.md) and
[source walkthrough](docs/learn-grabline.md).

## Before you start

- Report a reproducible problem using the GitHub bug form. Include the version,
  operating system, steps, expected result and actual error.
- Discuss substantial feature or architecture changes in an issue first.
- Report exploitable findings through [SECURITY.md](SECURITY.md), privately.
- Keep reports and reviews respectful, specific and focused on the work.

## Development setup

CI and release builds use **Python 3.12**. The package requires Python 3.12 or
newer, but newer interpreters also need compatible PySide6/libtorrent wheels.
Git is required. Deno runs the extension's JavaScript tests. FFmpeg/FFprobe and
7-Zip enable the corresponding media/archive integration checks.

### Windows PowerShell

```powershell
git clone https://github.com/GrabLine/GrabLine.git
Set-Location GrabLine
git switch dev
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m app
```

Calling the environment's Python directly avoids activation-policy changes.
See [Windows development](docs/windows-development.md) for the existing PC
checkout, optional QA tools and isolated test commands.

### Linux / macOS

```sh
git clone https://github.com/GrabLine/GrabLine.git
cd GrabLine
git switch dev
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m app
```

Linux also needs Qt runtime libraries. Ubuntu 22.04 CI installs `libegl1`,
`libgl1` and `libxkbcommon0` for offscreen tests; a visible X11 desktop can need
additional xcb libraries listed in the
[release workflow](.github/workflows/release.yml). Use your distribution's
packages rather than assuming a pip install supplies system GUI libraries.

## Checks

With the virtual environment active, run these from the repository root:

```sh
python -m pip check
python -m ruff check .
python -m ruff format --check .
python -m mypy app --platform linux
```

The explicit mypy platform matches Linux CI; it does not execute Linux code on
Windows. Run the Python suite with an isolated profile using the commands in
[Testing](docs/testing.md), then run extension checks when applicable:

```sh
deno test --allow-read extension/test
```

For a code fix, first capture a failing regression, apply a focused correction,
and rerun it plus related tests. Validate downloaded bytes with hashes; inspect
transformed media with FFprobe. A local fixture pass is distinct from a public
site, actual toolbar popup, native GUI or VM pass.

CI runs the full non-public-network Python suite on Linux and selected
platform-sensitive cases on Windows/macOS. It does not currently run the Deno
suite or every opt-in audit. See [Testing](docs/testing.md) for the full scope.

## Branches and pull requests

- `dev` is the integration branch. Start a topic branch from current `dev` and
  normally target pull requests there.
- `main` is the public stable branch. Changes reach it
  by ordinary merges or fast-forwards. Preserve history; never force-push it.
- Version tags trigger installer publication. Ordinary development/documentation
  pushes do not publish a release. Follow [RELEASING.md](RELEASING.md) when a
  new version is explicitly ready.

Check `git status`, fetch `origin`, and preserve other contributors' changes.
Keep a pull request focused; explain the problem, resulting behaviour and actual
validation. Use the supplied PR template. Do not commit downloaded tools,
build output, local test evidence, profiles, databases, secrets or raw dumps.

The [website](https://grabline.github.io/) has its own repository,
[GrabLine/grabline.github.io](https://github.com/GrabLine/grabline.github.io).
Desktop code, the browser extension, packaging and QA stay together here.
See [Website maintenance](docs/website.md) for deployment and old-link checks.
Published tags are immutable; `main` and `dev` reject deletion and force pushes.

## Design and security conventions

Downloads run off the Qt GUI thread. Keep engine/scheduling work in `app/core`
and `app/engines`; widgets observe that state. Pass subprocess arguments as
lists, preserve credential origin scoping, and retain TLS, archive-path and
native-message validation. Read the [security model](docs/security-model.md)
before changing these paths.

Use existing UI tokens and translation helpers. Follow the JSON catalogs in
`app/i18n` and the scripts under `tools` for translation changes. Update the
relevant user/developer guide when behaviour changes, check relative links and
avoid presenting historical measurements as current guarantees.

Contributions are distributed under the repository's
[AGPL-3.0-or-later license](LICENSE).
