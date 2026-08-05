// GrabLine Connect - shared site-module hover button.
//
// Every site module is just a matcher: given a hovered element, return the
// element to anchor the button to and the URL to grab, or null. This file
// owns the rest - the shadow-root button, the show dwell (no flicker while
// scanning a grid), the rect keep-alive (players that spawn *over* the
// anchor steal the hover; as long as the pointer stays inside the anchor's
// box the button survives), and the per-site off switch.
//
// Loaded before each content/sites/*.js via the manifest.

(() => {
  const api = globalThis.browser ?? globalThis.chrome;
  const SHOW_DELAY_MS = 150;
  const RECT_MARGIN = 8;
  const BUTTON_SIZE = 30;

  // The button chrome - icons, logo, colours, corner positioning, click
  // feedback - is shared with the generic overlay in content/lib/button-kit.js
  // so the two can't drift. This file owns the dwell and the rect keep-alive.
  // Clicking hands the URL to GrabLine, which shows its Download Info dialog;
  // the in-page quality panel that used to live here is gone.
  const kit = globalThis.grablineButtonKit;
  // The user's chosen corner (popup), kept live.
  const cornerOf = kit.watchCorner();

  globalThis.grablineSiteButton = ({ resolve }) => {
    let enabled = true;
    let hoverGlobal = true; // master switch (popup): hover buttons off everywhere
    api.storage.local.get(["disabledSites", "hoverButtons"]).then(
      ({ disabledSites = [], hoverButtons = true }) => {
        if (disabledSites.includes(location.hostname)) enabled = false;
        hoverGlobal = hoverButtons;
      },
    );
    api.storage.onChanged.addListener((changes, area) => {
      if (area !== "local") return;
      if (changes.disabledSites) {
        enabled = !(changes.disabledSites.newValue ?? []).includes(location.hostname);
        if (!enabled) hide();
      }
      if (changes.hoverButtons) {
        hoverGlobal = changes.hoverButtons.newValue !== false;
        if (!hoverGlobal) hide();
      }
    });

    const { host, button } = kit.createButton(BUTTON_SIZE);

    let currentUrl = null;
    let currentRect = null;
    // A show that is waiting out the dwell. Tracked with its own rect so DOM
    // churn under the pointer (YouTube spawns preview chips the moment you
    // hover) cannot cancel it while the pointer is still on the thumbnail.
    let pendingUrl = null;
    let pendingRect = null;
    let hideTimer = 0;
    let showTimer = 0;

    function attachHost() {
      // Prefer <html> over <body>: many sites (Reddit, reels feeds) transform
      // <body>, which breaks position:fixed against the viewport.
      const root = document.documentElement || document.body;
      if (root && !host.isConnected) root.appendChild(host);
    }

    function clearPending() {
      clearTimeout(showTimer);
      pendingUrl = null;
      pendingRect = null;
    }

    function hide() {
      clearPending();
      button.style.display = "none";
      currentUrl = null;
      currentRect = null;
    }

    const feedback = (reply) => kit.showFeedback(button, reply, hide);

    function scheduleHide() {
      clearTimeout(hideTimer);
      hideTimer = setTimeout(hide, 300);
    }

    function insideRect(event, rect) {
      return (
        rect !== null &&
        event.clientX >= rect.left - RECT_MARGIN &&
        event.clientX <= rect.right + RECT_MARGIN &&
        event.clientY >= rect.top - RECT_MARGIN &&
        event.clientY <= rect.bottom + RECT_MARGIN
      );
    }

    function showFor(anchor, url) {
      attachHost();
      const rect = anchor.isConnected ? anchor.getBoundingClientRect() : pendingRect;
      clearPending();
      if (rect === null) return; // anchor re-rendered away and we lost it
      currentUrl = url;
      currentRect = rect;
      const position = kit.placeInCorner(rect, BUTTON_SIZE, cornerOf(), {
        width: window.innerWidth,
        height: window.innerHeight,
      });
      button.style.left = `${position.left}px`;
      button.style.top = `${position.top}px`;
      button.style.display = "flex";
      kit.resetButton(button);
    }

    document.addEventListener(
      "mouseover",
      (event) => {
        if (!enabled || !hoverGlobal) return;
        // Walk the composed path so a <video> inside an open shadow root
        // (Reddit shreddit-player, etc.) still reaches the site matcher.
        const path = typeof event.composedPath === "function" ? event.composedPath() : [event.target];
        let hit = null;
        try {
          for (const node of path) {
            if (!(node instanceof Element)) continue;
            hit = resolve(node);
            if (hit) break;
          }
        } catch {
          hit = null; // a matcher must never break the page
        }
        if (!hit) {
          // Pointer still inside the shown or pending target's box: the
          // "miss" is just an overlay/preview stealing the hover - hold on.
          if (insideRect(event, currentRect) || insideRect(event, pendingRect)) return;
          clearPending();
          if (currentUrl) scheduleHide();
          return;
        }
        clearTimeout(hideTimer);
        if (hit.url === currentUrl) return; // already shown for this target
        if (hit.url === pendingUrl) return; // dwell in progress - let it fire
        clearPending();
        pendingUrl = hit.url;
        pendingRect = hit.anchor.getBoundingClientRect();
        showTimer = setTimeout(() => showFor(hit.anchor, hit.url), SHOW_DELAY_MS);
      },
      { passive: true },
    );
    document.addEventListener("scroll", hide, { passive: true, capture: true });
    // Switching tab or window must not leave a stale button behind.
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) hide();
    });
    window.addEventListener("blur", hide);
    // Entering or leaving fullscreen (a video going fullscreen) reparents the
    // page under a fixed-position button, stranding it mid-screen - hide it.
    document.addEventListener("fullscreenchange", hide, true);
    document.addEventListener("webkitfullscreenchange", hide, true);

    button.addEventListener("mouseenter", () => clearTimeout(hideTimer));
    button.addEventListener("mouseleave", scheduleHide);
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (!currentUrl) return;
      // Hand the URL to GrabLine; its Download Info dialog picks the quality.
      feedback(await grablineSend({ cmd: "grab", url: currentUrl }));
    });
  };
})();
