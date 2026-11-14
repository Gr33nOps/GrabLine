# Disposable Windows guest checks

This package is prepared for a clean, administrator-provisioned Windows VM.
It has **not run on the host**. Do not use the unrelated existing VirtualBox VM.

1. Provision a licensed supported Windows guest, browser and clean snapshot.
   Disable shared personal folders and automatic clipboard synchronization.
2. Copy the current and previous GrabLine installers, current portable build,
   this directory and the source revision recorded in the QA manifest into the guest.
   Record ISO/version, VM provider, snapshot ID, package SHA-256 and guest identity.
3. Run `guest.ps1 -Stage Preflight -Evidence C:\GrabLine-QA`.
   It refuses a physical machine and the known development host name.
4. Observe signatures and SmartScreen normally. Do not bypass a security block.
   With the clean snapshot selected, run `Install` with `-Installer`.
5. Run `Inspect`, then the individual scenarios below. Record screenshots,
   expected and actual state, hashes and guest-only application logs after each.
6. Restore the clean snapshot before the previous-version upgrade branch.
   Start the previous build, create synthetic settings/history and a deterministic
   completed download, then upgrade with the app closed and separately running.
7. Run `Uninstall` only after supplying the exact synthetic download directory.
   Confirm test downloads remain byte-identical and application registration is gone.
   Inspect residual user data separately: preserving it is different from leaving
   broken associations/native-host registration behind.

## Individual required cases

| ID | Required action and assertion |
|---|---|
| VM-INSTALL | Fresh install; first-run wizard; default paths; app and host launch |
| VM-PORTABLE | Launch portable build from a path with spaces; write only test profile |
| VM-UPGRADE-CLOSED | Previous packaged version with synthetic settings/history; upgrade; preserved rows, values and hashes |
| VM-UPGRADE-RUNNING | Upgrade while old process is running; orderly handoff, no stale workers or corrupt DB |
| VM-SHORTCUTS | Start-menu/desktop shortcuts resolve to installed executable |
| VM-ASSOCIATIONS | Open test `.torrent` and `magnet:` from Explorer/browser; exact source arrives once |
| VM-HOST-REGISTRATION | Inspect Chrome/Edge/Firefox manifests and registration; load extension; real native ping and transfer |
| VM-LOGIN | Enable guest-only startup; guest logout/login; one app process, usable UI/native host |
| VM-REBOOT | Guest reboot; startup and native host recover; queued synthetic transfers resume with matching hashes |
| VM-UPDATE-VALID | Use a test update manifest/package; real download, integrity verification and installer handoff |
| VM-UPDATE-BAD-HASH | Alter one package byte; reject before executing; existing app remains usable |
| VM-UPDATE-CANCEL | Cancel a slow update; no installer starts and original app remains usable |
| VM-UNINSTALL | Remove installation; shortcuts/associations/host entries cleaned; synthetic downloads preserved |
| VM-QUEUE-QUIT | After final successful guest job, app quits exactly once |
| VM-QUEUE-LOCK | Guest locks only after final job; operator unlocks; no duplicate action |
| VM-QUEUE-SLEEP | Record `powercfg /a`; supported guest sleeps after final job and resumes intact |
| VM-QUEUE-HIBERNATE | Supported guest hibernates after final job and resumes intact; otherwise NOT APPLICABLE with OS evidence |
| VM-QUEUE-SHUTDOWN | Guest shuts down after final job; host stays running; inspect persisted completion after boot |
| VM-SMARTSCREEN | Record signing/SmartScreen outcome without bypassing any block |

Use queue action settings through the actual app; this script deliberately does
not issue host or guest power commands. Keep a VM console open for observation.
Physical battery, sound audibility and real VPN/ISP behaviour remain separate
hardware/infrastructure checks.

## Result discipline

`PASS` requires the assertions above. `BLOCKED` requires a concrete missing
prerequisite. A prepared script or a successful host build is never a guest pass.
Preserve an earlier failure and its reason when retrying after fixing the setup.
Write each case into `cases/<ID>.json` using the schema in the host harness.
Do not upload browser profiles, vault entries, passwords or personal history.
