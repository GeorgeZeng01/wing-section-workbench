/* UI shell: theme switching + dock resizing.
   No app logic here — app.js listens for "wss-themechange" to re-ink
   charts and the drawing. */

const THEME_KEY = "wss-theme";
const DOCK_KEY = "wss-dock-h";
const root = document.documentElement;

/* ---------------- theme ---------------- */

const ICON_SUN = `<svg viewBox="0 0 16 16" width="14" height="14" fill="none"
  stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true">
  <circle cx="8" cy="8" r="3.2"/>
  <path d="M8 1.2v1.8M8 13v1.8M1.2 8H3M13 8h1.8M3.2 3.2l1.3 1.3M11.5 11.5l1.3 1.3M12.8 3.2l-1.3 1.3M4.5 11.5l-1.3 1.3"/>
</svg>`;
const ICON_MOON = `<svg viewBox="0 0 16 16" width="14" height="14" fill="none"
  stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" aria-hidden="true">
  <path d="M13.2 9.8A5.6 5.6 0 0 1 6.2 2.8a5.6 5.6 0 1 0 7 7z"/>
</svg>`;

const themeBtn = document.getElementById("btn-theme");

function applyTheme(theme, { persist = true, announce = true } = {}) {
  root.dataset.theme = theme;
  if (persist) { try { localStorage.setItem(THEME_KEY, theme); } catch (e) {} }
  if (themeBtn) {
    themeBtn.innerHTML = theme === "dark" ? ICON_SUN : ICON_MOON;
    themeBtn.title = theme === "dark"
      ? "Switch to the light theme" : "Switch to the dark theme";
  }
  if (announce) {
    window.dispatchEvent(new CustomEvent("wss-themechange", { detail: { theme } }));
  }
}

applyTheme(root.dataset.theme || "light", { persist: false, announce: false });
themeBtn?.addEventListener("click", () =>
  applyTheme(root.dataset.theme === "dark" ? "light" : "dark"));

/* ---------------- dock resize ---------------- */

const divider = document.getElementById("dock-resize");
const dock = document.querySelector(".dock");

try {
  const saved = parseFloat(localStorage.getItem(DOCK_KEY));
  if (saved > 0) root.style.setProperty("--dock-h", saved + "px");
} catch (e) {}

let drag = null;

function clampDockH(h) {
  return Math.min(Math.max(h, 180), Math.round(window.innerHeight * 0.72));
}

divider?.addEventListener("pointerdown", (e) => {
  if (!dock) return;
  drag = { y: e.clientY, h: dock.getBoundingClientRect().height };
  divider.classList.add("dragging");
  divider.setPointerCapture(e.pointerId);
  e.preventDefault();
});
divider?.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const h = clampDockH(drag.h + (drag.y - e.clientY));
  root.style.setProperty("--dock-h", h + "px");
});
function endDrag() {
  if (!drag) return;
  drag = null;
  divider.classList.remove("dragging");
  const h = dock.getBoundingClientRect().height;
  try { localStorage.setItem(DOCK_KEY, String(Math.round(h))); } catch (e) {}
  window.dispatchEvent(new Event("resize")); // let charts re-measure
}
divider?.addEventListener("pointerup", endDrag);
divider?.addEventListener("pointercancel", endDrag);
divider?.addEventListener("dblclick", () => {
  root.style.removeProperty("--dock-h");
  try { localStorage.removeItem(DOCK_KEY); } catch (e) {}
  window.dispatchEvent(new Event("resize"));
});
