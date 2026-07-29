# Privacy Policy: GrabLine & GrabLine Connect

*Last updated: 2026-07-23*

GrabLine (the desktop app) and GrabLine Connect (the browser extension) are
open-source software published under AGPL-3.0. Their privacy model is simple:

**GrabLine does not collect analytics, run ads, or create accounts.** Nothing
is sent to the GrabLine authors. The app and extension do not phone home with
usage data. Network traffic only happens for the work you ask for, plus a few
optional or maintenance contacts described below.

## What stays on your device

- Your download queue, history, and settings live in a local SQLite database
  under your user profile. Delete the folder and they are gone.
- Cloud login passwords and key passphrases are stored in your OS keychain
  (Windows Credential Manager, macOS Keychain, or Linux Secret Service), not
  in GrabLine's files.
- URLs you hand off from the browser travel over Native Messaging (a local
  stdio pipe between your browser and the app) into that same local database.
  No network service, port, or server of ours is involved.

## What touches the network

- **Downloads you start** contact that URL's server (and, for Smart Engine
  sites, the pages yt-dlp needs to resolve it). Torrents talk to peers and
  trackers you chose.
- **Update check** (on launch, quiet): GrabLine asks GitHub's releases API
  whether a newer version exists. Only the version check runs; no usage data
  is attached. You can ignore or dismiss any update prompt.
- **FFmpeg / Deno**, when you install them from Settings, are fetched over
  HTTPS from their official build hosts and verified against pinned SHA-256
  checksums.
- **Optional VirusTotal / Google Safe Browsing** (off by default): if you
  paste your own API keys, GrabLine may send a file hash (VirusTotal) or a
  URL (Safe Browsing) to those services. They never run unless you enable
  them with your keys.
- The optional **"Use my browser session"** feature (off by default) lets
  yt-dlp read cookies from your browser for a download. Those cookies are
  used in memory for that job and are not uploaded to GrabLine.

## Browser handoffs

When GrabLine Connect sends a URL to the app, optional request headers from
the page (such as `Referer` or `Cookie`) may be stored with that job in the
local database so a gated download can finish. They stay on your machine.
Exports of the download list strip those headers so you do not share session
cookies by accident.

## The extension specifically

- The per-tab media list (what the page's own network traffic loaded) is kept
  in `storage.session` and is discarded when the tab closes. It never leaves
  the browser except as a URL (and related handoff fields) you send to your
  local app.
- The extension makes no requests of its own, injects no remote code, and
  talks to exactly one destination: the GrabLine app on your machine.

## Questions

Open an issue: https://github.com/Gr33nOps/GrabLine/issues
