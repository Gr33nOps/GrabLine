# GrabLine Connect

GrabLine's browser companion detects media, provides hover/context-menu actions
and hands URLs to the desktop app over Native Messaging. Media processing and
managed downloads run in GrabLine. Start with the
[installation guide](../docs/install.md#browser-extension) for normal use.

## Install and pair

**Firefox:** use the signed
[Mozilla Add-ons listing](https://addons.mozilla.org/en-US/firefox/addon/grabline-connect/).
For temporary development testing, select `extension/manifest.json` in Firefox's
temporary-add-on loader. A temporary add-on is removed when Firefox restarts.

**Chromium development:** open the browser's extension manager, enable Developer
mode and load this repository's `extension/` folder. The source manifest contains
a key for a stable unpacked identity. Store-targeted release ZIPs omit that key,
so unpacking a store ZIP is not equivalent to loading this source folder.

In the desktop app, use Settings → Browser Integration → **Pair browsers**.
For intentional source pairing, from the environment containing this checkout:

```sh
python -m app.native_host.install
```

Registration changes which local host this browser invokes. Pair the intended
source/installed copy, keep its path stable and reload the extension after edits.
The native host allow-list must include the extension's actual identity; do not
relax it to accept arbitrary extensions.

Open the popup to inspect status. **Connected** means the host sees the app;
**app not running** means the host answers but the GUI is closed; **not paired**
means native-host discovery/registration failed. Explicit download handoffs can
queue for next launch, while takeover requires a running app. Open GrabLine/Open
Settings focuses a running app; launch it first if it is closed.

## Actions and preferences

- Popup: Open GrabLine, Open Settings, Grab This Tab, paste/send a URL, detected
  media and recent downloads.
- Hover: video/audio buttons, opt-in image buttons, master/per-site switches,
  four corner positions and progress display.
- Context menu: send a link/image/media/page or selected URLs; collect images
  or links into the app's selection dialog before Download All.
- Media discovery: list response-derived files and HLS/DASH streams for the tab.
  Discovery is not a guarantee that a protected or expired stream will download.

## Download takeover

Takeover is on by default and can be disabled in the popup.

| Browser | Behaviour |
| --- | --- |
| Chromium | Pauses an eligible browser download, hands it to the running app and retains a browser fallback while the desktop transfer runs. Recovery follows the implemented job/browser state. |
| Firefox | Intercepts eligible attachment/binary responses before a browser download item is created; after acceptance, retries/failures remain in the desktop app. |

Browser-generated blobs, POST exports, assistant-generated files and supported
browser-verification exclusions remain in the browser. If the app is unavailable
or rejects an automatic handoff, the browser download remains responsible for
the response. Test recovery in the actual browser; a simulated API success is
not proof of a recovered file.

## How the bridge works

```text
page/popup/context menu
  → background.js
  → length-prefixed JSON over Native Messaging stdio
  → local host validation
  → SQLite handoffs
  → desktop resolver, Download Info and queue
```

There is no listening localhost server. The native host validates message size,
schemes, fields and header contents. Progress/recent state is read from the
app's database. While tracking a handoff, the background script keeps a native
port and polls for status; this is distinct from pointer-driven hover handling.
See [Privacy](../PRIVACY.md) for cookies and retained local request headers.

## Site modules

Shared button machinery lives in `content/sites/button.js`. Dedicated selectors
and canonical URL logic are in:

| Module | Target |
| --- | --- |
| `youtube.js` | Watch pages, thumbnails, Shorts and YouTube Music |
| `vimeo.js` | Video links and supported player embeds |
| `x.js` | Video tweet permalinks |
| `soundcloud.js` | Track titles and the current player |
| `instagram.js` | Supported post/reel permalinks |
| `reddit.js` | Post media, including supported open Shadow DOM |
| `dailymotion.js` | Video links and supported player frames |

The generic overlay handles HTML5 media, permitted frames and open Shadow DOM.
Browser-restricted pages cannot run normal content scripts. Site DOM changes,
accounts and region/bot checks remain external dependencies; controlled selector
fixtures do not certify live-site downloads.

## Verify a change

From the repository root:

```sh
deno test --allow-read extension/test
```

The Deno suite tests isolated JavaScript logic. Real browser/GUI cases live in
the [Windows QA harness](../scripts/windows_qa/README.md): popup actions,
preferences/restarts, collections, overlays, takeover, recovery and progress.
Use disposable profiles with a matching app/native-host database and fresh
fixture URLs. Inspect console/worker logs, jobs and final file hashes.

An actual toolbar popup and `chrome-extension://.../popup/popup.html` opened
as a tab are separate testing levels. Keep Firefox and Chromium results separate.
The [testing guide](../docs/testing.md) records release evidence and limits.

## Package and publish

```sh
python scripts/package_extension.py --out dist
```

The script creates Chrome and Firefox ZIPs with target-specific manifests,
removing the source identity key and unsupported browser-specific fields or
permissions. AMO signing/store submission is a separate operation; the Firefox
ZIP is not itself a permanently signed add-on. Before publishing, verify the
actual assigned extension ID against the desktop allow-list and test pairing.
See the [store listing kit](../docs/store-listing.md) and
[release process](../RELEASING.md).
