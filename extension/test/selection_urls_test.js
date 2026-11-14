import { assertEquals } from "./harness.js";

Deno.test("selected plain-text URLs are collected alongside selected elements", async () => {
  const code = await Deno.readTextFile(
    new URL("../content/overlay.js", import.meta.url),
  );
  const start = code.indexOf("function collectSelection()");
  const end = code.indexOf("api.runtime.onMessage.addListener", start);
  const urls =
    "https://fixture.example/a.bin\nhttps://fixture.example/b.mp4\nhttps://fixture.example/a.bin";
  const selection = {
    isCollapsed: false,
    containsNode: () => false,
    toString: () => urls,
  };
  const document = { links: [], images: [], querySelectorAll: () => [] };
  const collect = new Function(
    "window",
    "document",
    "const MAX_LINKS=300;" + code.slice(start, end) +
      "return collectSelection;",
  )(
    { getSelection: () => selection },
    document,
  );
  assertEquals(collect(), [
    "https://fixture.example/a.bin",
    "https://fixture.example/b.mp4",
  ]);
});
