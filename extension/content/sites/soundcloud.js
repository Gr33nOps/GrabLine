// GrabLine Connect - SoundCloud site module (F2.6).
//
// SoundCloud plays through a hidden MSE audio element, so the generic
// overlay has nothing to hover - and the page URL is wrong the moment you
// play from Discover or the feed (that's how "soundcloud:user discover
// 404" happens). This module puts the button on actual track links instead:
// the bottom play bar (whatever is playing right now) and track titles in
// lists/charts. Fail-silent: right-click a track title + paste keep
// working if SoundCloud's DOM churns.

(() => {
  const TRACK_LINKS = [
    "a.playbackSoundBadge__titleLink",
    "a.trackItem__trackTitle",
    "a.soundTitle__title",
    "a.chartTrack__title",
  ].join(", ");
  // Track permalinks are exactly /artist/track; these first segments are
  // app chrome, never artists.
  const NOT_TRACKS =
    /^\/(discover|search|feed|library|you|stream|charts|upload|messages|notifications|settings|tags|popular|people|stations|pages)(\/|$)/;

  globalThis.grablineSiteButton({
    resolve(target) {
      const bar = target.closest(".playbackSoundBadge");
      const link = bar
        ? bar.querySelector("a.playbackSoundBadge__titleLink")
        : target.closest(TRACK_LINKS);
      const href = link?.getAttribute("href");
      if (!href) return null;
      let url;
      try {
        url = new URL(href, location.origin);
      } catch {
        return null;
      }
      if (!/^\/[^/]+\/[^/]+$/.test(url.pathname) || NOT_TRACKS.test(url.pathname)) {
        return null;
      }
      return { anchor: bar ?? link, url: `${location.origin}${url.pathname}` };
    },
  });
})();
