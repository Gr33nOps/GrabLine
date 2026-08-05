// GrabLine Connect - the one guarded path from a page or the popup to the
// background.
//
// api.runtime.sendMessage rejects with "Could not establish connection.
// Receiving end does not exist." whenever the background worker is asleep or the
// page has no receiver. Left unhandled inside an async click handler that
// becomes an unhandled rejection and the button never shows its check/cross.
// Everything that talks to the background goes through here, so a dropped
// message surfaces as a normal { type: "error" } reply instead of a throw.
(() => {
  const api = globalThis.browser ?? globalThis.chrome;
  globalThis.grablineSend = async (message) => {
    try {
      const reply = await api.runtime.sendMessage(message);
      return reply ?? { type: "error", message: "no reply from GrabLine" };
    } catch (error) {
      return { type: "error", message: error?.message ?? String(error) };
    }
  };
})();
