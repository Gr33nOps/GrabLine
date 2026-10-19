import { assertEquals } from "./harness.js";

Deno.test("captured YouTube chunks use the page and its cookies", async () => {
  const code = await Deno.readTextFile(new URL("../background.js", import.meta.url));
  const prefix = code.slice(0, code.indexOf("async function pingGrabLine"));
  const sent = [];
  const cookieUrls = [];
  const api = {
    cookies: { getAll: async ({ url }) => { cookieUrls.push(url); return []; } },
    runtime: { sendNativeMessage: async (_host, message) => { sent.push(message); return { type: "queued" }; } },
    storage: { session: { set: async () => {} } },
  };
  const send = new Function("globalThis", "navigator", prefix + "\nreturn sendToGrabLine;")(
    { browser: api }, { userAgent: "test-browser" },
  );
  const page = "https://www.youtube.com/watch?v=example";
  await send("https://rr1.googlevideo.com/videoplayback?sq=15", { url: page }, {
    fallbackUrls: ["https://rr1.googlevideo.com/videoplayback?sq=14"],
  });
  assertEquals(sent[0].url, page);
  assertEquals(sent[0].fallbackUrls, []);
  assertEquals(cookieUrls, [page]);
  await send("https://example.com/file.zip", { url: page });
  assertEquals(sent[1].url, "https://example.com/file.zip");
});

Deno.test("clearance sessions and unreadable cookie stores stay in browser", async () => {
  const code = await Deno.readTextFile(new URL("../background.js", import.meta.url));
  const prefix = code.slice(0, code.indexOf("// YouTube (and friends)"));
  for (const [cookies, expected] of [
    [[{ name: "cf_clearance", value: "test" }], true],
    [[{ name: "session", value: "test" }], false],
    [[], false],
    [null, true],
  ]) {
    const check = new Function("globalThis", prefix + "\nreturn requiresBrowserSession;")({
      browser: { cookies: { getAll: async () => {
        if (cookies === null) throw new Error("permission unavailable");
        return cookies;
      } } },
    });
    assertEquals(await check("https://files.example.com/archive.7z"), expected);
  }
});

Deno.test("protected download is neither paused nor handed off", async () => {
  const code = await Deno.readTextFile(new URL("../background.js", import.meta.url));
  const start = code.indexOf("api.downloads.onCreated.addListener");
  const end = code.indexOf("// Firefox uses downloads.onCreated too.", start);
  let callback;
  const calls = [];
  const api = { downloads: {
    onCreated: { addListener: (cb) => { callback = cb; } },
    pause: async () => calls.push("pause"),
    cancel: async () => calls.push("cancel"),
  } };
  new Function("api", "requiresBrowserSession", "updateDownloadUi", "downloadSourcePage", "isAssistantPage", `
    const interceptEnabled = true;
    const handledDownloadIds = new Set();
    const shouldIntercept = () => true;
    let browserDownloadVisible = false;
    ${code.slice(start, end)}
  `)(api, async () => true, async () => calls.push("show-browser-download"), async () => null, () => false);
  callback({ id: 1, url: "https://files.example.com/archive.7z" });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assertEquals(calls, ["show-browser-download"]);
});
