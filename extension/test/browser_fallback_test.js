import { assertEquals } from "./harness.js";

const source = await Deno.readTextFile(new URL("../background.js", import.meta.url));
const takeoverCode = source.slice(
  source.indexOf("const pendingTakeovers = new Map()"),
  source.indexOf("// Firefox uses downloads.onCreated too."),
);

function harness(reply, status, { resumeFailsOnce = false } = {}) {
  const calls = [];
  let onCreated;
  let failResume = resumeFailsOnce;
  const api = {
    storage: { session: {
      get: async () => ({}),
      set: async () => {},
    } },
    downloads: {
      onCreated: { addListener: (fn) => { onCreated = fn; } },
      pause: async () => { calls.push("pause"); },
      resume: async () => {
        calls.push("resume");
        if (failResume) {
          failResume = false;
          throw new Error("temporary browser error");
        }
      },
      search: async () => [{ paused: true }],
      cancel: async () => { calls.push("cancel"); },
      erase: async () => { calls.push("erase"); },
    },
    runtime: { sendNativeMessage: async () => status() },
  };
  const controls = new Function("api", "reply", "setTimeout", "clearTimeout", `
    const HOST_NAME = "test";
    const handledDownloadIds = new Set();
    const interceptEnabled = true;
    const shouldIntercept = () => true;
    const downloadSourcePage = async () => null;
    const isAssistantPage = () => false;
    const requiresBrowserSession = async () => false;
    const interceptionOn = async () => true;
    const handoffDownloadItem = async () => reply;
    const showBrowserDownload = async () => {};
    ${takeoverCode}
    return { pollTakeovers, pendingTakeovers };
  `)(api, reply, () => 1, () => {});
  const created = async () => {
    onCreated({ id: 7, url: "https://files.example.com/archive.zip" });
    await new Promise((resolve) => setTimeout(resolve, 0));
  };
  return { calls, created, ...controls };
}

Deno.test("desktop failure resumes the original browser download", async () => {
  const h = harness(
    { type: "queued", appRunning: true, previousJobId: 4 },
    () => ({ appRunning: true, jobs: [{
      url: "https://files.example.com/archive.zip", id: 5, status: "failed",
    }] }),
  );
  await h.created();
  assertEquals(h.calls, ["pause"]);
  await h.pollTakeovers();
  assertEquals(h.calls, ["pause", "resume"]);
  assertEquals(h.pendingTakeovers.size, 0);
});

Deno.test("browser item is cancelled only after the new desktop job completes", async () => {
  let jobId = 4;
  const h = harness(
    { type: "queued", appRunning: true, previousJobId: 4 },
    () => ({ appRunning: true, jobs: [{
      url: "https://files.example.com/archive.zip", id: jobId, status: "completed",
    }] }),
  );
  await h.created();
  await h.pollTakeovers();
  assertEquals(h.calls, ["pause"]); // the old completed job is not this handoff
  jobId = 5;
  await h.pollTakeovers();
  assertEquals(h.calls, ["pause", "cancel", "erase"]);
});

Deno.test("host loss and rejected handoffs return downloads to the browser", async () => {
  const lost = harness(
    { type: "queued", appRunning: true, previousJobId: 0 },
    () => { throw new Error("native host closed"); },
  );
  await lost.created();
  await lost.pollTakeovers();
  assertEquals(lost.calls, ["pause", "resume"]);

  const rejected = harness({ type: "notRunning" }, () => ({}));
  await rejected.created();
  assertEquals(rejected.calls, ["pause", "resume"]);
});

Deno.test("a handoff that never creates a job times out to the browser", async () => {
  const h = harness(
    { type: "queued", appRunning: true, previousJobId: 0 },
    () => ({ appRunning: true, jobs: [{
      url: "https://files.example.com/archive.zip", status: "pending",
    }] }),
  );
  await h.created();
  h.pendingTakeovers.get(7).startedAt = Date.now() - 91_000;
  await h.pollTakeovers();
  assertEquals(h.calls, ["pause", "resume"]);
});

Deno.test("a transient browser resume error is retried", async () => {
  const h = harness(
    { type: "queued", appRunning: true, previousJobId: 0 },
    () => ({ appRunning: false }),
    { resumeFailsOnce: true },
  );
  await h.created();
  await h.pollTakeovers();
  assertEquals(h.pendingTakeovers.size, 1);
  await h.pollTakeovers();
  assertEquals(h.calls, ["pause", "resume", "resume"]);
  assertEquals(h.pendingTakeovers.size, 0);
});
