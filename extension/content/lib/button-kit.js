// GrabLine Connect - the shared hover-button chrome.
//
// Both the generic overlay (content/overlay.js) and the site-module factory
// (content/sites/button.js) draw the same floating download button: the
// GrabLine logo in a rounded shadow-root button that swaps to a check/cross
// after a grab. Those primitives - the icons, the logo, the accent/status
// colours, the corner positioning, the click feedback - lived in both files and
// had drifted (a 16 vs 17px icon, a 6 vs 8px inset). One definition here.
//
// Each caller keeps its own show/hide behaviour: the overlay follows the media
// every frame, the site button dwells and can open a quality panel. Those are
// genuinely different, not duplicated, so they stay put.
(() => {
  const SVGNS = "http://www.w3.org/2000/svg";
  const ICON = {
    download: ["M8 2v8", "M5 7l3 3 3-3", "M3 13h10"],
    check: ["M3 8.5L6.5 12 13 4"],
    error: ["M4 4l8 8", "M12 4l-8 8"],
  };
  // Match the app's accent + status colours (design.py).
  const COLORS = { accent: "#0170fd", ok: "#1f9d55", warn: "#cf222e" };
  const CORNER_INSET = 8; // gap between the button and the media's edge

  function iconSvg(paths, size = 16) {
    const el = document.createElementNS(SVGNS, "svg");
    const attrs = {
      width: String(size),
      height: String(size),
      viewBox: "0 0 16 16",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "1.5",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
    };
    for (const [name, value] of Object.entries(attrs)) el.setAttribute(name, value);
    for (const d of paths) {
      const path = document.createElementNS(SVGNS, "path");
      path.setAttribute("d", d);
      el.appendChild(path);
    }
    return el;
  }

  // The button wears the GrabLine logo; feedback swaps in a check/cross.
  function logoImg() {
    const api = globalThis.browser ?? globalThis.chrome;
    const img = document.createElement("img");
    img.src = api.runtime.getURL("icons/icon48.png");
    img.alt = "";
    img.style.cssText = "width:100%;height:100%;border-radius:8px;display:block;";
    return img;
  }

  // A shadow-hosted button, sized by the caller (the overlay uses a larger
  // button on full <video>s than the site modules use on grid thumbnails).
  // Returns the pieces; the caller attaches host where it wants and adds any
  // extra shadow content (the quality panel) itself.
  function createButton(size) {
    const host = document.createElement("div");
    // Host must never eat page clicks: pointer-events none on the wrapper,
    // auto only on the actual 34px button. Without this, a mis-sized host can
    // sit over the page and feel like "every click starts a download".
    host.style.cssText = "all: initial; position: fixed; z-index: 2147483647; pointer-events: none;";
    const shadow = host.attachShadow({ mode: "closed" });
    const button = document.createElement("button");
    button.replaceChildren(logoImg());
    button.title = "Download with GrabLine";
    button.style.cssText = [
      "position: fixed",
      "z-index: 2147483647",
      "display: none",
      "align-items: center",
      "justify-content: center",
      `width: ${size}px`,
      `height: ${size}px`,
      "padding: 0",
      "border: none",
      "border-radius: 8px",
      "background: transparent",
      "color: #fff",
      "cursor: pointer",
      "pointer-events: auto",
      "box-shadow: 0 2px 8px rgba(0,0,0,.4)",
    ].join(";");
    shadow.appendChild(button);
    return { host, shadow, button };
  }

  // Reset the button to its resting logo look (after a feedback swap, or when
  // shown for a fresh target).
  function resetButton(button) {
    button.style.background = "transparent";
    button.style.padding = "0";
    button.replaceChildren(logoImg());
  }

  // Where the button sits for a media element's box, clamped to the viewport.
  // Pure - unit-tested (test/button_geometry_test.js).
  function placeInCorner(rect, size, corner, viewport) {
    const left =
      corner.endsWith("left") ? rect.left + CORNER_INSET : rect.right - size - CORNER_INSET;
    const top =
      corner.startsWith("bottom") ? rect.bottom - size - CORNER_INSET : rect.top + CORNER_INSET;
    return {
      left: Math.min(Math.max(4, left), viewport.width - size - 4),
      top: Math.min(Math.max(4, top), viewport.height - size - 4),
    };
  }

  // The check/cross flash after a grab, then hand back to the caller's hide.
  function showFeedback(button, reply, onDone) {
    const failed = reply?.type === "error";
    button.replaceChildren(iconSvg(failed ? ICON.error : ICON.check));
    button.style.background = failed ? COLORS.warn : COLORS.ok;
    button.style.padding = "";
    setTimeout(onDone, 900);
  }

  // The user's chosen corner (popup setting), kept live. Returns a getter and
  // calls back on change so the caller can reposition.
  function watchCorner(onChange) {
    const api = globalThis.browser ?? globalThis.chrome;
    let corner = "top-right";
    api.storage.local.get("buttonCorner").then(({ buttonCorner = "top-right" }) => {
      corner = buttonCorner;
      onChange?.(corner);
    });
    api.storage.onChanged.addListener((changes, area) => {
      if (area === "local" && changes.buttonCorner) {
        corner = changes.buttonCorner.newValue ?? "top-right";
        onChange?.(corner);
      }
    });
    return () => corner;
  }

  // Like Element.closest, but walks out of open Shadow DOM hosts. Reddit's
  // shreddit-player (and similar custom players) keep <video> inside a
  // shadow root; a plain closest() from that video never reaches the post.
  function closestDeep(start, selector) {
    let node = start instanceof Element ? start : null;
    while (node) {
      if (node.matches?.(selector)) return node;
      if (node.assignedSlot) {
        node = node.assignedSlot;
        continue;
      }
      if (node.parentElement) {
        node = node.parentElement;
        continue;
      }
      const root = node.getRootNode?.();
      if (root instanceof ShadowRoot) {
        node = root.host;
        continue;
      }
      break;
    }
    return null;
  }

  // First <video>/<audio> under an open shadow tree (nested hosts included).
  function mediaInOpenShadow(root, depth = 0) {
    if (!root || depth > 8) return null;
    const direct = root.querySelector?.("video, audio");
    if (direct instanceof HTMLMediaElement) return direct;
    for (const el of root.querySelectorAll?.("*") ?? []) {
      if (el.shadowRoot) {
        const hit = mediaInOpenShadow(el.shadowRoot, depth + 1);
        if (hit) return hit;
      }
    }
    return null;
  }

  globalThis.grablineButtonKit = {
    ICON,
    COLORS,
    iconSvg,
    logoImg,
    createButton,
    resetButton,
    placeInCorner,
    showFeedback,
    watchCorner,
    closestDeep,
    mediaInOpenShadow,
  };
})();
