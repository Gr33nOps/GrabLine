# GrabLine documentation

These guides describe the 1.32.15 application and current repository workflow.
Use the [latest release](https://github.com/Gr33nOps/GrabLine/releases/latest)
for packaged downloads; the `dev` branch may contain changes beyond that build.

## Install and use

| Guide | What it covers |
| --- | --- |
| [Installation](install.md) | Windows, macOS and Linux packages; checksums; updates; backups; uninstall |
| [User guide](usage.md) | Adding files, media, torrents and cloud URLs; queues; settings reference |
| [Troubleshooting](troubleshooting.md) | Connection status, transfer failures, missing tools and diagnostics |
| [GrabLine Connect](../extension/README.md) | Browser pairing, takeover, hover buttons, media discovery and development |
| [Release notes](releases/README.md) | Current changes and historical source notes |

## Develop and verify

| Guide | What it covers |
| --- | --- |
| [Contributing](../CONTRIBUTING.md) | Prerequisites, cross-platform setup, checks and review workflow |
| [Windows development](windows-development.md) | PowerShell setup, the local checkout and test-profile isolation |
| [Testing](testing.md) | Suite commands, test levels, the release baseline and external blockers |
| [Windows QA harness](../scripts/windows_qa/README.md) | Resumable browser, settings, media, cloud and sustained-load checks |
| [Disposable guest tests](../scripts/windows_qa/guest/README.md) | Installation, upgrade, reboot/login and guest-only power actions |
| [Architecture and learning](learn-grabline.md) | Source map and exercises tracing a download end to end |
| [Performance](performance.md) | Dated benchmark results and reproduction methods |

## Maintain and publish

| Guide | What it covers |
| --- | --- |
| [Release process](../RELEASING.md) | Versioning, verification, branch history and the current retention policy |
| [Packaging](../packaging/README.md) | Native build tools, portable bundles, installers and extension ZIPs |
| [Store listing kit](store-listing.md) | Extension descriptions, permissions and reviewer notes |
| [Security model](security-model.md) | Trust boundaries and enforced/advisory checks |
| [Security reporting](../SECURITY.md) | Private vulnerability reports and supported versions |
| [Privacy policy](../PRIVACY.md) | Local data, browser sessions and service contacts |

For a bug report, include the application version, operating system, exact
steps and a sanitized error or log excerpt. Use the repository's issue forms.
Do not attach a personal database, browser profile, credentials or crash dump.
