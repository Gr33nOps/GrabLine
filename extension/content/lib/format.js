// GrabLine Connect - byte formatting, shared by the popup's media list and the
// on-page progress pill (both used to carry their own drifting copy).
(() => {
  function humanBytes(count) {
    if (!count) return "";
    const units = ["B", "KB", "MB", "GB"];
    let value = count;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
  }

  globalThis.grablineFormat = { humanBytes };
})();
