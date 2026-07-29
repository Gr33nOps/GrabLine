// Load a content-script lib file into a fresh sandbox and return whatever it
// attached to globalThis. The extension's lib files are plain classic scripts
// (an IIFE that assigns globalThis.grablineX = {...}) so they can be injected as
// content scripts; running them here with globalThis rebound to a throwaway
// object lets the pure logic be tested without a browser.
export async function load(relPath) {
  const code = await Deno.readTextFile(new URL(relPath, import.meta.url));
  const sandbox = {};
  new Function("globalThis", code)(sandbox);
  return sandbox;
}

export function assertEquals(actual, expected, message) {
  const got = JSON.stringify(actual);
  const want = JSON.stringify(expected);
  if (got !== want) throw new Error(`${message ?? "assertEquals"}: got ${got}, want ${want}`);
}
