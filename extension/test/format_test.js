import { assertEquals, load } from "./harness.js";

const { humanBytes } = (await load("../content/lib/format.js")).grablineFormat;

Deno.test("humanBytes is empty for zero or missing (the popup shows nothing)", () => {
  assertEquals(humanBytes(0), "");
  assertEquals(humanBytes(undefined), "");
  assertEquals(humanBytes(null), "");
});

Deno.test("humanBytes scales to the right unit and rounds", () => {
  assertEquals(humanBytes(512), "512 B");
  assertEquals(humanBytes(1024), "1.0 KB");
  assertEquals(humanBytes(1536), "1.5 KB");
  assertEquals(humanBytes(5 * 1024 * 1024), "5.0 MB");
  assertEquals(humanBytes(3 * 1024 ** 3), "3.0 GB");
});

Deno.test("humanBytes caps at GB rather than inventing a bigger unit", () => {
  assertEquals(humanBytes(2048 * 1024 ** 3), "2048.0 GB");
});
