// GrabLine Connect - Reddit site module.
//
// Native posts keep <video> inside shreddit-player's open Shadow DOM, so the
// generic overlay's elementsFromPoint never sees a media element. Link posts
// (r/videos → YouTube) often have no <video> at all until you click - just a
// thumbnail. This module shows the button on the player / media region and
// hands the post permalink to the app; yt-dlp resolves the real media.
// Fail-silent by design: if Reddit's DOM churns, right-click and paste keep
// working and this file is the whole blast radius.

(() => {
  const { closestDeep } = globalThis.grablineButtonKit;
  const PLAYER = "shreddit-player, shreddit-player-2";
  const MEDIA_ZONE =
    "shreddit-player, shreddit-player-2, [slot='post-media-container'], [id$='-aspect-ratio']";
  const EXTERNAL_VIDEO =
    /youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|redd\.it|v\.redd\.it|streamable\.com|twitch\.tv|tiktok\.com|imgur\.com|gfycat\.com|giphy\.com/i;

  function postPermalink(post) {
    const raw = post.getAttribute("permalink");
    if (!raw) return null;
    try {
      return new URL(raw, location.origin).href.split("?")[0];
    } catch {
      return null;
    }
  }

  function isMediaPost(post) {
    const type = (post.getAttribute("post-type") || "").toLowerCase();
    if (type === "video" || type === "gif" || type === "gallery" || type === "image") {
      return true;
    }
    const href = post.getAttribute("content-href") || "";
    return EXTERNAL_VIDEO.test(href);
  }

  globalThis.grablineSiteButton({
    resolve(target) {
      const player = closestDeep(target, PLAYER);
      const post = closestDeep(target, "shreddit-post");
      if (player && post) {
        const url = postPermalink(post);
        if (url) return { anchor: player, url };
      }
      if (!post || !isMediaPost(post)) return null;
      // Only the media region - not the title, votes, or sidebar chrome.
      const zone = closestDeep(target, MEDIA_ZONE);
      if (!zone && !(target instanceof HTMLImageElement) && target.tagName !== "IMG") {
        return null;
      }
      const url = postPermalink(post);
      if (!url) return null;
      const anchor =
        post.querySelector(PLAYER) ||
        post.querySelector("[slot='post-media-container'], [id$='-aspect-ratio']") ||
        zone ||
        post;
      return { anchor, url };
    },
  });
})();
