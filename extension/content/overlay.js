// GrabLine Connect - element sniffer + hover button (F1.2).
//
// One floating button, hosted in a closed shadow root so page CSS can't touch
// it. Shown when the pointer rests on a <video>, <audio>, or big-enough
// <img>; clicking hands the media URL (or the page URL for blob-backed
// players, which the Smart Engine usually understands) to the desktop app.

(() => {
  const api = globalThis.browser ?? globalThis.chrome;
  const MIN_IMAGE_SIZE = 200;
  const HIDE_DELAY_MS = 350;

  // The hover button's chrome - icons, logo, colours, positioning, feedback -
  // is shared with the site-module button in content/lib/button-kit.js so the
  // two can't drift. This file owns only when the generic button appears and
  // how it follows the media.
  const kit = globalThis.grablineButtonKit;
  const BUTTON_SIZE = 34;
  // Hosts where a site module (content/sites/*.js) owns the media UI. On
  // those hosts the generic overlay stands back: browse pages run inline
  // preview <video>s whose blob src would fall back to the *page* URL (= the
  // feed, not the video), and site thumbnails already have their own button.
  // videos: the generic overlay only decorates <video> on matching paths
  // (the real player page). images: skipped entirely when true.
  const SITE_RULES = [
    {
      hosts: /(^|\.)youtube\.com$/,
      videos: /^$/, // never - youtube.js owns thumbnails AND the player button
      images: true,
    },
    {
      hosts: /(^|\.)(x|twitter)\.com$/,
      videos: /^$/, // never - the x.js module handles every tweet video
      images: false,
    },
    {
      hosts: /(^|\.)reddit\.com$/,
      videos: /^$/, // never - reddit.js hands the post permalink (not blob/gif)
      images: true,
    },
    {
      hosts: /(^|\.)(geo\.)?dailymotion\.com$/,
      videos: /^$/, // never - dailymotion.js hands the /video/<id> URL
      images: true,
    },
  ];

  function siteRule() {
    return SITE_RULES.find((rule) => rule.hosts.test(location.hostname)) ?? null;
  }

  let enabled = true;
  // Master switch (popup): turn hover buttons off everywhere. Right-click and
  // the toolbar popup still work.
  let hoverGlobal = true;
  // Images are opt-in (popup toggle): a button on every profile picture and chat
  // thumbnail is noise, and right-click + the gallery grabber cover images.
  let imagesEnabled = false;
  let currentTarget = null;
  let hideTimer = 0;

  // Which corner of the hovered element the button sits in (popup setting).
  let corner = "top-right";
  // The on-page progress pill is off by default - the app window and tray
  // already show progress. Opt in from the popup.
  let pagePillEnabled = false;

  api.storage.local
    .get(["disabledSites", "overlayImages", "buttonCorner", "hoverButtons", "pagePill"])
    .then(
      ({
        disabledSites = [],
        overlayImages = false,
        buttonCorner = "top-right",
        hoverButtons = true,
        pagePill = false,
      }) => {
        if (disabledSites.includes(location.hostname)) enabled = false;
        imagesEnabled = overlayImages;
        corner = buttonCorner;
        hoverGlobal = hoverButtons;
        pagePillEnabled = pagePill;
      },
    );
  api.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes.disabledSites) {
      enabled = !(changes.disabledSites.newValue ?? []).includes(location.hostname);
      if (!enabled) hideButton();
    }
    if (changes.hoverButtons) {
      hoverGlobal = changes.hoverButtons.newValue !== false;
      if (!hoverGlobal) hideButton();
    }
    if (changes.overlayImages) {
      imagesEnabled = Boolean(changes.overlayImages.newValue);
      if (!imagesEnabled) hideButton();
    }
    if (changes.buttonCorner) {
      corner = changes.buttonCorner.newValue ?? "top-right";
    }
    if (changes.pagePill) {
      pagePillEnabled = Boolean(changes.pagePill.newValue);
      if (!pagePillEnabled) {
        for (const url of [...pillRows.keys()]) dropPill(url, 0);
      }
    }
  });

  // ------------------------------------------------------------- button

  const { host, button } = kit.createButton(BUTTON_SIZE);

  function attachHost() {
    // Attach to <html>, not <body>. A `position: fixed` element is positioned
    // relative to the nearest ancestor with a transform/filter/perspective, not
    // the viewport - and many sites transform <body> (page transitions, zoom,
    // reels feeds), which threw the button to wrong, "weird angle" positions.
    // <html> is far less likely to be transformed, so fixed means fixed.
    const root = document.documentElement;
    if (root && !host.isConnected) root.appendChild(host);
  }

  function mediaUrlFor(element) {
    let src = null;
    if (element instanceof HTMLImageElement) src = element.currentSrc || element.src;
    else if (element instanceof HTMLMediaElement) {
      src = element.currentSrc || element.src;
      if (!src) src = element.querySelector("source")?.src ?? null;
    }
    // blob:/data: sources can't be fetched outside the page. Send the page
    // URL (the Smart Engine may know the site) and flag it so the background
    // attaches the streams the sniffer saw in this tab as fallbacks - on
    // no-name streaming sites the sniffed .m3u8 IS the movie.
    if (!src || !/^https?:/.test(src)) return { url: location.href, fromPage: true };
    return { url: src, fromPage: false };
  }

  function eligible(element) {
    const rule = siteRule();
    if (element instanceof HTMLMediaElement) {
      // A video with no source at all and no buffered data is a placeholder or
      // an ad slot that isn't playing anything grabbable - showing the button
      // there is the "appears on media but says not downloadable" complaint.
      // A real player (including blob/MSE streaming) has a currentSrc or has
      // loaded data (readyState >= HAVE_CURRENT_DATA), so this keeps those.
      const hasMedia = Boolean(element.currentSrc || element.src) || element.readyState >= 2;
      if (!hasMedia) return false;
      if (rule) return rule.videos.test(location.pathname);
      return true;
    }
    if (element instanceof HTMLImageElement) {
      if (!imagesEnabled || rule?.images) return false;
      return (
        element.naturalWidth >= MIN_IMAGE_SIZE && element.naturalHeight >= MIN_IMAGE_SIZE
      );
    }
    return false;
  }

  function placeButton(rect) {
    const at = kit.placeInCorner(rect, BUTTON_SIZE, corner, {
      width: window.innerWidth,
      height: window.innerHeight,
    });
    button.style.left = `${at.left}px`;
    button.style.top = `${at.top}px`;
  }

  function showButtonFor(element) {
    attachHost();
    const rect = element.getBoundingClientRect();
    if (rect.width < 40 || rect.height < 40) return;
    currentTarget = element;
    placeButton(rect);
    button.style.display = "flex";
    kit.resetButton(button);
    startFollowing();
  }

  function hideButton() {
    clearTimeout(hideTimer);
    stopFollowing();
    button.style.display = "none";
    currentTarget = null;
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideButton, HIDE_DELAY_MS);
  }

  // Keep the button glued to its media while shown. Native scrolling fires
  // "scroll", but reels/shorts feeds move with CSS transforms that don't - so
  // we re-read the target's box every frame. The loop stops the instant the
  // button hides, so it only runs during an actual hover.
  let followId = 0;

  function reposition() {
    if (!currentTarget) return;
    if (!currentTarget.isConnected) return hideButton();
    const rect = currentTarget.getBoundingClientRect();
    const offscreen =
      rect.bottom < 0 ||
      rect.top > window.innerHeight ||
      rect.right < 0 ||
      rect.left > window.innerWidth;
    if (offscreen || rect.width < 40 || rect.height < 40) return hideButton();
    placeButton(rect);
  }

  function startFollowing() {
    if (!followId) followId = requestAnimationFrame(followFrame);
  }

  function stopFollowing() {
    if (followId) cancelAnimationFrame(followId);
    followId = 0;
  }

  function followFrame() {
    followId = 0;
    if (button.style.display === "none") return;
    reposition();
    if (button.style.display !== "none") followId = requestAnimationFrame(followFrame);
  }

  // ------------------------------------------------- gallery grab (F2.2)
  // The background script asks for every big-enough image on the page; a
  // wrapping <a> that links straight to an image wins over the (often
  // thumbnail-sized) <img> src.

  const IMAGE_HREF = /\.(jpe?g|png|gif|webp|avif|bmp)(\?|$)/i;
  const MAX_GALLERY_ITEMS = 200;

  function collectImages() {
    const urls = [];
    const seen = new Set();
    for (const img of document.images) {
      if (urls.length >= MAX_GALLERY_ITEMS) break;
      const src = img.currentSrc || img.src;
      if (!src || !/^https?:/.test(src)) continue;
      if (img.naturalWidth < MIN_IMAGE_SIZE && img.naturalHeight < MIN_IMAGE_SIZE) continue;
      let url = src;
      const href = img.closest("a")?.getAttribute("href");
      if (href) {
        try {
          const full = new URL(href, location.href).toString();
          if (IMAGE_HREF.test(full)) url = full;
        } catch {
          /* unparsable href - keep the img src */
        }
      }
      if (seen.has(url)) continue;
      seen.add(url);
      urls.push(url);
    }
    return urls;
  }

  // ------------------------------------------------ progress pill (F1.3)
  // The background script polls the app (over Native Messaging) for every
  // download grabbed from this tab and forwards updates here. One stack of
  // pills, bottom-right; finished downloads linger briefly, then fade.

  const pillHost = document.createElement("div");
  const pillShadow = pillHost.attachShadow({ mode: "closed" });
  const pillStack = document.createElement("div");
  pillStack.style.cssText = [
    "position: fixed",
    "right: 16px",
    "bottom: 16px",
    "z-index: 2147483647",
    "display: none",
    "flex-direction: column",
    "align-items: flex-end",
    "gap: 6px",
  ].join(";");
  pillShadow.appendChild(pillStack);
  const pillRows = new Map(); // url -> { row, timer }
  const { humanBytes } = globalThis.grablineFormat;

  function pillRowFor(url) {
    let entry = pillRows.get(url);
    if (!entry) {
      const row = document.createElement("div");
      row.style.cssText = [
        "max-width: 340px",
        "padding: 8px 14px",
        "border-radius: 8px",
        "background: rgba(31,34,40,.96)",
        "color: #fff",
        "font: 500 12px/1.3 system-ui, sans-serif",
        "box-shadow: 0 2px 8px rgba(0,0,0,.35)",
        "overflow: hidden",
        "text-overflow: ellipsis",
        "white-space: nowrap",
      ].join(";");
      pillStack.appendChild(row);
      entry = { row, timer: 0 };
      pillRows.set(url, entry);
    }
    return entry;
  }

  function dropPill(url, delayMs) {
    const entry = pillRows.get(url);
    if (!entry) return;
    clearTimeout(entry.timer);
    entry.timer = setTimeout(() => {
      entry.row.remove();
      pillRows.delete(url);
      if (!pillRows.size) pillStack.style.display = "none";
    }, delayMs);
  }

  function pillText(job) {
    const name = job.name ?? "download";
    if (job.status === "completed") return `Done · ${name}`;
    if (job.status === "failed") return `Failed · ${name}`;
    if (job.total && job.downloaded != null) {
      return `${Math.min(100, Math.round((job.downloaded / job.total) * 100))}% · ${name}`;
    }
    if (job.downloaded) return `${humanBytes(job.downloaded)} · ${name}`;
    return `Starting · ${name}`;
  }

  function renderProgress(items) {
    if (!pagePillEnabled) return; // opt-in; the app + tray already show progress
    if (document.body && !pillHost.isConnected) document.body.appendChild(pillHost);
    for (const job of items) {
      if (job.status === "cancelled") {
        dropPill(job.url, 0);
        continue;
      }
      const entry = pillRowFor(job.url);
      entry.row.textContent = pillText(job);
      entry.row.title = job.url;
      if (job.status === "completed") dropPill(job.url, 5000);
      if (job.status === "failed") dropPill(job.url, 8000);
    }
    if (pillRows.size) pillStack.style.display = "flex";
  }

  // Every http(s) link on the page, most-downloadable first. The desktop
  // app's picker filters and routes them; here we just gather and dedupe.
  const MAX_LINKS = 300;
  const FILE_LINK =
    /\.(mp4|mkv|webm|mov|avi|m4v|mp3|m4a|flac|wav|ogg|opus|aac|zip|rar|7z|tar|gz|xz|iso|pdf|docx?|xlsx?|pptx?|epub|apk|exe|dmg|appimage|deb|rpm|jpe?g|png|gif|webp|svg)(\?|$)/i;

  function collectLinks() {
    const withExt = [];
    const rest = [];
    const seen = new Set();
    for (const anchor of document.links) {
      if (seen.size >= MAX_LINKS) break;
      const href = anchor.href;
      if (!href || !/^https?:/.test(href) || seen.has(href)) continue;
      seen.add(href);
      (FILE_LINK.test(href) ? withExt : rest).push(href);
    }
    // File-looking links first so the picker's default selection is useful.
    return [...withExt, ...rest].slice(0, MAX_LINKS);
  }

  // Everything downloadable inside the user's text selection: links, images,
  // and playing media that the highlighted region touches. The app's picker
  // then filters by type, so one gesture covers "download the selected
  // links / images / videos / documents".
  function collectSelection() {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) return [];
    const urls = [];
    const seen = new Set();
    const push = (url) => {
      if (url && /^https?:/.test(url) && !seen.has(url) && urls.length < MAX_LINKS) {
        seen.add(url);
        urls.push(url);
      }
    };
    for (const anchor of document.links) {
      if (selection.containsNode(anchor, true)) push(anchor.href);
    }
    for (const img of document.images) {
      if (selection.containsNode(img, true)) push(img.currentSrc || img.src);
    }
    for (const media of document.querySelectorAll("video, audio")) {
      if (!selection.containsNode(media, true)) continue;
      push(media.currentSrc || media.src || media.querySelector("source")?.src);
    }
    return urls;
  }

  api.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.cmd === "collectImages") {
      sendResponse({ urls: collectImages() });
    } else if (message?.cmd === "collectLinks") {
      sendResponse({ urls: collectLinks() });
    } else if (message?.cmd === "collectSelection") {
      sendResponse({ urls: collectSelection() });
    } else if (message?.cmd === "progress" && Array.isArray(message.items)) {
      renderProgress(message.items);
    }
    return false;
  });

  // Find a <video>/<audio> under the pointer even when the site paints its own
  // controls or a click-catching layer on top (reels, shorts, live players):
  // elementsFromPoint returns the whole stack at that spot, including elements
  // sitting *behind* others. That's what makes the button appear on media the
  // page covers, which plain event.target matching misses.
  //
  // Also walk open Shadow DOM: Reddit's shreddit-player (and similar hosts)
  // keep the real <video> inside a shadow root, and elementsFromPoint only
  // returns the host - without this the hover button never appears there.
  function mediaUnderPointer(x, y) {
    for (const el of document.elementsFromPoint(x, y)) {
      if (el === host) continue;
      if (el instanceof HTMLMediaElement) return el;
      if (el.shadowRoot) {
        const nested = kit.mediaInOpenShadow(el.shadowRoot);
        if (nested) return nested;
      }
    }
    return null;
  }

  // mouseover fires on every element boundary the pointer crosses, and each
  // hit-test (elementsFromPoint) walks the stack at that spot - on a dense page
  // that is a lot of work per pointer sweep. Coalesce to one hit-test per frame
  // against the latest position: the settling spot is always the one processed,
  // so the button is no less responsive.
  let pendingHover = null;
  let hoverScheduled = false;

  function runHover() {
    hoverScheduled = false;
    const at = pendingHover;
    pendingHover = null;
    if (!at) return;
    // Videos (incl. streams/reels) win, found even when covered; images stay
    // opt-in and are only taken from the direct target (never through a layer).
    let media = mediaUnderPointer(at.x, at.y);
    if (!media && at.target instanceof HTMLImageElement) media = at.target;
    if (media && eligible(media)) {
      clearTimeout(hideTimer);
      showButtonFor(media);
    } else if (currentTarget && !currentTarget.contains(at.target)) {
      scheduleHide();
    }
  }

  document.addEventListener(
    "mouseover",
    (event) => {
      if (!enabled || !hoverGlobal || event.target === host) return;
      pendingHover = { x: event.clientX, y: event.clientY, target: event.target };
      if (hoverScheduled) return;
      hoverScheduled = true;
      requestAnimationFrame(runHover);
    },
    { passive: true },
  );
  document.addEventListener("scroll", reposition, { passive: true, capture: true });
  window.addEventListener("resize", reposition, { passive: true });
  // Switching tab or window must not leave a stale button stuck on the page.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) hideButton();
  });
  window.addEventListener("blur", hideButton);
  // A video going fullscreen reparents the page under this fixed button and
  // strands it in the middle of the screen - hide it on any fullscreen change.
  document.addEventListener("fullscreenchange", hideButton, true);
  document.addEventListener("webkitfullscreenchange", hideButton, true);

  button.addEventListener("mouseenter", () => clearTimeout(hideTimer));
  button.addEventListener("mouseleave", scheduleHide);
  button.addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (!currentTarget) return;
    const media = mediaUrlFor(currentTarget);
    const reply = await grablineSend({ cmd: "grab", url: media.url, sniff: media.fromPage });
    // Quick inline feedback, then fade away.
    kit.showFeedback(button, reply, hideButton);
  });

  // No click stealing here on purpose. An earlier build intercepted clicks on
  // "file-looking" links (.jpg, .pdf, .mp4, …) which broke ordinary browsing -
  // image galleries and inline PDF/video viewers stopped opening. Clicks now
  // always navigate normally; if the navigation turns out to be a download,
  // the background's webRequest / downloads.onCreated takeover picks it up.
})();
