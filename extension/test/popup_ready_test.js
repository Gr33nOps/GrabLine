import { assertEquals } from "./harness.js";

Deno.test("popup preferences save once connected even while recent jobs are loading", async () => {
  const source = await Deno.readTextFile(
    new URL("../popup/popup.js", import.meta.url),
  );
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) {
        elements.set(id, {
          checked: false,
          disabled: false,
          value: "",
          textContent: "",
          listeners: {},
          addEventListener(type, callback) {
            this.listeners[type] = callback;
          },
        });
      }
      return elements.get(id);
    },
  };
  const saved = { intercept: false };
  const api = {
    runtime: { getManifest: () => ({ version: "test" }) },
    storage: {
      local: {
        get: (key) => Promise.resolve({ [key]: saved[key] }),
        set: (values) => {
          Object.assign(saved, values);
          return Promise.resolve();
        },
      },
    },
  };
  let connected = false;
  let release;
  const recent = new Promise((resolve) => {
    release = resolve;
  });
  const run = new Function(
    "api",
    "document",
    "activeTab",
    "renderStatus",
    "renderQuickActions",
    "renderMediaList",
    "renderRecent",
    "paired",
    `
      ${source.slice(source.indexOf("async function wireToggles"))}
    `,
  );
  run(
    api,
    document,
    async () => ({ url: "http://127.0.0.1/" }),
    async () => {
      connected = true;
    },
    async () => {},
    async () => {},
    () => recent,
    true,
  );
  for (let i = 0; i < 100 && !connected; i++) await Promise.resolve();
  assertEquals(connected, true);
  const checkbox = document.getElementById("toggle-intercept");
  checkbox.checked = true;
  checkbox.listeners.change?.();
  await Promise.resolve();
  release();
  assertEquals(saved.intercept, true);
});
