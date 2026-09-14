// GrabLine Connect - Vimeo site module (F2.6).
//
// A hover button on links to Vimeo videos (vimeo.com/<digits>, on browse pages,
// channels, search). Clicking hands the canonical video URL to the app.
// Fail-silent by design: if Vimeo's DOM churns, right-click and paste keep
// working and this file is the whole blast radius.

(() => {
  const VIDEO_PATH = /^\/(\d{6,})(?:[/?#]|$)/;

  globalThis.grablineSiteButton({
    resolve(target) {
      const anchor = target.closest("a[href]");
      const href = anchor?.getAttribute("href");
      if (!href) return null;
      let url;
      try {
        url = new URL(href, location.origin);
      } catch {
        return null;
      }
      if (!/(^|\.)vimeo\.com$/.test(url.hostname)) return null;
      const match = url.pathname.match(VIDEO_PATH);
      if (!match) return null;
      return { anchor, url: `https://vimeo.com/${match[1]}` };
    },
  });
})();
