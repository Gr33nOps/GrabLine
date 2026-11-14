import { assertEquals } from "./harness.js";

Deno.test("Chromium takeover retains the source tab id for real progress updates", async () => {
  const source = await Deno.readTextFile(
    new URL("../background.js", import.meta.url),
  );
  const start = source.indexOf("async function handoffDownloadItem");
  const end = source.indexOf("const pendingTakeovers", start);
  let delivered;
  const active = { id: 17, url: "https://fixture.example/page", title: "QA" };
  const api = { tabs: { query: async () => [active] } };
  const handoff = new Function(
    "api",
    "sendToGrabLine",
    source.slice(start, end) + "return handoffDownloadItem;",
  )(
    api,
    async (url, tab) => {
      delivered = { url, tab };
      return { type: "queued" };
    },
  );
  await handoff({ url: "https://fixture.example/file.bin" }, active.url);
  assertEquals(delivered.tab.id, 17);
});

Deno.test("cross-origin downloads retain progress when Chromium truncates the referrer to its origin", async () => {
  const source = await Deno.readTextFile(
    new URL("../background.js", import.meta.url),
  );
  const start = source.indexOf("async function handoffDownloadItem");
  const end = source.indexOf("const pendingTakeovers", start);
  let delivered;
  const active = {
    id: 23,
    url: "https://fixture.example/nested/page",
    title: "QA",
  };
  const api = { tabs: { query: async () => [active] } };
  const handoff = new Function(
    "api",
    "sendToGrabLine",
    source.slice(start, end) + "return handoffDownloadItem;",
  )(
    api,
    async (url, tab) => {
      delivered = { url, tab };
      return { type: "queued" };
    },
  );
  await handoff(
    { url: "https://cdn.example/file.bin" },
    "https://fixture.example/",
  );
  assertEquals(delivered.tab.id, 23);
});
