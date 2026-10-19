import { assertEquals } from "./harness.js";

const code = await Deno.readTextFile(new URL("../background.js", import.meta.url));
const helperCode = code.slice(0, code.indexOf("// YouTube (and friends)"));
const interceptCode = code.slice(
  code.indexOf("api.downloads.onCreated.addListener"),
  code.indexOf("// Firefox uses downloads.onCreated too."),
);
Deno.test("assistant source pages are matched by host, not a lookalike URL", () => {
  const matches = new Function("globalThis", helperCode + "\nreturn isAssistantPage;")({ browser: {} });
  for (const url of [
    "https://chatgpt.com/c/123", "https://chat.openai.com/c/123",
    "https://claude.ai/chat/123", "https://gemini.google.com/app/123",
    "https://aistudio.google.com/prompts/new_chat",
  ]) assertEquals(matches(url), true);
  for (const url of [
    "https://example.com/file.pdf", "https://chatgpt.com.evil.example/file.pdf", "garbage",
  ]) assertEquals(matches(url), false);
});

Deno.test("browser-generated blob downloads stay in the browser even with an HTTP final URL", () => {
  const start = code.indexOf("function shouldIntercept(item)");
  const end = code.indexOf("// While we're taking downloads over", start);
  const shouldIntercept = new Function(code.slice(start, end) + "\nreturn shouldIntercept;")();
  assertEquals(shouldIntercept({ url: "blob:https://chatgpt.com/123", finalUrl: "https://cdn.example.com/a.pdf" }), false);
  assertEquals(shouldIntercept({ url: "data:application/pdf;base64,abc" }), false);
  assertEquals(shouldIntercept({ url: "https://cdn.example.com/a.pdf" }), true);
});

Deno.test("Chrome leaves generated chat downloads untouched and shows browser Downloads", async () => {
  for (const source of ["https://chatgpt.com/c/1", "https://claude.ai/chat/1", "https://gemini.google.com/app/1"]) {
    let onCreated;
    const calls = [];
    const api = { downloads: {
      onCreated: { addListener: (callback) => { onCreated = callback; } },
      pause: async () => calls.push("pause"),
      cancel: async () => calls.push("cancel"),
    } };
    const fn = new Function("api", "downloadSourcePage", "isAssistantPage", "requiresBrowserSession", "updateDownloadUi", `
      const interceptEnabled = true;
      const handledDownloadIds = new Set();
      const shouldIntercept = () => true;
      let browserDownloadVisible = false;
      ${interceptCode}
    `);
    const matches = new Function("globalThis", helperCode + "\nreturn isAssistantPage;")({ browser: {} });
    fn(api, async (item) => item.referrer, matches, async () => false,
      async () => calls.push("show-browser-download"));
    onCreated({ id: 1, url: "https://assets.example.com/generated.pdf", referrer: source });
    await new Promise((resolve) => setTimeout(resolve, 0));
    assertEquals(calls, ["show-browser-download"]);
  }
});

Deno.test("download source uses referrer, then originating tab, then active tab", async () => {
  const api = { tabs: {
    get: async (id) => ({ url: id === 7 ? "https://gemini.google.com/app/1" : null }),
    query: async () => [{ url: "https://claude.ai/chat/1" }],
  } };
  const source = new Function("globalThis", helperCode + "\nreturn downloadSourcePage;")({ browser: api });
  assertEquals(await source({ referrer: "https://chatgpt.com/c/1", tabId: 7 }), "https://chatgpt.com/c/1");
  assertEquals(await source({ tabId: 7 }), "https://gemini.google.com/app/1");
  assertEquals(await source({}), "https://claude.ai/chat/1");
});
