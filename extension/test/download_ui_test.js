import { assertEquals } from "./harness.js";

const source = await Deno.readTextFile(new URL("../background.js", import.meta.url));
const trackingCode = source.slice(
  source.indexOf("const browserVisibleIds = new Set()"),
  source.indexOf("// null until storage answers."),
);

Deno.test("browser download UI returns to takeover mode after native downloads end", async () => {
  let changed;
  let erased;
  const visible = [];
  const api = { downloads: {
    onChanged: { addListener: (listener) => { changed = listener; } },
    onErased: { addListener: (listener) => { erased = listener; } },
    search: async () => [{ state: "in_progress" }],
  } };
  const { showBrowserDownload } = new Function("api", "visible", `
    async function updateDownloadUi() { visible.push(browserVisibleIds.size > 0); }
    ${trackingCode}
    return { showBrowserDownload };
  `)(api, visible);

  await showBrowserDownload(1);
  await showBrowserDownload(2);
  changed({ id: 1, state: { current: "complete" } });
  assertEquals(visible, [true, true, true]);
  erased(2);
  assertEquals(visible, [true, true, true, false]);

  await showBrowserDownload(3);
  changed({ id: 3, state: { current: "interrupted" } });
  assertEquals(visible.slice(-2), [true, false]);
});

Deno.test("a download completed before metadata lookup cannot leave the UI visible", async () => {
  let changed;
  const visible = [];
  const api = { downloads: {
    onChanged: { addListener: (listener) => { changed = listener; } },
    onErased: { addListener: () => {} },
    search: async () => [{ state: "complete" }],
  } };
  const showBrowserDownload = new Function("api", "visible", `
    async function updateDownloadUi() { visible.push(browserVisibleIds.size > 0); }
    ${trackingCode}
    return showBrowserDownload;
  `)(api, visible);

  await showBrowserDownload(8);
  assertEquals(visible, [true, false]);
  changed({ id: 8, state: { current: "complete" } });
  assertEquals(visible, [true, false]);
});
