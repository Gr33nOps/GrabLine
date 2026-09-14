import { assertEquals, load } from "./harness.js";

const { placeInCorner } = (await load("../content/lib/button-kit.js")).grablineButtonKit;

const rect = { left: 100, top: 100, right: 300, bottom: 250 };
const viewport = { width: 1000, height: 800 };

Deno.test("top-right sits inset from the media's top-right corner", () => {
  assertEquals(placeInCorner(rect, 30, "top-right", viewport), { left: 262, top: 108 });
});

Deno.test("bottom-left sits inset from the media's bottom-left corner", () => {
  assertEquals(placeInCorner(rect, 30, "bottom-left", viewport), { left: 108, top: 212 });
});

Deno.test("an off-screen box clamps the button to the viewport's top-left", () => {
  const off = { left: -500, top: -500, right: -480, bottom: -480 };
  assertEquals(placeInCorner(off, 30, "top-left", viewport), { left: 4, top: 4 });
});

Deno.test("a box past the far edge clamps against the right/bottom", () => {
  const far = { left: 2000, top: 2000, right: 2100, bottom: 2100 };
  assertEquals(placeInCorner(far, 30, "top-right", viewport), { left: 966, top: 766 });
});
