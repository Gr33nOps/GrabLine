import { assertEquals, load } from "./harness.js";

const { cleanTitle, mediaName } = (await load("../content/lib/naming.js")).grablineNaming;

Deno.test("cleanTitle strips the site-name boilerplate off a tab title", () => {
  assertEquals(cleanTitle("Great Video - YouTube"), "Great Video");
  assertEquals(cleanTitle("Reel | Instagram"), "Reel");
  assertEquals(cleanTitle("Track · SoundCloud"), "Track");
  assertEquals(cleanTitle(""), "");
  assertEquals(cleanTitle(null), "");
});

Deno.test("mediaName keeps a meaningful URL leaf", () => {
  assertEquals(
    mediaName({ url: "https://cdn.example/videos/holiday-clip.mp4" }, null),
    "holiday-clip.mp4",
  );
});

Deno.test("mediaName falls back to the title for an ugly or hash leaf", () => {
  assertEquals(
    mediaName({ url: "https://cdn.example/hls/master.m3u8", title: "My Movie - YouTube" }, null),
    "My Movie",
  );
  assertEquals(
    mediaName({ url: "https://cdn.example/1a2b3c4d5e6f7a.ts", title: "Clip" }, null),
    "Clip",
  );
});

Deno.test("mediaName last-resorts to the media kind, then a generic word", () => {
  assertEquals(mediaName({ url: "https://cdn.example/master.m3u8", kind: "stream" }, null), "stream");
  assertEquals(mediaName({ url: "not a url" }, null), "media");
});
