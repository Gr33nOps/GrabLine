// GrabLine Connect - background (MV3 service worker / Firefox event page).
//
// Deliberately thin and stateless: detect, decorate, deliver. Every download
// happens in the desktop app; this file only relays URLs over Native
// Messaging and keeps a small per-tab list of sniffed media in session
// storage (the service worker can die at any time - nothing lives here).

const api = globalThis.browser ?? globalThis.chrome;
const HOST_NAME = "dev.grabline.host";
const MENU_ID = "grabline-download";
const GALLERY_MENU_ID = "grabline-gallery";
const LINKS_MENU_ID = "grabline-links";
const SELECTION_MENU_ID = "grabline-selection";
const MAX_ITEMS_PER_TAB = 12;

// ---------------------------------------------------------------- native

// The cookies a request to `url` would carry, as a Cookie header. Lets the app
// fetch a login-gated file that the browser could reach.
async function cookieHeaderFor(url) {
  try {
    const cookies = await api.cookies.getAll({ url });
    return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch {
    return "";
  }
}

async function sendToGrabLine(
  url,
  tab,
  {
    quality = null,
    fallbackUrls = [],
    credentials = false,
    title = null,
    onlyIfRunning = false,
  } = {},
) {
  const message = {
    type: "download",
    url,
    pageUrl: tab?.url ?? null,
    pageTitle: title ?? tab?.title ?? null,
    source: "extension",
    quality,
    fallbackUrls,
    referer: tab?.url ?? null,
    userAgent: navigator.userAgent,
    // Set when we're about to cancel a browser download: the host must not
    // record a handoff it cannot deliver, or the file arrives twice.
    onlyIfRunning,
    // Cookies only for file downloads (interception / right-click a link),
    // never for media grabs - yt-dlp handles logins its own way there.
    cookie: credentials ? await cookieHeaderFor(url) : "",
  };
  try {
    const reply = await api.runtime.sendNativeMessage(HOST_NAME, message);
    await api.storage.session.set({ lastNativeError: null });
    if (typeof reply?.appRunning === "boolean") noteAppRunning(reply.appRunning);
    if (reply?.type === "queued" && tab?.id != null) track(url, tab.id);
    return reply ?? { type: "error", message: "empty reply from host" };
  } catch (error) {
    const detail = error?.message ?? String(error);
    await api.storage.session.set({ lastNativeError: detail });
    return { type: "error", message: detail, notPaired: true };
  }
}

async function pingGrabLine() {
  try {
    const reply = await api.runtime.sendNativeMessage(HOST_NAME, { type: "ping" });
    await api.storage.session.set({ lastNativeError: null });
    if (typeof reply?.appRunning === "boolean") noteAppRunning(reply.appRunning);
    return reply;
  } catch (error) {
    const detail = error?.message ?? String(error);
    await api.storage.session.set({ lastNativeError: detail });
    return null;
  }
}

// ------------------------------------------------ progress tracking (F1.3)
// Every URL grabbed from a tab is polled over a persistent native-messaging
// port (the host answers straight from the jobs table) and the progress is
// forwarded to that tab's content script, which renders the pill. The open
// port keeps the service worker alive while downloads run; the tracked map
// is mirrored to storage.session so a worker restart picks it back up.

const TRACK_LIMIT = 20;
const TRACK_TTL_MS = 30 * 60 * 1000;
const FINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
const tracked = new Map(); // url -> { tabId, addedAt }
let pollTimer = null;
let statusPort = null;

api.storage.session.get("trackedDownloads").then(({ trackedDownloads }) => {
  for (const [url, info] of trackedDownloads ?? []) tracked.set(url, info);
  if (tracked.size) schedulePoll();
});

function saveTracked() {
  void api.storage.session.set({ trackedDownloads: [...tracked.entries()] });
}

function track(url, tabId) {
  if (tracked.size >= TRACK_LIMIT && !tracked.has(url)) return;
  tracked.set(url, { tabId, addedAt: Date.now() });
  saveTracked();
  schedulePoll();
}

function schedulePoll() {
  if (pollTimer == null && tracked.size) pollTimer = setTimeout(pollStatus, 1000);
}

function statusPortFor() {
  if (!statusPort) {
    statusPort = api.runtime.connectNative(HOST_NAME);
    statusPort.onMessage.addListener(onStatusReply);
    statusPort.onDisconnect.addListener(() => {
      statusPort = null;
    });
  }
  return statusPort;
}

function stopPolling() {
  if (statusPort) {
    statusPort.disconnect();
    statusPort = null;
  }
}

function pollStatus() {
  pollTimer = null;
  const now = Date.now();
  for (const [url, info] of tracked) {
    if (now - info.addedAt > TRACK_TTL_MS) tracked.delete(url);
  }
  if (!tracked.size) {
    saveTracked();
    stopPolling();
    return;
  }
  try {
    statusPortFor().postMessage({ type: "status", urls: [...tracked.keys()] });
  } catch {
    statusPort = null;
    tracked.clear();
    saveTracked();
    return;
  }
  schedulePoll();
}

function onStatusReply(reply) {
  if (reply?.type !== "status") return;
  const byTab = new Map();
  let changed = false;
  for (const job of reply.jobs ?? []) {
    const info = tracked.get(job.url);
    if (!info) continue;
    if (FINAL_STATUSES.has(job.status)) {
      tracked.delete(job.url); // the final state still reaches the pill below
      changed = true;
    }
    const list = byTab.get(info.tabId) ?? [];
    list.push(job);
    byTab.set(info.tabId, list);
  }
  for (const [tabId, items] of byTab) {
    api.tabs.sendMessage(tabId, { cmd: "progress", items }).catch(() => {});
  }
  if (changed) saveTracked();
  if (!tracked.size) stopPolling();
}

// ----------------------------------------------------- context menu (F1.6)

function registerMenus() {
  // removeAll first so re-registration never trips "duplicate id" errors.
  api.contextMenus.removeAll(() => {
    void api.runtime.lastError; // ignore - removeAll on empty is fine
    api.contextMenus.create({
      id: MENU_ID,
      title: "Download with GrabLine",
      contexts: ["link", "image", "video", "audio", "page", "selection"],
    });
    api.contextMenus.create({
      id: GALLERY_MENU_ID,
      title: "Download all images with GrabLine",
      contexts: ["page", "image"],
    });
    api.contextMenus.create({
      id: LINKS_MENU_ID,
      title: "Download all links with GrabLine",
      contexts: ["page"],
    });
    // Highlight part of a page, right-click: every link, image, and playing
    // media inside the selection goes to the app's checkable picker.
    api.contextMenus.create({
      id: SELECTION_MENU_ID,
      title: "Download selected links & media with GrabLine",
      contexts: ["selection"],
    });
  });
}

// MV3 backgrounds restart often, and Firefox event pages drop menus with
// them - registering only on onInstalled made "Download with GrabLine"
// vanish until a reinstall. Register on install, on browser startup, AND on
// every background evaluation.
api.runtime.onInstalled.addListener(registerMenus);
api.runtime.onStartup.addListener(registerMenus);
registerMenus();

// -------------------------------------------- collect images / links grab

async function sendCollection(tab, collectCmd, hostType) {
  if (!tab?.id) return;
  let reply = null;
  try {
    reply = await api.tabs.sendMessage(tab.id, { cmd: collectCmd });
  } catch {
    return; // no content script on this page (browser UI, store pages …)
  }
  const urls = reply?.urls ?? [];
  if (!urls.length) return;
  try {
    await api.runtime.sendNativeMessage(HOST_NAME, {
      type: hostType,
      urls,
      pageUrl: tab.url ?? null,
      pageTitle: tab.title ?? null,
    });
    await api.storage.session.set({ lastNativeError: null });
  } catch (error) {
    await api.storage.session.set({ lastNativeError: error?.message ?? String(error) });
  }
}

api.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === GALLERY_MENU_ID) {
    await sendCollection(tab, "collectImages", "gallery");
    return;
  }
  if (info.menuItemId === LINKS_MENU_ID) {
    await sendCollection(tab, "collectLinks", "links");
    return;
  }
  if (info.menuItemId === SELECTION_MENU_ID) {
    await sendCollection(tab, "collectSelection", "links");
    return;
  }
  if (info.menuItemId !== MENU_ID) return;
  const selected = (info.selectionText ?? "").trim();
  const url =
    info.linkUrl ??
    info.srcUrl ??
    (/^https?:\/\/\S+$/.test(selected) ? selected : null) ??
    info.pageUrl;
  // A right-clicked link may be a login-gated file, so pass cookies along.
  if (url) await sendToGrabLine(url, tab, { credentials: Boolean(info.linkUrl) });
});

// ------------------------------------------------- network sniffer (F1.4)
// Observe-only webRequest (MV3 removed blocking; we never wanted it).

const MEDIA_CONTENT_TYPES =
  /^(video\/|audio\/|application\/(vnd\.apple\.mpegurl|x-mpegurl|dash\+xml))/i;
const MEDIA_URL_PATTERN = /\.(m3u8|mpd|mp4|webm|mkv|mp3|m4a|flac|ogg|opus|wav|mov)(\?|$)/i;
// Segment fetches (one every few seconds) would flood the list; the
// manifest is the useful thing to grab, so segments are skipped.
const SEGMENT_PATTERN = /\.(ts|m4s|aac)(\?|$)|(^video\/mp2t$)/i;

function headerValue(headers, name) {
  const found = (headers ?? []).find((h) => h.name.toLowerCase() === name);
  return found?.value ?? null;
}

function classify(details) {
  const contentType = (headerValue(details.responseHeaders, "content-type") ?? "").split(";")[0];
  const url = details.url;
  if (SEGMENT_PATTERN.test(url) || SEGMENT_PATTERN.test(contentType)) return null;
  const isManifest = /mpegurl|dash\+xml/i.test(contentType) || /\.(m3u8|mpd)(\?|$)/i.test(url);
  if (isManifest) return { kind: "stream" };
  if (MEDIA_CONTENT_TYPES.test(contentType) || MEDIA_URL_PATTERN.test(url)) {
    const length = Number(headerValue(details.responseHeaders, "content-length"));
    return { kind: contentType.startsWith("audio/") ? "audio" : "video", size: length || null };
  }
  return null;
}

async function recordMedia(tabId, item) {
  // Name the media by what the tab is showing right now - the URL leaf of a
  // stream is usually a meaningless hash, but the tab title is the video.
  try {
    const tab = await api.tabs.get(tabId);
    item.title = tab?.title || null;
  } catch {
    item.title = null;
  }
  const key = `tab:${tabId}`;
  const stored = await api.storage.session.get(key);
  // Dedupe by URL but move it back to the top with a fresh timestamp: media
  // that's still being fetched (the reel you're watching, the stream that's
  // playing) stays current, while things you scrolled past sink and fall off
  // the small cap - so the list reflects what's playing now, not a history.
  const items = (stored[key] ?? []).filter((existing) => existing.url !== item.url);
  items.unshift(item);
  const trimmed = items.slice(0, MAX_ITEMS_PER_TAB);
  await api.storage.session.set({ [key]: trimmed });
  updateBadge(tabId, trimmed.length);
}

function updateBadge(tabId, count) {
  api.action.setBadgeText({ tabId, text: count ? String(count) : "" });
  api.action.setBadgeBackgroundColor({ tabId, color: "#0170fd" });
}

api.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const media = classify(details);
    if (!media) return;
    void recordMedia(details.tabId, {
      url: details.url,
      kind: media.kind,
      size: media.size ?? null,
      seenAt: Date.now(),
    });
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders"],
);

api.tabs.onRemoved.addListener((tabId) => {
  void api.storage.session.remove(`tab:${tabId}`);
});

// Navigating a tab to a new page starts a fresh list.
api.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === "loading" && changeInfo.url) {
    void api.storage.session.remove(`tab:${tabId}`);
    updateBadge(tabId, 0);
  }
});

// --------------------------------------------------- interception (F1.5)
// On by default (toggle lives in the popup), but only fires when the app is
// running - see the listener below. chrome.downloads based: the download is
// cancelled the moment it starts and the app re-requests it.

function shouldIntercept(item) {
  // Take over every real download regardless of type (exe, torrent, image,
  // video, any extension) - true IDM behavior. The only ones we must leave
  // alone are URLs the app can't re-fetch: blob:/data:/filesystem: downloads
  // are generated in the page and exist only inside the browser.
  const url = item.finalUrl || item.url || "";
  return /^https?:/i.test(url);
}

// While we're taking downloads over, hide Chromium's download shelf/bubble so
// the intercepted download doesn't flash in the browser UI before GrabLine
// picks it up. Feature-detected: needs the downloads.ui/downloads.shelf
// permissions (present in the Chrome store build; a no-op on Firefox, which
// has neither API - a momentary flash there is unavoidable with the
// downloads-API takeover). Re-shown whenever interception is off or the app
// isn't running, so native browser downloads stay visible.
let lastAppRunning = false;

// null until storage answers. An MV3 service worker is restarted constantly,
// and each restart re-runs this file, so assuming "on" during that gap made a
// user who had switched takeover OFF still lose their first download to it.
let interceptEnabled = null;
const interceptLoaded = api.storage.local.get("intercept").then(({ intercept = true }) => {
  if (interceptEnabled === null) interceptEnabled = intercept;
  void updateDownloadUi(); // the shelf was left visible while this was unknown
  return interceptEnabled;
});

// The settled toggle. Callers that can afford to wait (all of them: the
// download is paused or the response is held) get the real value.
async function interceptionOn() {
  return interceptEnabled === null ? interceptLoaded : interceptEnabled;
}

async function updateDownloadUi() {
  const visible = !(interceptEnabled === true && lastAppRunning);
  try {
    if (api.downloads.setUiOptions) await api.downloads.setUiOptions({ enabled: visible });
    else if (api.downloads.setShelfEnabled) api.downloads.setShelfEnabled(visible);
  } catch {
    /* permission not granted in this build - keep the browser UI as is */
  }
}

function noteAppRunning(running) {
  if (running !== lastAppRunning) {
    lastAppRunning = running;
    void updateDownloadUi();
  }
}

// Keep lastAppRunning fresh so the browser's download shelf is already hidden
// (or shown) by the time a download starts, rather than flashing first. The
// takeover decision itself no longer reads it - it waits for a real answer.
function warmAppStatus() {
  void pingGrabLine();
}
api.runtime.onStartup.addListener(warmAppStatus);
api.runtime.onInstalled.addListener(warmAppStatus);
warmAppStatus();
if (api.alarms) {
  try {
    void api.alarms.create("grabline-ping", { periodInMinutes: 1 });
    api.alarms.onAlarm.addListener((alarm) => {
      if (alarm.name === "grabline-ping") warmAppStatus();
    });
  } catch {
    /* alarms permission optional - cold starts still ping above */
  }
}

api.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.intercept) {
    interceptEnabled = changes.intercept.newValue ?? true;
    void updateDownloadUi();
  }
});

async function handoffDownloadItem(item) {
  const url = item.finalUrl || item.url;
  const [active] = await api.tabs.query({ active: true, lastFocusedWindow: true });
  const chosenName = (item.filename || "").split(/[\\/]/).pop() || null;
  return sendToGrabLine(
    url,
    { url: item.referrer || active?.url || null, title: active?.title || chosenName },
    { credentials: true, onlyIfRunning: true },
  );
}

api.downloads.onCreated.addListener((item) => {
  // Pause synchronously (it is reversible), decide asynchronously, and only
  // then take the download away. Two orderings were tried and both lost a
  // file: awaiting storage/a ping before cancelling let small downloads finish
  // in the browser, and cancelling before confirming the handoff meant a
  // closed app made the download vanish from both places. Pausing first can do
  // neither - a pause that lost the race throws, and anything short of the app
  // accepting the URL resumes the browser's own download.
  if (interceptEnabled === false || !shouldIntercept(item)) return;
  void (async () => {
    try {
      await api.downloads.pause(item.id);
    } catch {
      return; // already finished; leave it to the browser
    }
    const reply = (await interceptionOn()) ? await handoffDownloadItem(item) : null;
    if (reply?.type !== "queued" || !reply.appRunning) {
      try {
        await api.downloads.resume(item.id);
      } catch {
        /* the browser may have finished it during the pause - fine */
      }
      return;
    }
    try {
      await api.downloads.cancel(item.id);
      await api.downloads.erase({ id: item.id });
    } catch {
      /* the browser finished it anyway; GrabLine has it too - harmless */
    }
  })();
});

// ----------------------------------- proactive interception (Firefox, F1.5b)
// downloads.onCreated (above) fires only after the browser has already
// committed to the download, so the item flashes in the download panel before
// we cancel it - the reported "it shows in the browser for a millisecond" bug.
// Blocking webRequest cancels the response at the network layer, before any
// download item exists, so nothing ever appears in the browser. Firefox
// honours blocking webRequest in MV3; Chrome does not (policy-only), so there
// this listener simply isn't installed and the onCreated path (with the shelf
// hidden via setUiOptions) stands.

// Content-types with no inline viewer: the browser always downloads these, so
// taking them over matches its own decision. An allowlist by design - anything
// the browser renders itself (html, pdf, images, video, audio) must NEVER be
// here, or ordinary browsing breaks: a video/* match on this list once
// cancelled the fetches that in-page players stream through. Media the user
// actually wants saved still arrives here as Content-Disposition: attachment,
// or as a downloads.onCreated item handled above.
const DOWNLOAD_CONTENT_TYPES =
  /^application\/(?:octet-stream|zip|gzip|x-gzip|x-tar|x-rar-compressed|vnd\.rar|x-7z-compressed|x-bzip2|x-xz|x-msdownload|x-msdos-program|vnd\.microsoft\.portable-executable|x-apple-diskimage|x-iso9660-image|x-bittorrent|x-debian-package|vnd\.android\.package-archive)/i;

function isForcedDownload(details) {
  // The unambiguous server signal first: Content-Disposition: attachment always
  // means "download", and overrides any inline rendering.
  const disposition = (headerValue(details.responseHeaders, "content-disposition") ?? "")
    .trim()
    .toLowerCase();
  if (disposition.startsWith("attachment")) return true;
  // The MIME allowlist applies only to frame navigations. On xhr/fetch/object
  // traffic, octet-stream and friends are routine (APIs, wasm, range probes),
  // so anything short of an explicit attachment stays untouched there.
  if (details.type !== "main_frame" && details.type !== "sub_frame") return false;
  const type = (headerValue(details.responseHeaders, "content-type") ?? "")
    .split(";")[0]
    .trim()
    .toLowerCase();
  return DOWNLOAD_CONTENT_TYPES.test(type);
}

async function interceptResponse(details) {
  // Cancel only once the app has actually accepted the URL. The response is
  // held until this resolves, so there is no race to lose - and if the app is
  // closed (or the toggle turns out to be off) we simply let the browser have
  // its download instead of cancelling it into nowhere. One native message
  // does both the check and the handoff; forced downloads are rare, so this
  // never touches ordinary browsing.
  if (!(await interceptionOn())) return {};
  const [active] = await api.tabs
    .query({ active: true, lastFocusedWindow: true })
    .catch(() => []);
  const referrer = details.originUrl || details.documentUrl || active?.url || null;
  const reply = await sendToGrabLine(
    details.url,
    { url: referrer, title: active?.title || null },
    { credentials: true, onlyIfRunning: true },
  );
  if (reply?.type !== "queued" || !reply.appRunning) return {};
  return { cancel: true };
}

// Stays synchronous for the common case (returns {} at once), so navigation is
// never delayed; only an actual download awaits the handoff.
function onDownloadHeaders(details) {
  if (interceptEnabled === false || details.tabId < 0) return {};
  if (!isForcedDownload(details)) return {};
  return interceptResponse(details);
}

try {
  api.webRequest.onHeadersReceived.addListener(
    onDownloadHeaders,
    {
      urls: ["<all_urls>"],
      // Frames catch download navigations; xhr/other/object catch forced
      // attachment responses that never become a top-level document (those
      // types only ever act on Content-Disposition: attachment, see above).
      // Never listen on type "image" or "media" - that breaks inline photos
      // and in-page players.
      types: ["main_frame", "sub_frame", "object", "xmlhttprequest", "other"],
    },
    ["blocking", "responseHeaders"],
  );
} catch {
  // Blocking webRequest unavailable (Chrome MV3) - the onCreated path stands.
}

// ------------------------------------------------------------- messages

async function tabForMessage(sender, message) {
  if (sender.tab) return sender.tab;
  if (message.tabId == null) return null; // popup passes the active tab's id
  try {
    return await api.tabs.get(message.tabId);
  } catch {
    return null;
  }
}

// The streams/media the sniffer saw in a tab, best candidates first -
// attached as fallbacks when a blob-backed player forced a page-URL grab.
async function sniffedUrlsFor(tabId) {
  if (tabId == null) return [];
  const key = `tab:${tabId}`;
  const stored = await api.storage.session.get(key);
  const items = stored[key] ?? [];
  const streams = items.filter((item) => item.kind === "stream");
  const rest = items.filter((item) => item.kind !== "stream");
  return [...streams, ...rest].slice(0, 3).map((item) => item.url);
}

api.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.cmd === "grab") {
    (async () => {
      const tab = await tabForMessage(sender, message);
      const fallbackUrls = message.sniff ? await sniffedUrlsFor(tab?.id) : [];
      return sendToGrabLine(message.url, tab, {
        quality: message.quality ?? null,
        fallbackUrls,
        title: message.title ?? null,
        credentials: Boolean(message.credentials),
      });
    })().then(sendResponse);
    return true; // async response
  }
  if (message?.cmd === "ping") {
    pingGrabLine().then(sendResponse);
    return true;
  }
  if (message?.cmd === "recent") {
    askGrabLine({ type: "recent", limit: 5 }).then(sendResponse);
    return true;
  }
  if (message?.cmd === "focus") {
    askGrabLine({ type: "focus", target: message.target ?? null }).then(sendResponse);
    return true;
  }
  return false;
});

// A one-shot native request that never throws: returns the reply, or null if
// the host isn't reachable (older app, not paired). Callers degrade quietly.
async function askGrabLine(payload) {
  try {
    const reply = await api.runtime.sendNativeMessage(HOST_NAME, payload);
    if (typeof reply?.appRunning === "boolean") noteAppRunning(reply.appRunning);
    return reply ?? null;
  } catch {
    return null;
  }
}
