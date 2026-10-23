/* Gentle iPhone/iPad Home Screen suggestion. Safari does not allow a native install prompt. */
(() => {
  "use strict";

  const prompt = document.getElementById("ios-install-prompt");
  if (!prompt) return;

  const ua = navigator.userAgent || "";
  const isIPadDesktop = navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1;
  const isIOS = /iPad|iPhone|iPod/i.test(ua) || isIPadDesktop;
  if (!isIOS) return;

  const isInstalled = () =>
    navigator.standalone === true ||
    (typeof window.matchMedia === "function" &&
      window.matchMedia("(display-mode: standalone)").matches);
  if (isInstalled()) return;

  const isSafari = /Safari/i.test(ua) &&
    !/CriOS|FxiOS|EdgiOS|OPiOS|DuckDuckGo|GSA/i.test(ua);
  const note = document.getElementById("ios-install-safari-only");
  if (note) note.hidden = isSafari;

  // Dismissal is optional and only silences the suggestion for 7 days.
  const key = "aureolis:ios-home-screen-dismissed-at";
  const sevenDays = 7 * 24 * 60 * 60 * 1000;
  try {
    const lastDismissed = Number(localStorage.getItem(key));
    if (Number.isFinite(lastDismissed) && lastDismissed > 0 &&
        Date.now() - lastDismissed < sevenDays) return;
  } catch (_) {
    // Private browsing or a restricted storage environment is fine.
  }

  function dismiss() {
    prompt.hidden = true;
    try { localStorage.setItem(key, String(Date.now())); } catch (_) {}
  }

  for (const id of ["ios-install-close", "ios-install-later", "ios-install-done"]) {
    document.getElementById(id)?.addEventListener("click", dismiss);
  }

  prompt.hidden = false;

  // If a browser switches to standalone display, remove the banner.
  if (typeof window.matchMedia === "function") {
    const mode = window.matchMedia("(display-mode: standalone)");
    const onModeChange = () => { if (isInstalled()) prompt.hidden = true; };
    if (mode.addEventListener) mode.addEventListener("change", onModeChange);
    else if (mode.addListener) mode.addListener(onModeChange);
  }
})();
