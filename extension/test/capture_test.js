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
