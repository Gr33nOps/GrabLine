import { assertEquals } from "./harness.js";

Deno.test("Chromium leaves POST attachment responses in the browser", async () => {
  const source = await Deno.readTextFile(
    new URL("../background.js", import.meta.url),
  );
  const marker = source.indexOf("const browserOnlyResponses");
  const start = marker >= 0
    ? marker
    : source.indexOf("api.webRequest.onHeadersReceived.addListener");
  const end = source.indexOf("// While we're taking downloads over", start);
  let headers;
  const noop = { addListener() {} };
  const api = {
    webRequest: {
      onHeadersReceived: {
        addListener(fn) {
          headers = fn;
        },
      },
    },
    tabs: { onRemoved: noop, onUpdated: noop },
    storage: { session: { remove() {} } },
  };
  const intercept = new Function(
    "api",
    "classify",
    "recordMedia",
    "updateBadge",
    source.slice(start, end) + "\nreturn shouldIntercept;",
  )(api, () => null, () => {}, () => {});
  const url = "https://fixture.example/export";
  headers({
    url,
    method: "POST",
    statusCode: 200,
    type: "main_frame",
    tabId: 1,
  });
  assertEquals(intercept({ url }), false);
  // A later GET to the same endpoint is independently replayable.
  headers({
    url,
    method: "GET",
    statusCode: 200,
    type: "main_frame",
    tabId: 1,
  });
  assertEquals(intercept({ url }), true);
});
