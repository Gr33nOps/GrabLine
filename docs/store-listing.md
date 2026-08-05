# GrabLine Connect - store listing kit (F2.5)

Everything needed to submit the extension to the Chrome Web Store and
addons.mozilla.org. Build the upload zips first:

```bash
python scripts/package_extension.py     # → dist/grabline-connect-{chrome,firefox}-<version>.zip
```

## Identity

| | |
|---|---|
| Name | **GrabLine Connect** |
| Category | Productivity → Tools (CWS) / Download Management (AMO) |
| Homepage | https://github.com/Gr33nOps/GrabLine |
| Support | https://github.com/Gr33nOps/GrabLine/issues |
| Contributions URL (AMO) | https://ko-fi.com/zain021xd |
| Privacy policy URL | https://github.com/Gr33nOps/GrabLine/blob/main/PRIVACY.md |
| Firefox add-on ID | `grabline@grabline.dev` (pinned in the manifest) |

> The Chrome Web Store assigns the published extension a **new ID** (the
> store rejects manifests with a `key`). After first publication, add the
> store-assigned ID to `CHROME_EXTENSION_IDS` in `app/native_host/__init__.py`
> and re-run `python -m app.native_host.install`, so paired hosts accept it.

## Summary (short description)

> The connector for the GrabLine download manager: right-click download,
> hover buttons on media, quality picking for 1000+ sites - every download
> runs in the open-source desktop app, not the browser.

## Description (long)

> GrabLine Connect pairs your browser with **GrabLine**, the free and
> open-source download manager (AGPL-3.0). The extension is deliberately
> thin - it detects and delivers, the desktop app downloads:
>
> - **Right-click → "Download with GrabLine"** on any link, image, video, or page
> - **Hover button** on videos, audio, and large images (the button is the GrabLine logo)
> - **Per-tab media list**: every stream (.m3u8/.mpd) and media file the page loaded
> - **Download all images** on a page into a selection grid
> - **Download takeover** (on by default): browser downloads are cancelled and handed to GrabLine
>
> On sites the Smart Engine knows (YouTube and 1000+ more), the app opens a
> Download Info dialog to confirm the file and pick quality (4K to 144p, MP3/M4A),
> with subtitles and clip trimming available in the app. Everywhere else you get
> segmented, resumable multi-connection downloading.
>
> **Requires the free GrabLine desktop app** (Windows/macOS/Linux):
> https://github.com/Gr33nOps/GrabLine - the extension talks to it over
> Native Messaging only. No account, no cloud, no telemetry, no ads.
>
> GrabLine does not bypass DRM or logins.

## Permission justifications

| Permission | Why |
|---|---|
| `nativeMessaging` | The single purpose of the extension: hand URLs to the local GrabLine app. Nothing else can do this. |
| `contextMenus` | The "Download with GrabLine" / "Download all images" right-click entries. |
| `storage` | User toggles (per-site overlay off-switch, interception) and the per-tab sniffed-media list (`storage.session`, gone when the tab closes). |
| `webRequest` + `<all_urls>` | Read response headers to list a tab's media and streams in the popup, and to recognize a starting download for takeover. Nothing leaves the browser. |
| `webRequestBlocking` (Firefox) | Takeover cancels a forced download at the network layer so the desktop app can fetch it instead, with no flash of a browser download. Chrome ignores blocking `webRequest` in MV3, so there the `downloads` API does the takeover. On by default. |
| `downloads` | The takeover path on Chromium: cancel a starting browser download and re-dispatch it to the app. On by default. |
| `tabs` | Page URL/title accompany a handed-off URL so the app can name files sensibly. |
| Content scripts on `<all_urls>` | The hover button and the image collector must run where the media is. They render one button in a closed shadow root and send nothing anywhere except the local app. |

**Single purpose statement (CWS):** connect the browser to the locally
installed GrabLine download manager.

**Data use disclosures:** the extension collects nothing, transmits nothing
off-device, and has no remote code. All traffic is stdio to the local native
host. See PRIVACY.md.

## Reviewer notes (AMO "notes to reviewer" / CWS review field)

> This extension requires its native-host counterpart, the open-source
> GrabLine desktop app: https://github.com/Gr33nOps/GrabLine
> Install: `pip install -e .` then `python -m app.native_host.install`
> (registers the Native Messaging manifest), then load the extension.
> Without the app, the extension idles with a "not paired" popup status.
> The whole source is in the repo above; the zip is built unminified by
> `scripts/package_extension.py`.

## Assets still needed before submission

- [ ] 1-5 screenshots, 1280×800 (queue window + Download Info dialog + popup)
- [ ] CWS promo tile 440×280 (optional but recommended)
- [ ] Developer accounts: CWS one-time $5, AMO free
