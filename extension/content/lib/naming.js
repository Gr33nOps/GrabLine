// GrabLine Connect - naming a sniffed stream for the popup's media list.
//
// The desktop app does the authoritative naming when a download is saved
// (app/core/naming.py). This is the display-side mirror: a stream's URL leaf is
// usually a meaningless hash (master.m3u8, videoplayback, a hex id), so we fall
// back to the page title with the site-name boilerplate stripped, matching
// naming.clean_page_title. Keep the two in step when either changes.
(() => {
  // Generic stream/segment leaf names that carry no meaning - fall back to the
  // page title for these (master.m3u8, videoplayback, a hex hash, ...).
  const UGLY_LEAF =
    /^(master|index|playlist|manifest|chunklist|videoplayback|video|audio|media|stream|init|segment|seg|frag|output|dash|hls|prog)\b/i;

  function cleanTitle(title) {
    return (title || "")
      .replace(
        /\s*[-|•·:-]\s*(YouTube|Instagram|Vimeo|TikTok|Twitter|X|Facebook|Dailymotion|Twitch|SoundCloud|Reddit)\s*$/i,
        "",
      )
      .trim();
  }

  function mediaName(item, tab) {
    try {
      const leaf = decodeURIComponent(
        new URL(item.url).pathname.split("/").filter(Boolean).pop() || "",
      );
      const base = leaf.replace(/\.[a-z0-9]{2,4}$/i, "");
      const named =
        leaf && base.length >= 3 && !UGLY_LEAF.test(base) && !/^[0-9a-f]{12,}$/i.test(base);
      if (named) return leaf;
    } catch {
      // Unparsable URL - fall through to the title.
    }
    return cleanTitle(item.title) || cleanTitle(tab?.title) || item.kind || "media";
  }

  globalThis.grablineNaming = { UGLY_LEAF, cleanTitle, mediaName };
})();
