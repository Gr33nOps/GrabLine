// GrabLine Connect - Instagram site module.
//
// A hover button on Reels and video posts: on links to /reel/<id> or /p/<id>
// (profile grids, the explore page), and on the playing <video> itself in
// the Reels feed or a post lightbox. Clicking hands the canonical URL to
// the app, which resolves it with yt-dlp. Fail-silent by design: if
// Instagram's DOM churns, right-click and paste keep working and this file
// is the whole blast radius.

(() => {
  const ITEM = /^\/(reels?|p)\/([A-Za-z0-9_-]+)/;

  function canonical(pathname) {
    const match = pathname.match(ITEM);
    if (!match) return null;
    const kind = match[1] === "p" ? "p" : "reel";
    return `https://www.instagram.com/${kind}/${match[2]}/`;
  }

  globalThis.grablineSiteButton({
    resolve(target) {
      const anchor = target.closest("a[href]");
      if (anchor) {
        let url;
        try {
          url = new URL(anchor.getAttribute("href"), location.origin);
        } catch {
          return null;
        }
        if (!/(^|\.)instagram\.com$/.test(url.hostname)) return null;
        const canon = canonical(url.pathname);
        if (!canon) return null;
        return { anchor, url: canon };
      }
      // Hovering the playing video itself (the Reels feed, a post lightbox).
      const video = target.closest("video");
      if (!video) return null;
      const article = video.closest("article");
      const link = article?.querySelector(
        'a[href^="/reel/"], a[href^="/reels/"], a[href^="/p/"]'
      );
      let pathname = location.pathname;
      if (link) {
        try {
          pathname = new URL(link.getAttribute("href"), location.origin).pathname;
        } catch {
          /* keep location.pathname */
        }
      }
      const canon = canonical(pathname);
      if (!canon) return null;
      return { anchor: video, url: canon };
    },
  });
})();
