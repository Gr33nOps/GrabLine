import { assertEquals } from "./harness.js";

Deno.test("page collections target the top frame so an empty iframe cannot win the reply race", async () => {
  const source = await Deno.readTextFile(
    new URL("../background.js", import.meta.url),
  );
  const start = source.indexOf("async function sendCollection");
  const end = source.indexOf("api.contextMenus.onClicked.addListener", start);
  let options;
  let delivered;
  const api = {
    tabs: {
      sendMessage: async (_id, _message, selected) => {
        options = selected;
        return {
          urls: selected?.frameId === 0
            ? ["https://fixture.example/image.png"]
            : [],
        };
      },
    },
    runtime: {
      sendNativeMessage: async (_host, message) => {
        delivered = message;
      },
    },
    storage: { session: { set: async () => {} } },
  };
  const collect = new Function(
    "api",
    "HOST_NAME",
    source.slice(start, end) + "return sendCollection;",
  )(api, "dev.grabline.host");
  await collect(
    { id: 11, url: "https://fixture.example/" },
    "collectImages",
    "gallery",
  );
  assertEquals(options?.frameId, 0);
  assertEquals(delivered.urls, ["https://fixture.example/image.png"]);
});
