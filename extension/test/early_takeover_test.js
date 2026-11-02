import { assertEquals } from "./harness.js";

const source = await Deno.readTextFile(new URL("../background.js", import.meta.url));

function harness({ firefox = true, intercept = true, cookies = [], reply } = {}) {
  const events = {};
  const event = (name) => ({ addListener: (fn, ...args) => {
    (events[name] ??= []).push({ fn, args });
  } });
  const sent = [];
  const calls = [];
  const tab = { id: 1, url: "https://site.example/downloads", title: "Files" };
  const api = {
    runtime: {
      getManifest: () => ({ permissions: ["webRequestBlocking"] }),
      ...(firefox ? { getBrowserInfo: async () => ({ name: "Firefox" }) } : {}),
      onStartup: event("startup"), onInstalled: event("installed"),
      onMessage: event("message"),
      sendNativeMessage: async (_host, message) => {
        if (message.type === "ping") return { appRunning: true };
        sent.push(message);
        return reply ? reply(message) : { type: "queued", appRunning: true, previousJobId: 0 };
      },
    },
    storage: {
      session: { get: async () => ({}), set: async () => {}, remove: async () => {} },
      local: { get: async () => ({ intercept }) }, onChanged: event("storage"),
    },
    cookies: { getAll: async () => cookies },
    tabs: {
      get: async () => tab, query: async () => [tab],
      onRemoved: event("removed"), onUpdated: event("updated"),
    },
    contextMenus: { removeAll: (done) => done(), create: () => {}, onClicked: event("menu") },
    webRequest: { onHeadersReceived: event("headers") },
    downloads: {
      onCreated: event("created"), onChanged: event("changed"), onErased: event("erased"),
      pause: async () => calls.push("pause"), cancel: async () => calls.push("cancel"),
      resume: async () => calls.push("resume"), erase: async () => calls.push("erase"),
      search: async () => [{ state: "complete" }],
    },
  };
  new Function("globalThis", "navigator", "setTimeout", source)(
    { browser: api }, { userAgent: "test" }, () => 1,
  );
  const blocking = events.headers.find(({ args }) => args[1].includes("blocking"));
  return { events, sent, calls, blocking, tab };
}

const response = (overrides = {}) => ({
  url: "https://files.example/file.torrent", method: "GET", statusCode: 200,
  tabId: 1, type: "main_frame", originUrl: "https://site.example/downloads",
  responseHeaders: [{ name: "Content-Type", value: "application/x-bittorrent" }],
  ...overrides,
});

Deno.test("Firefox cancels accepted file responses before any browser download API call", async () => {
  const h = harness({ cookies: [{ name: "session", value: "synthetic" }] });
  assertEquals(await h.blocking.fn(response()), { cancel: true });
  assertEquals(h.calls, []);
  assertEquals(h.sent.length, 1);
  assertEquals(h.sent[0].url, "https://files.example/file.torrent");
  assertEquals(h.sent[0].onlyIfRunning, true);
  assertEquals(h.sent[0].cookie, "session=synthetic");
  // Retries of the same response are cancelled without another desktop job.
  assertEquals(await h.blocking.fn(response()), { cancel: true });
  assertEquals(h.sent.length, 1);
});

Deno.test("attachment responses work without a file extension, including concurrent retries", async () => {
  const h = harness();
  const file = response({ url: "https://files.example/export?id=123", responseHeaders: [
    { name: "Content-Type", value: "text/plain" },
    { name: "Content-Disposition", value: 'Attachment; filename="notes.txt"' },
  ] });
  assertEquals(await Promise.all([h.blocking.fn(file), h.blocking.fn(file)]),
    [{ cancel: true }, { cancel: true }]);
  assertEquals(h.sent.length, 1);
});

Deno.test("a rejected or unavailable desktop app leaves the original response intact", async () => {
  let accept = false;
  const h = harness({ reply: () => {
    if (!accept) throw new Error("native host unavailable");
    return { type: "queued", appRunning: true };
  } });
  assertEquals(await h.blocking.fn(response()), {});
  accept = true;
  assertEquals(await h.blocking.fn(response()), { cancel: true });
  assertEquals(h.sent.length, 2); // failed handoff must not poison deduplication
  for (const reply of [{ type: "notRunning" }, { type: "queued", appRunning: false }]) {
    const closed = harness({ reply: () => reply });
    assertEquals(await closed.blocking.fn(response()), {});
  }
});

Deno.test("pages, playback, API fetches, POST exports, redirects and errors are untouched", async () => {
  const h = harness();
  for (const override of [
    { type: "media" }, { type: "xmlhttprequest" }, { method: "POST" },
    { statusCode: 302 }, { statusCode: 403 }, { statusCode: 204 },
    { responseHeaders: [{ name: "Content-Type", value: "text/html" }] },
    { responseHeaders: [{ name: "Content-Type", value: "video/mp4" }] },
    { responseHeaders: [{ name: "Content-Type", value: "application/pdf" }] },
    { tabId: -1, originUrl: "moz-extension://id/background.js" },
  ]) assertEquals(await h.blocking.fn(response(override)), {});
  assertEquals(h.sent, []);
  // A redirected final file response can be accepted normally.
  assertEquals(await h.blocking.fn(response({ url: "https://cdn.example/final.zip" })), { cancel: true });
});

Deno.test("disabled interception, protected sessions and assistant exports remain browser-owned", async () => {
  for (const options of [{ intercept: false }, { cookies: [{ name: "cf_clearance" }] }]) {
    const h = harness(options);
    assertEquals(await h.blocking.fn(response()), {});
    assertEquals(h.sent, []);
  }
  const h = harness();
  assertEquals(await h.blocking.fn(response({ originUrl: "https://chatgpt.com/c/example" })), {});
  assertEquals(h.sent, []);
});

Deno.test("Firefox never pauses items that remain browser-owned; Chrome retains its fallback", async () => {
  const h = harness();
  h.events.created[0].fn({ id: 1, url: "blob:https://example.com/test" });
  await Promise.resolve();
  assertEquals(h.calls, []);
  assertEquals(h.sent, []);
  const chrome = harness({ firefox: false });
  assertEquals(chrome.blocking, undefined);
});

Deno.test("concurrent Chromium takeovers share one accepted native handoff", async () => {
  const h = harness({ firefox: false });
  h.events.created[0].fn({ id: 1, url: "https://files.example/file.zip" });
  h.events.created[0].fn({ id: 2, url: "https://files.example/file.zip" });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assertEquals(h.sent.length, 1);
  assertEquals(h.calls, ["pause", "pause", "cancel", "erase"]);
});
