// GrabLine Connect - toolbar popup: pairing status, quick actions, the tab's
// detected media, recent downloads, and the interception / hover preferences.

const api = globalThis.browser ?? globalThis.chrome;
const { humanBytes } = globalThis.grablineFormat;
const { mediaName } = globalThis.grablineNaming;

async function activeTab() {
  const [tab] = await api.tabs.query({ active: true, currentWindow: true });
  return tab ?? null;
}

let paired = false;

async function renderStatus() {
  const status = document.getElementById("status");
  const help = document.getElementById("pairing-help");
  const reply = await api.runtime.sendMessage({ cmd: "ping" });
  if (!reply) {
    status.textContent = "not paired";
    status.className = "status bad";
    help.hidden = false;
    paired = false;
  } else if (reply.appRunning) {
    status.textContent = "connected";
    status.className = "status ok";
    paired = true;
  } else {
    status.textContent = "app not running";
    status.className = "status warn";
    status.title = "Downloads queue and start when you open GrabLine.";
    paired = true;
  }
}

const STATUS_LABEL = {
  downloading: "Downloading",
  queued: "Queued",
  paused: "Paused",
  completed: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
  pending: "Pending",
};
const STATUS_COLOR = {
  completed: "var(--ok)",
  failed: "var(--warn)",
  cancelled: "var(--text3)",
  paused: "var(--text3)",
};

async function renderQuickActions(tab) {
  document.getElementById("open-app").addEventListener("click", () => {
    void api.runtime.sendMessage({ cmd: "focus" });
    window.close();
  });
  const grabTab = document.getElementById("grab-tab");
  grabTab.addEventListener("click", async () => {
    if (!tab?.url) return;
    grabTab.disabled = true;
    grabTab.textContent = "Sent";
    await grablineSend({ cmd: "grab", url: tab.url, tabId: tab.id });
  });

  const input = document.getElementById("paste-url");
  const send = document.getElementById("paste-send");
  const submit = async () => {
    const url = input.value.trim();
    if (!/^https?:\/\//i.test(url)) return;
    send.disabled = true;
    send.textContent = "Sent";
    await grablineSend({ cmd: "grab", url });
    input.value = "";
    setTimeout(() => {
      send.disabled = false;
      send.textContent = "Send";
    }, 1200);
  };
  send.addEventListener("click", submit);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") void submit();
  });

  document.getElementById("open-settings").addEventListener("click", () => {
    void api.runtime.sendMessage({ cmd: "focus", target: "settings" });
    window.close();
  });
}

async function renderMediaList(tab) {
  const list = document.getElementById("media-list");
  const key = `tab:${tab.id}`;
  const stored = await api.storage.session.get(key);
  const all = stored[key] ?? [];
  if (!all.length) return;
  const items = [...all].sort((a, b) => (b.seenAt ?? 0) - (a.seenAt ?? 0)).slice(0, 6);
  list.textContent = "";
  for (const item of items) {
    const row = document.createElement("li");
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = item.kind;
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = mediaName(item, tab);
    name.title = item.url;
    const size = document.createElement("span");
    size.className = "size";
    size.textContent = humanBytes(item.size);
    const grab = document.createElement("button");
    grab.textContent = "Download";
    grab.addEventListener("click", async () => {
      grab.disabled = true;
      grab.textContent = "Sent";
      grab.classList.add("done");
      const reply = await grablineSend({
        cmd: "grab",
        url: item.url,
        tabId: tab.id,
        title: mediaName(item, tab),
      });
      if (reply?.type === "error") {
        grab.textContent = "Failed";
        grab.classList.remove("done");
        grab.disabled = false;
      }
    });
    row.append(kind, name, size, grab);
    list.appendChild(row);
  }
}

async function renderRecent() {
  const reply = await api.runtime.sendMessage({ cmd: "recent" });
  const jobs = reply?.jobs ?? [];
  if (!jobs.length) return;
  document.getElementById("recent-section").hidden = false;
  const list = document.getElementById("recent-list");
  list.textContent = "";
  for (const job of jobs) {
    const row = document.createElement("li");
    row.className = "recent-row";
    row.addEventListener("click", () => {
      void api.runtime.sendMessage({ cmd: "focus" });
      window.close();
    });

    const top = document.createElement("div");
    top.className = "recent-top";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = job.name || job.url;
    const state = document.createElement("span");
    state.className = "recent-status";
    state.textContent = STATUS_LABEL[job.status] ?? job.status;
    state.style.color = STATUS_COLOR[job.status] ?? "var(--accent)";
    top.append(name, state);

    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("span");
    const pct =
      job.status === "completed"
        ? 100
        : job.total
          ? Math.round((job.downloaded / job.total) * 100)
          : 0;
    fill.style.width = `${pct}%`;
    if (STATUS_COLOR[job.status]) fill.style.background = STATUS_COLOR[job.status];
    bar.appendChild(fill);

    row.append(top, bar);
    list.appendChild(row);
  }
}

async function wireToggles(tab) {
  const intercept = document.getElementById("toggle-intercept");
  const hover = document.getElementById("toggle-hover");
  const overlay = document.getElementById("toggle-overlay");
  const images = document.getElementById("toggle-images");
  const { intercept: interceptOn = true } = await api.storage.local.get("intercept");
  intercept.checked = interceptOn;
  intercept.addEventListener("change", () => {
    void api.storage.local.set({ intercept: intercept.checked });
  });

  const { hoverButtons = true } = await api.storage.local.get("hoverButtons");
  hover.checked = hoverButtons;
  const reflectHover = () => {
    overlay.disabled = images.disabled = !hover.checked;
  };
  reflectHover();
  hover.addEventListener("change", () => {
    void api.storage.local.set({ hoverButtons: hover.checked });
    reflectHover();
  });

  const { overlayImages = false } = await api.storage.local.get("overlayImages");
  images.checked = overlayImages;
  images.addEventListener("change", () => {
    void api.storage.local.set({ overlayImages: images.checked });
  });

  const pill = document.getElementById("toggle-pill");
  const { pagePill = false } = await api.storage.local.get("pagePill");
  pill.checked = pagePill;
  pill.addEventListener("change", () => {
    void api.storage.local.set({ pagePill: pill.checked });
  });

  const cornerSelect = document.getElementById("button-corner");
  const { buttonCorner = "top-right" } = await api.storage.local.get("buttonCorner");
  cornerSelect.value = buttonCorner;
  cornerSelect.addEventListener("change", () => {
    void api.storage.local.set({ buttonCorner: cornerSelect.value });
  });

  const hostname = tab?.url ? new URL(tab.url).hostname : null;
  if (!hostname) {
    overlay.disabled = true;
    return;
  }
  const { disabledSites = [] } = await api.storage.local.get("disabledSites");
  overlay.checked = !disabledSites.includes(hostname);
  overlay.addEventListener("change", async () => {
    const { disabledSites: current = [] } = await api.storage.local.get("disabledSites");
    const next = current.filter((site) => site !== hostname);
    if (!overlay.checked) next.push(hostname);
    await api.storage.local.set({ disabledSites: next });
  });
}

(async () => {
  const ver = document.getElementById("ext-version");
  if (ver) ver.textContent = `v${api.runtime.getManifest().version}`;
  const tab = await activeTab();
  await renderStatus();
  await renderQuickActions(tab);
  if (tab) await renderMediaList(tab);
  if (paired) await renderRecent();
  await wireToggles(tab);
})();
