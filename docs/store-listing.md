# GrabLine Connect store listing kit

Reviewed against source version **1.32.15**, 14 November 2026. This is a submission
reference, not a claim that every store currently publishes that version. Verify
the live listing and current store requirements before each submission.

## Build and identity

```sh
python scripts/package_extension.py --out dist
```

The script emits Chrome/Firefox ZIPs with their respective manifests. They are
unminified and omit development tests/docs. Firefox signing and store publication
are separate operations. The source manifest's pinned unpacked Chromium key is
removed from store packages; verify the actual store-assigned identity against
`CHROME_EXTENSION_IDS` in `app/native_host/__init__.py` before shipping a desktop
host. Never relax the allow-list to accept arbitrary extensions.

| Field | Value |
| --- | --- |
| Name | GrabLine Connect |
| Homepage | https://grabline.github.io/GrabLine/ |
| Source / support | https://github.com/GrabLine/GrabLine |
| Privacy policy | https://github.com/GrabLine/GrabLine/blob/main/PRIVACY.md |
| Firefox identity | `grabline@grabline.dev` |
| Existing Firefox listing | https://addons.mozilla.org/en-US/firefox/addon/grabline-connect/ |

## Suggested listing text

**Short description:** Send links and detected media to the GrabLine desktop
download manager with hover buttons, context menus and a browser popup.

**Long description:** GrabLine Connect pairs your browser with the free,
AGPL-licensed GrabLine desktop app for Windows, macOS and Linux. Send a link,
media page or selected URLs; collect page images/links; inspect detected media;
and track app downloads from the browser. Quality selection, downloading and
conversion happen in the desktop app. Image hover is opt-in, and hover/takeover
preferences can be changed in the popup.

Download takeover is enabled by default. Chromium retains a paused browser
fallback while the app downloads; Firefox can intercept eligible responses
before a browser item exists. Generated blobs, POST exports and applicable
verification-session downloads stay in the browser. A running app is required
for automatic takeover. See the extension guide for recovery behaviour.

The desktop app is required. Communication with it uses Native Messaging;
GrabLine Connect does not require a GrabLine account, run ads or send usage
analytics. Media availability depends on the site and your access. GrabLine does
not bypass DRM or account restrictions.

## Permission justifications

Use the **generated target manifest**, not the cross-browser source manifest,
when completing store forms:

| Permission | Purpose |
| --- | --- |
| `nativeMessaging` | Send handoffs to the local app and read its status |
| `contextMenus` | Download and collection actions from genuine context menus |
| `storage` | Preferences, per-tab media and recovery state |
| `alarms` | Scheduled background/recovery work |
| `webRequest` and `<all_urls>` | Detect media responses and eligible handoff context |
| `webRequestBlocking`, Firefox only | Intercept eligible downloads before a browser item is created |
| `downloads` | Observe/manage browser downloads and takeover fallback |
| `downloads.shelf` / `downloads.ui`, Chromium only | Control download UI where the browser exposes it |
| `cookies` | Include matching session context when handing selected downloads to the app |
| `tabs` | Current page URL/title and per-tab media/actions |
| Content scripts on permitted sites | Hover buttons and page image/link collection |

Data sent through the host remains local at handoff; the desktop app then makes
the requested transfer/service connections. Disclose cookie/URL handling using
[PRIVACY.md](../PRIVACY.md), not an absolute “no data access” statement.

## Reviewer notes and final checks

Reviewers need the desktop app or a Python 3.12 source environment. Follow
[Contributing](../CONTRIBUTING.md), run `python -m app.native_host.install`, launch
the app and load the matching extension. An unpaired popup without that native
counterpart is expected. Packaging performs no source minification.

Before submission, verify actual permissions/IDs, pairing, takeover and browser
recovery, popup/media actions, and the published privacy URL. Capture current
screenshots at the store's requested dimensions and supply any required developer
account/listing fields. See [extension testing](../extension/README.md#verify-a-change)
and [release instructions](../RELEASING.md). Do not claim store approval or live-site
certification from package generation alone.
