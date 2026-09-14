import { assertEquals, load } from "./harness.js";

const { closestDeep } = (await load("../content/lib/button-kit.js")).grablineButtonKit;

// Minimal Element/ShadowRoot stand-ins - enough to prove closestDeep walks out
// of an open shadow host the way Reddit's shreddit-player requires.

function el(tag) {
  return {
    tagName: tag.toUpperCase(),
    parentElement: null,
    assignedSlot: null,
    shadowRoot: null,
    _root: null,
    matches(selector) {
      const parts = selector.split(",").map((s) => s.trim().toLowerCase());
      const mine = this.tagName.toLowerCase();
      return parts.some((part) => part === mine);
    },
    getRootNode() {
      return this._root || this;
    },
  };
}

Deno.test("closestDeep walks out of an open shadow host to the post", () => {
  const post = el("shreddit-post");
  const player = el("shreddit-player");
  const video = el("video");
  player.parentElement = post;
  const shadow = { host: player };
  video._root = shadow;

  assertEquals(closestDeep(video, "shreddit-player"), player);
  assertEquals(closestDeep(video, "shreddit-post"), post);
  assertEquals(closestDeep(video, "article"), null);
});
