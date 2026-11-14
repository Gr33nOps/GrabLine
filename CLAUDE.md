# GrabLine desktop - notes for Claude sessions

GrabLine is a Python + PySide6 (Qt 6) download manager. This file covers the phone-remote work; the general developer guide is `docs/windows-development.md` and `docs/testing.md`.

## Phone remote (GrabLine for Android)

- Work branch: `feature/phone-remote` (from `dev`). Local commits only.
- Spec, protocol and build plan live in the Android repo (`grabline-android/docs/SPEC.md`, `PROTOCOL.md`, `BUILD_PLAN.md`). The canonical protocol copy and JSON fixtures live here in `protocol/`; both repos test against `protocol/fixtures/`.
- All listener code lives in `app/remote/`. It is the only place allowed to listen on a port (`scripts/check_no_listen.py` enforces this in CI). The listener is off by default, binds only to private and link-local addresses, and has a loopback test mode (off by default) for the Android emulator.
- Transport: Qt `QWebSocketServer` in secure mode; certificates with `cryptography`; pairing QR codes with `segno`. No mDNS.
- Phone links enter the `handoffs` table with source `phone` and an `options` column, and take the unattended add path in `app/ui/main_window.py`: never a modal dialog (quality panel, "Download it again?", low-disk warning). Default quality `best`.
- `handle_message` in `app/native_host/host.py` is reused for existing message types. `PROTOCOL_VERSION` is 6.

## Hard rules

- Before every commit: `ruff check .`, `ruff format --check .`, `mypy app --platform linux` and the affected tests pass.
- Tests run with `QT_QPA_PLATFORM=offscreen` and a temporary `LOCALAPPDATA`; never the real profile.
- Anything touching pairing, auth, TLS, keys or the listener's access checks needs a fable reviewer PASS.
- Never log pairing codes, device keys or signatures. Never commit CLAUDE.local.md.
