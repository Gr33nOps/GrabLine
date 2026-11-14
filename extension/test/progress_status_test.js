import { assertEquals } from "./harness.js";

Deno.test("paused page progress shows the paused state instead of an active percentage", async () => {
  const code = await Deno.readTextFile(
    new URL("../content/overlay.js", import.meta.url),
  );
  const start = code.indexOf("function pillText(job)");
  const end = code.indexOf("function renderProgress", start);
  const text = new Function(
    "humanBytes",
    code.slice(start, end) + "return pillText;",
  )((v) => String(v));
  assertEquals(
    text({ name: "qa.bin", status: "paused", downloaded: 50, total: 100 }),
    "Paused · qa.bin",
  );
});
