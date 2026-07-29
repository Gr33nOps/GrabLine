// GrabLine Connect - Dailymotion site module.
//
// The watch page hosts the real player in a cross-origin iframe
// (geo.dailymotion.com), so a top-frame-only overlay never sees pointer
// events over the video. This module runs in both frames (manifest
// all_frames): on www it covers /video/<id> links and the player chrome;
// inside geo it finds the <video> and hands the canonical watch URL
// (from the path, query, or embedding referrer). Fail-silent by design.

(() => {
  const { closestDeep } = globalThis.grablineButtonKit;
  const VIDEO_PATH = /^\/video\/([a-zA-Z0-9]+)(?:[/?#]|$)/;
  const PLAYER_ZONE =
    "#player-wrapper, #player-embed-script-wrapper, .dailymotion-player-wrapper, iframe.dailymotion-player, video";

  function videoIdFromLocation() {
    const fromPath = location.pathname.match(VIDEO_PATH);
    if (fromPath) return fromPath[1];
    try {
      const params = new URLSearchParams(location.search);
      for (const key of ["video", "video_id", "id", "vid"]) {
        const value = params.get(key);
        if (value && /^[a-zA-Z0-9]+$/.test(value)) return value;
      }
    } catch {
      /* ignore */
    }
    const fromRef = (document.referrer || "").match(/dailymotion\.com\/video\/([a-zA-Z0-9]+)/i);
    if (fromRef) return fromRef[1];
    // Player shells often stash the id in boot JSON when the query string is empty.
    try {
      const html = document.documentElement?.innerHTML?.slice(0, 250000) || "";
      const boot = html.match(/\b(?:video_id|videoId|"video")\s*[:=]\s*["']?(x[a-zA-Z0-9]+)/);
      if (boot) return boot[1];
    } catch {
      /* ignore */
    }
    return null;
  }

  function canonical(id) {
    return id ? `https://www.dailymotion.com/video/${id}` : null;
  }

  const ON_GEO = /(^|\.)geo\.dailymotion\.com$/.test(location.hostname);

  globalThis.grablineSiteButton({
    resolve(target) {
      // Browse / related rails: a link to /video/<id>.
      const anchor = closestDeep(target, "a[href*='/video/']");
      if (anchor) {
        let url;
        try {
          url = new URL(anchor.getAttribute("href"), location.origin);
        } catch {
          return null;
        }
        if (!/(^|\.)dailymotion\.com$/.test(url.hostname)) return null;
        const match = url.pathname.match(VIDEO_PATH);
        if (!match) return null;
        return { anchor, url: canonical(match[1]) };
      }

      const id = videoIdFromLocation();
      if (!id) return null;

      // geo player iframe: the whole frame is the player - any hover counts.
      if (ON_GEO) {
        const media = closestDeep(target, "video") || document.querySelector("video") || target;
        return {
          anchor: media instanceof Element ? media : document.documentElement,
          url: canonical(id),
        };
      }

      // www watch page: only when the pointer is over the player chrome.
      const zone =
        closestDeep(target, PLAYER_ZONE) ||
        (target instanceof HTMLMediaElement ? target : null);
      if (!zone) return null;
      return { anchor: zone, url: canonical(id) };
    },
  });
})();
