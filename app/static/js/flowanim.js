/* Animated flow view: particles advected through the SOLVED steady
   velocity field — the same gridded, interior-masked field the static
   PNG renders (one server-side code path), so the two views can never
   disagree. Particle motion is the local solved velocity; playback is
   deliberately slowed (real air crosses the frame in a fraction of a
   second) and the active slowdown is always stated in the caption.

   Interaction: Ctrl+wheel zooms about the cursor (plain wheel keeps
   scrolling the page), drag pans, double-click resets. The field data
   is SWAPPABLE (setField) while the view transform persists — the app
   uses this for level-of-detail: zoom deep into a full-domain view and
   it refetches a fine grid for just the visible window, so resolution
   follows the zoom. Trail rendering is preset-switchable (setTrail)
   between comet, long streaks and persistent streaklines; particle
   count is live-adjustable (setDensity). The backdrop the particles
   ride over is a mount option (opts.background: the velocity field,
   the Cp field, or plain chrome) — the app remounts to switch it,
   because the Cp backdrop needs a field the fetch must carry. */

/* chrome per app theme — dark is the studio's original look; light sits
   on the drafting theme without a heavy dark slab */
const CHROME = {
  dark: { bg: "#0c111c", panel: "#232c40", edge: "#93a3c2",
          particle: [255, 255, 255] },
  light: { bg: "#ffffff", panel: "#dfe4ee", edge: "#46536f",
           particle: [18, 24, 38] },
};

/* colormap stops (matplotlib): magma matches the studio's dark view,
   turbo is the rainbow Fluent users read natively, viridis in between;
   rdbu_r / coolwarm are the diverging ramps the static Cp views use
   (dark / light theme respectively) */
const CMAPS = {
  magma: [
    [0.001, 0.000, 0.014], [0.113, 0.065, 0.277], [0.317, 0.071, 0.485],
    [0.513, 0.148, 0.508], [0.716, 0.215, 0.475], [0.904, 0.320, 0.388],
    [0.987, 0.536, 0.382], [0.997, 0.770, 0.535], [0.987, 0.991, 0.750],
  ],
  viridis: [
    [0.267, 0.005, 0.329], [0.283, 0.141, 0.458], [0.254, 0.265, 0.530],
    [0.207, 0.372, 0.553], [0.164, 0.471, 0.558], [0.128, 0.567, 0.551],
    [0.135, 0.659, 0.518], [0.267, 0.749, 0.441], [0.478, 0.821, 0.318],
    [0.741, 0.873, 0.150], [0.993, 0.906, 0.144],
  ],
  turbo: [
    [0.190, 0.072, 0.232], [0.276, 0.408, 0.978], [0.150, 0.698, 0.926],
    [0.099, 0.897, 0.615], [0.451, 0.996, 0.309], [0.796, 0.928, 0.212],
    [0.985, 0.703, 0.226], [0.960, 0.412, 0.093], [0.783, 0.155, 0.021],
    [0.480, 0.016, 0.011],
  ],
  rdbu_r: [
    [0.020, 0.188, 0.380], [0.129, 0.400, 0.674], [0.262, 0.576, 0.765],
    [0.573, 0.773, 0.870], [0.819, 0.898, 0.941], [0.968, 0.968, 0.968],
    [0.992, 0.859, 0.780], [0.957, 0.647, 0.510], [0.839, 0.376, 0.302],
    [0.698, 0.094, 0.168], [0.404, 0.000, 0.121],
  ],
  coolwarm: [
    [0.230, 0.299, 0.754], [0.406, 0.537, 0.934], [0.602, 0.731, 0.999],
    [0.788, 0.845, 0.939], [0.867, 0.864, 0.863], [0.944, 0.734, 0.612],
    [0.957, 0.598, 0.477], [0.867, 0.379, 0.302], [0.706, 0.016, 0.150],
  ],
};

/* trail presets — fade is the per-frame erase alpha (lower = longer
   trails), alpha0/alphaSpan the stroke opacity ramp with local speed.
   persistent keeps long streaklines but with a deliberately dimmer
   tail: the old near-zero fade left week-old strokes at close to full
   ink and the view read as a solid smear */
const TRAILS = {
  comet: { fade: 0.055, maxAge: 6, alpha0: 0.14, alphaSpan: 0.6,
           width0: 0.7, widthSpan: 0.9 },
  long: { fade: 0.016, maxAge: 16, alpha0: 0.32, alphaSpan: 0.55,
          width0: 0.8, widthSpan: 0.8 },
  persistent: { fade: 0.006, maxAge: 45, alpha0: 0.4, alphaSpan: 0.45,
                width0: 0.9, widthSpan: 0.7 },
};

function makeColormap(name) {
  const stops = CMAPS[name] || CMAPS.magma;
  return (t) => {
    const x = Math.max(0, Math.min(1, t)) * (stops.length - 1);
    const i = Math.min(Math.floor(x), stops.length - 2), f = x - i;
    const a = stops[i], b = stops[i + 1];
    return [(a[0] + (b[0] - a[0]) * f) * 255,
            (a[1] + (b[1] - a[1]) * f) * 255,
            (a[2] + (b[2] - a[2]) * f) * 255];
  };
}

export function mountFlowAnim(host, data, opts = {}) {
  const bgC = host.querySelector("canvas[data-role=bg]");
  const ptC = host.querySelector("canvas[data-role=pt]");
  const bg = bgC.getContext("2d");
  const pt = ptC.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const theme = CHROME[opts.theme] || CHROME.dark;
  const [pr, pg, pb] = theme.particle;
  const bgMode = ["field", "cp", "plain"].includes(opts.background)
    ? opts.background : "field";
  // auto colormap follows the static views: sequential for velocity,
  // the theme's diverging ramp for Cp; an explicit choice wins in both
  const cmap = makeColormap(
    opts.cmap || (bgMode === "cp"
      ? (opts.theme === "light" ? "coolwarm" : "rdbu_r")
      : (opts.theme === "light" ? "turbo" : "magma")));
  let trail = { ...(TRAILS[opts.trail] || TRAILS.comet) };

  /* the BASE field defines the fitted view; setField swaps in detail
     windows without touching the transform, so zooming stays seamless */
  const base = data;
  let F = null;   // active field (base or a fetched detail window)

  function buildField(d) {
    const nx = d.nx, ny = d.ny;
    // umax also normalizes particle stroke styling, so the scale clamp
    // only redefines it when the clamp targets the velocity backdrop
    const umax = Math.max(
      (bgMode === "field" && opts.vmax) || base.umag_p99, 1e-6);
    let field = null;
    const vals = bgMode === "cp" ? d.cp
      : bgMode === "field" ? d.umag : null;
    if (vals) {
      field = document.createElement("canvas");
      field.width = nx; field.height = ny;
      const fctx = field.getContext("2d");
      const img = fctx.createImageData(nx, ny);
      // Cp maps symmetrically about 0 on the BASE field's range so
      // level-of-detail windows keep the mount's color scale
      const lim = Math.max(
        (bgMode === "cp" && opts.vmax) || base.cp_p98 || 1, 1e-6);
      const pf = parseInt(theme.panel.slice(1), 16);
      for (let j = 0; j < ny; j++) {
        for (let i = 0; i < nx; i++) {
          const s = vals[j * nx + i];
          const o = ((ny - 1 - j) * nx + i) * 4;
          if (s == null) {
            img.data[o] = (pf >> 16) & 255;
            img.data[o + 1] = (pf >> 8) & 255;
            img.data[o + 2] = pf & 255;
            img.data[o + 3] = 255;
          } else {
            const t = bgMode === "cp" ? (s / lim + 1) / 2 : s / umax;
            const [r, g, b] = cmap(t);
            img.data[o] = r; img.data[o + 1] = g; img.data[o + 2] = b;
            img.data[o + 3] = 255;
          }
        }
      }
      fctx.putImageData(img, 0, 0);
    }
    return { nx, ny, x0: d.x0, x1: d.x1, y0: d.y0, y1: d.y1,
             spanX: d.x1 - d.x0, spanY: d.y1 - d.y0,
             u: d.u, v: d.v, umax, canvas: field };
  }

  function sample(x, y) {
    const fx = ((x - F.x0) / F.spanX) * (F.nx - 1);
    const fy = ((y - F.y0) / F.spanY) * (F.ny - 1);
    if (fx < 0 || fy < 0 || fx > F.nx - 1 || fy > F.ny - 1) return null;
    const i = Math.min(Math.floor(fx), F.nx - 2);
    const j = Math.min(Math.floor(fy), F.ny - 2);
    const tx = fx - i, ty = fy - j;
    const c00 = j * F.nx + i, c10 = c00 + 1,
          c01 = c00 + F.nx, c11 = c01 + 1;
    const u = F.u, v = F.v;
    if (u[c00] == null || u[c10] == null || u[c01] == null
        || u[c11] == null) return null;
    const w00 = (1 - tx) * (1 - ty), w10 = tx * (1 - ty),
          w01 = (1 - tx) * ty, w11 = tx * ty;
    return [u[c00] * w00 + u[c10] * w10 + u[c01] * w01 + u[c11] * w11,
            v[c00] * w00 + v[c10] * w10 + v[c01] * w01 + v[c11] * w11];
  }

  /* view transform: world meters -> canvas px */
  let scale = 1, offX = 0, offY = 0, fitScale = 1;
  let cssW = 0, cssH = 0;
  function fit() {
    cssW = host.clientWidth;
    cssH = Math.max(180,
                    Math.round(cssW * (base.y1 - base.y0)
                               / (base.x1 - base.x0)));
    for (const c of [bgC, ptC]) {
      c.width = Math.round(cssW * dpr);
      c.height = Math.round(cssH * dpr);
      c.style.width = cssW + "px";
      c.style.height = cssH + "px";
    }
    scale = (cssW * dpr) / (base.x1 - base.x0);
    offX = -base.x0 * scale;
    offY = (cssH * dpr) + base.y0 * scale;
    fitScale = scale;
  }
  const sx = (x) => x * scale + offX;
  const sy = (y) => offY - y * scale;

  function drawBackground() {
    bg.setTransform(1, 0, 0, 1, 0, 0);
    bg.fillStyle = theme.bg;
    bg.fillRect(0, 0, bgC.width, bgC.height);
    if (F.canvas) {
      bg.imageSmoothingEnabled = true;
      bg.drawImage(F.canvas, sx(F.x0), sy(F.y1),
                   F.spanX * scale, F.spanY * scale);
    }
    for (const poly of base.polys) {
      bg.beginPath();
      poly.forEach(([px, py], k) =>
        k ? bg.lineTo(sx(px), sy(py)) : bg.moveTo(sx(px), sy(py)));
      bg.closePath();
      bg.fillStyle = theme.panel;
      bg.fill();
      bg.strokeStyle = theme.edge;
      bg.lineWidth = 1.2 * dpr;
      bg.stroke();
    }
    bg.strokeStyle = theme.edge;   // the ground
    bg.lineWidth = 1.6 * dpr;
    bg.beginPath();
    bg.moveTo(0, sy(0));
    bg.lineTo(bgC.width, sy(0));
    bg.stroke();
    pt.setTransform(1, 0, 0, 1, 0, 0);
    pt.clearRect(0, 0, ptC.width, ptC.height);
  }

  /* particle pool, respawned inside the ACTIVE field window; the count
     is a live setting (setDensity) — arrays are reallocated, and the
     orphaned trails of removed particles simply fade out */
  const clampN = (n) =>
    Math.max(200, Math.min(Math.round(+n) || 2200, 20000));
  let N = clampN(opts.density || 2200);
  let px = new Float32Array(N), py = new Float32Array(N),
      age = new Float32Array(N);
  function respawn(k) {
    px[k] = F.x0 + Math.random() * F.spanX;
    py[k] = F.y0 + Math.random() * F.spanY;
    age[k] = Math.random() * trail.maxAge * 0.7;
  }
  function respawnAll() {
    for (let k = 0; k < N; k++) respawn(k);
  }

  let slowdown = opts.slowdown || 20;
  let running = false, rafId = 0, last = 0;

  function step(now) {
    if (!running) return;
    rafId = requestAnimationFrame(step);
    if (host.offsetParent === null) return;   // tab hidden — skip work
    const dtWall = Math.min((now - last) / 1000, 0.05) || 0.016;
    last = now;
    const dt = dtWall / slowdown;
    pt.setTransform(1, 0, 0, 1, 0, 0);
    pt.globalCompositeOperation = "destination-out";
    pt.fillStyle = `rgba(0,0,0,${trail.fade})`;
    pt.fillRect(0, 0, ptC.width, ptC.height);
    pt.globalCompositeOperation = "source-over";
    pt.lineCap = "round";
    for (let k = 0; k < N; k++) {
      const vel = sample(px[k], py[k]);
      age[k] += dtWall;
      if (!vel || age[k] > trail.maxAge) { respawn(k); continue; }
      const nx2 = px[k] + vel[0] * dt, ny2 = py[k] + vel[1] * dt;
      const mag = Math.hypot(vel[0], vel[1]);
      const t = Math.min(mag / F.umax, 1);
      pt.strokeStyle = `rgba(${pr},${pg},${pb},` +
        `${trail.alpha0 + trail.alphaSpan * t})`;
      pt.lineWidth = (trail.width0 + trail.widthSpan * t) * dpr;
      pt.beginPath();
      pt.moveTo(sx(px[k]), sy(py[k]));
      pt.lineTo(sx(nx2), sy(ny2));
      pt.stroke();
      px[k] = nx2; py[k] = ny2;
    }
  }

  /* ---- view change notification (debounced) for level-of-detail ---- */
  let settleTimer = 0;
  function viewSettled() {
    clearTimeout(settleTimer);
    settleTimer = setTimeout(() => {
      if (!opts.onViewSettled) return;
      const xLo = (0 - offX) / scale;
      const xHi = (bgC.width - offX) / scale;
      const yHi = (offY - 0) / scale;
      const yLo = (offY - bgC.height) / scale;
      opts.onViewSettled({
        bbox: [xLo, yLo, xHi, yHi],
        zoom: scale / fitScale,
        pxPerCell: scale * (F.spanX / F.nx),
      });
    }, 450);
  }

  function resetView() {
    fit();
    drawBackground();
    viewSettled();
  }
  function applyZoom(factor, cx, cy) {
    const ns = Math.max(fitScale * 0.5,
                        Math.min(scale * factor, fitScale * 60));
    const f = ns / scale;
    offX = cx - (cx - offX) * f;
    offY = cy - (cy - offY) * f;
    scale = ns;
    drawBackground();
    viewSettled();
  }
  function onWheel(e) {
    // plain wheel scrolls the page past the panel; zoom is Ctrl+wheel
    // (the map-widget convention) so the view never hijacks scrolling
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    const r = host.getBoundingClientRect();
    applyZoom(Math.exp(-e.deltaY * 0.0016),
              (e.clientX - r.left) * dpr, (e.clientY - r.top) * dpr);
  }
  let drag = null;
  function onDown(e) {
    drag = { x: e.clientX, y: e.clientY };
    host.setPointerCapture?.(e.pointerId);
  }
  function onMove(e) {
    if (!drag) return;
    offX += (e.clientX - drag.x) * dpr;
    offY += (e.clientY - drag.y) * dpr;
    drag = { x: e.clientX, y: e.clientY };
    drawBackground();
    viewSettled();
  }
  const onUp = () => { drag = null; };
  const onDbl = () => resetView();
  host.addEventListener("wheel", onWheel, { passive: false });
  host.addEventListener("pointerdown", onDown);
  host.addEventListener("pointermove", onMove);
  host.addEventListener("pointerup", onUp);
  host.addEventListener("pointercancel", onUp);
  host.addEventListener("dblclick", onDbl);
  const ro = new ResizeObserver(() => { if (running) resetView(); });
  ro.observe(host);

  F = buildField(base);
  respawnAll();
  resetView();

  return {
    start() {
      if (running) return;
      running = true;
      last = performance.now();
      rafId = requestAnimationFrame(step);
    },
    stop() {
      running = false;
      cancelAnimationFrame(rafId);
    },
    setSlowdown(s) { slowdown = Math.max(1, s); },
    setTrail(name) {
      trail = { ...(TRAILS[name] || TRAILS.comet) };
    },
    setDensity(n) {
      N = clampN(n);
      px = new Float32Array(N);
      py = new Float32Array(N);
      age = new Float32Array(N);
      respawnAll();
    },
    setField(d) {
      // swap the sampled grid (a detail window or back to base) —
      // the view transform is untouched, so the zoom stays put
      F = buildField(d);
      respawnAll();
      drawBackground();
    },
    isBaseField() { return F && F.x0 === base.x0 && F.x1 === base.x1
                    && F.nx === base.nx; },
    baseData: base,
    resetView,
    snapshot() {
      const out = document.createElement("canvas");
      out.width = bgC.width; out.height = bgC.height;
      const o = out.getContext("2d");
      o.drawImage(bgC, 0, 0);
      o.drawImage(ptC, 0, 0);
      return out.toDataURL("image/png");
    },
    destroy() {
      this.stop();
      clearTimeout(settleTimer);
      ro.disconnect();
      host.removeEventListener("wheel", onWheel);
      host.removeEventListener("pointerdown", onDown);
      host.removeEventListener("pointermove", onMove);
      host.removeEventListener("pointerup", onUp);
      host.removeEventListener("pointercancel", onUp);
      host.removeEventListener("dblclick", onDbl);
    },
  };
}
