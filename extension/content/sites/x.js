// GrabLine Connect - X/Twitter site module (F2.6).
//
// Timeline videos play from blob: URLs, so the generic overlay could only
// offer the *timeline* URL - useless. This module shows the button when the
// pointer is over a video inside a tweet and hands the tweet's permalink
// (/user/status/<id>) to the app; yt-dlp resolves the actual media.
// Fail-silent by design; right-click on the tweet's timestamp link and
// paste keep working if X's DOM churns.

(() => {
  const PLAYER = "video, [data-testid='videoPlayer'], [data-testid='videoComponent']";
  const STATUS_PATH = /^\/[^/]+\/status\/\d+/;

  globalThis.grablineSiteButton({
    resolve(target) {
      const player = target.closest(PLAYER);
      if (!player) return null;
      const article = player.closest("article");
      if (!article) return null;
      for (const link of article.querySelectorAll("a[href*='/status/']")) {
        const href = link.getAttribute("href") ?? "";
        const match = href.match(STATUS_PATH);
        if (match) return { anchor: player, url: `${location.origin}${match[0]}` };
      }
      return null;
    },
  });
})();
