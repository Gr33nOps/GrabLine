# GrabLine Connect

A thin, auditable browser extension (MV3, one codebase for Chrome / Edge /
Brave / Firefox). It detects media, decorates the page, and hands URLs to the
desktop app. Every download, merge, and conversion happens in the app; the
whole extension is meant to be readable in one sitting.

## What it does

- **Toolbar popup** styled like the app: pairing status, quick actions (open
  GrabLine, grab this tab, paste a link), the media detected in this tab, a
  live view of your recent downloads, and the interception and hover
  preferences.
- **The hover button hands off to the app**: click the GrabLine logo button on
  a supported site and the URL goes straight to GrabLine, which opens its
  **Download Info dialog**
  (name, folder, category, and a quality choice for video) and starts. The
  quality is picked in the app, not on the page, so there is no in-page panel
  to keep in sync with the site's DOM.
- **Live progress pill**: anything grabbed from a tab shows its percentage
  bottom-right in that tab, streamed from the app over Native Messaging.
- **Right-click → "Download with GrabLine"** on any link, image, video,
  audio, selection, or page
- **Right-click → "Download all images with GrabLine"**: every
  big-enough image on the page lands in the app as a selectable thumbnail
  grid (a wrapping link to a full-size image wins over the thumbnail src)
- **Hover download button** on videos and audio. Images are opt-in via the
  popup, since a button on every avatar and thumbnail is noise and right-click
  plus the gallery grabber already cover images. There is a per-site off
  switch and a master switch in the popup.
- **Download takeover** (on by default): browser downloads of
  media, images (Save As), archives and other files are cancelled and
  re-dispatched to GrabLine for segmented downloading. Toggle off in the
  popup to keep the browser's own download UI.

It talks to the desktop app over Native Messaging only: the browser launches
GrabLine's host process and pipes JSON over stdio. There is no localhost
server and no open port, and the host manifests pin the extension IDs allowed
to connect.

## Pairing (two steps)

1. **Register the native host** - in the GrabLine app: **Settings → Pair
   browsers** (one click, covers Chrome/Chromium/Edge/Brave/Vivaldi/Opera/Arc/Firefox on
   Windows, macOS, and Linux). Terminal alternative:

   ```bash
   python -m app.native_host.install
   ```

2. **Load the extension:**
   - **Chrome / Edge / Brave:** `chrome://extensions` → enable *Developer
     mode* → *Load unpacked* → select this `extension/` folder. The manifest
     pins a stable ID, so pairing works no matter where the folder lives.
   - **Firefox:** `about:debugging#/runtime/this-firefox` → *Load Temporary
     Add-on* → select `extension/manifest.json` (session-only; see below for
     the permanent install).

Open the toolbar popup: it should say **connected**. If it says *not paired*,
re-run step 1 and reload the extension. If it says *app not running*, that's
fine - handed-off URLs queue in the database and the app picks them up the
moment it starts.

### How permanent is this?

- **Chrome / Edge / Brave:** an unpacked extension **survives restarts** -
  it stays installed until you remove it. The only cost is the "developer
  mode" reminder on the extensions page. Fully permanent, banner-free
  installs come with Chrome Web Store publication (kit in
  [docs/store-listing.md](../docs/store-listing.md)).
- **Firefox:** temporary add-ons are wiped on every restart - that's a
  Firefox rule for unsigned extensions, not something GrabLine can change.
  The fix is free and doesn't require a public listing: upload
  `dist/grabline-connect-firefox-*.zip` to addons.mozilla.org as
  **unlisted** (Submit New Add-on → "On your own"), and AMO's automated
  review signs it within minutes. The signed `.xpi` it gives back installs
  permanently in normal Firefox (drag it onto the browser). Re-sign each
  new version the same way.

## How a URL travels

```
right-click / hover button / popup → background.js → Native Messaging host
    → handoffs table (SQLite) → desktop app polls → resolver
    → Download Info dialog → the queue
```

## Site modules

A site module is a ~25-line matcher: hovered element in, `{anchor, url}`
out. The shared machinery - the shadow-root button, the show dwell, the
"keep the button alive while an inline preview player covers the thumbnail"
logic - lives once in `content/sites/button.js`.

- **youtube.js** - hover the button on video thumbnails (home, search, channels,
  Shorts shelf) *and on the player itself* on watch/Shorts pages; both hand
  the watch URL to the app, which opens its Download Info dialog. Also covers
  **YouTube Music** (song links and the bottom player bar - MP3 is one hover
  and one click away). The generic overlay stands back on YouTube entirely.
- **vimeo.js** - hover the button on links to `vimeo.com/<id>` videos (also
  player.vimeo.com embeds via `all_frames`).
- **x.js** - hover the button on videos inside tweets; sends the tweet's
  permalink (timeline videos are blob-backed, so the permalink is the only
  URL worth handing over).
- **soundcloud.js** - hover the button on the bottom play bar (whatever is
  playing now) and on track titles in lists; sends the track's permalink,
  never the browse page you happen to be on.
- **reddit.js** - hover the button on shreddit-player / post media (including
  Shadow DOM video and YouTube link-post thumbnails); sends the post
  permalink so yt-dlp can resolve the real file.
- **dailymotion.js** - hover on `/video/<id>` links and inside the
  geo.dailymotion.com player iframe (`all_frames`); sends the canonical
  watch URL.

The generic overlay also runs in every frame (`all_frames`) and walks open
Shadow DOM so HTML5 players that hide `<video>` in web components still get
a button on sites without a dedicated module.

Every selector lives at the top of its module; when a site's DOM churns,
that one file is the whole blast radius, and right-click + paste keep
working regardless.

## Tests

The pure logic (byte formatting, stream naming, button positioning) is covered
by Deno tests that load each `content/lib/*.js` module in isolation:

```
deno test --allow-read extension/test/
```

Anything touching the DOM, `chrome.*`/`browser.*`, or the MV3 lifecycle still
needs a manual pass: load the unpacked extension in **both** Firefox and Chrome
and exercise the sniffer, hover button (generic + site modules), the app's
Download Info dialog, context menus, interception, gallery/links/selection grab,
progress pill, and popup.

## Publishing to the stores

`python scripts/package_extension.py` builds store-ready zips for Chrome
and Firefox (each store wants a slightly different manifest); the listing
text, permission justifications, and reviewer notes are prewritten in
[docs/store-listing.md](../docs/store-listing.md).

## How the progress pill works

The background script keeps a persistent Native Messaging port open while the
downloads it started are running, asks the host for status once a second, and
the host answers from the app's SQLite jobs table. The open port also keeps
the MV3 service worker alive for exactly as long as there is something to
report.
