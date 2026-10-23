/* Mobile dashboard drawer: no dependencies, shared across all sidebar pages. */
(() => {
  "use strict";
  const sidebar = document.getElementById("dashboard-sidebar");
  const toggle = document.getElementById("mobile-menu-toggle");
  const closer = document.getElementById("mobile-menu-close");
  const backdrop = document.getElementById("mobile-nav-backdrop");
  if (!sidebar || !toggle || !closer || !backdrop) return;

  const isMobile = () => window.matchMedia("(max-width: 760px)").matches;
  let lastFocus = null;

  function closeMenu(restoreFocus = true) {
    if (!document.body.classList.contains("nav-open")) return;
    document.body.classList.remove("nav-open");
    toggle.setAttribute("aria-expanded", "false");
    if (restoreFocus && isMobile()) (lastFocus || toggle).focus();
    lastFocus = null;
  }

  function openMenu() {
    if (!isMobile() || document.body.classList.contains("guest-dashboard")) return;
    lastFocus = document.activeElement;
    document.body.classList.add("nav-open");
    toggle.setAttribute("aria-expanded", "true");
    closer.focus();
  }

  toggle.addEventListener("click", () =>
    document.body.classList.contains("nav-open") ? closeMenu() : openMenu()
  );
  closer.addEventListener("click", () => closeMenu());
  backdrop.addEventListener("click", () => closeMenu());
  sidebar.addEventListener("click", event => {
    if (event.target.closest("a[href]")) closeMenu(false);
  });

  document.addEventListener("keydown", event => {
    if (!document.body.classList.contains("nav-open")) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...sidebar.querySelectorAll(
      'a[href], button:not([disabled]), summary, input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )].filter(el => el.getClientRects().length);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  window.addEventListener("resize", () => {
    if (!isMobile()) closeMenu(false);
  });
})();
