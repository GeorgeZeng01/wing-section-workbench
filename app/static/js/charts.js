/* Minimal SVG chart engine: line charts with hairline grid, crosshair
   tooltip, legend, optional target line. No dependencies. */

const NS = "http://www.w3.org/2000/svg";

function el(tag, attrs = {}, parent = null) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}

function niceTicks(lo, hi, n = 5) {
  if (!isFinite(lo) || !isFinite(hi)) return [0, 1];
  if (lo === hi) { lo -= 1; hi += 1; }
  const span = hi - lo;
  const step0 = Math.pow(10, Math.floor(Math.log10(span / n)));
  let step = step0;
  for (const m of [1, 2, 2.5, 5, 10]) {
    if (span / (step0 * m) <= n) { step = step0 * m; break; }
  }
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) {
    ticks.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  }
  return ticks;
}

function fmt(v) {
  const a = Math.abs(v);
  if (a >= 10000) return v.toExponential(1);
  if (a >= 100) return v.toFixed(0);
  if (a >= 1) return +v.toFixed(2) + "";
  if (a === 0) return "0";
  if (a >= 0.01) return +v.toFixed(3) + "";
  return v.toExponential(1);
}

/**
 * Render a line chart.
 * spec: {
 *   series: [{name, color, x: [], y: [], dash?, markers?}],
 *   xLabel, yLabel, invertY?, targetY?, targetLabel?, height?, logY?,
 *   xBands?: [{from, to, color, label?}]   // shaded x-ranges behind the data
 * }
 */
export function lineChart(container, spec) {
  container.innerHTML = "";
  const W = Math.max(container.clientWidth || 480, 280);
  const H = spec.height || Math.max(container.clientHeight || 220, 180);
  const m = { t: 14, r: 14, b: 34, l: 52 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;

  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H,
                          class: "chart", role: "img" }, container);

  const xs = spec.series.flatMap(s => s.x).filter(Number.isFinite);
  let ys = spec.series.flatMap(s => s.y).filter(Number.isFinite);
  if (spec.targetY != null) ys = ys.concat([spec.targetY]);
  if (!xs.length || !ys.length) {
    el("text", { x: W / 2, y: H / 2, class: "chart-empty",
                 "text-anchor": "middle" }, svg).textContent = "no data";
    return;
  }
  const logY = !!spec.logY;
  const tf = logY ? (v => Math.log10(Math.max(v, 1e-12))) : (v => v);
  ys = ys.map(tf);
  let x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const ypad = (y1 - y0) * 0.07 || 0.5;
  y0 -= ypad; y1 += ypad;
  if (x0 === x1) { x0 -= 1; x1 += 1; }

  const X = v => m.l + ((v - x0) / (x1 - x0)) * iw;
  const Y = spec.invertY
    ? (v => m.t + ((tf(v) - y0) / (y1 - y0)) * ih)
    : (v => m.t + ih - ((tf(v) - y0) / (y1 - y0)) * ih);

  // shaded x-bands first, so grid, series and labels draw over them
  for (const b of spec.xBands || []) {
    const bx0 = Math.max(Math.min(b.from, b.to), x0);
    const bx1 = Math.min(Math.max(b.from, b.to), x1);
    if (!(bx1 > bx0)) continue;
    el("rect", { x: X(bx0), y: m.t, width: X(bx1) - X(bx0), height: ih,
                 fill: b.color, "fill-opacity": 0.10 }, svg);
    if (b.label) {
      el("text", { x: (X(bx0) + X(bx1)) / 2, y: m.t + 10, class: "tick",
                   "text-anchor": "middle", fill: b.color,
                   "fill-opacity": 0.9 }, svg).textContent = b.label;
    }
  }

  // grid + ticks
  const gx = niceTicks(x0, x1, 6), gy = niceTicks(y0, y1, 5);
  for (const v of gy) {
    const y = spec.invertY ? m.t + ((v - y0) / (y1 - y0)) * ih
                           : m.t + ih - ((v - y0) / (y1 - y0)) * ih;
    el("line", { x1: m.l, x2: m.l + iw, y1: y, y2: y, class: "grid" }, svg);
    el("text", { x: m.l - 7, y: y + 3.5, class: "tick", "text-anchor": "end" },
       svg).textContent = fmt(logY ? Math.pow(10, v) : v);
  }
  for (const v of gx) {
    const x = X(v);
    el("line", { x1: x, x2: x, y1: m.t, y2: m.t + ih, class: "grid" }, svg);
    el("text", { x, y: m.t + ih + 15, class: "tick", "text-anchor": "middle" },
       svg).textContent = fmt(v);
  }
  el("line", { x1: m.l, x2: m.l + iw, y1: m.t + ih, y2: m.t + ih,
               class: "axis" }, svg);
  el("line", { x1: m.l, x2: m.l, y1: m.t, y2: m.t + ih, class: "axis" }, svg);
  if (spec.xLabel) {
    el("text", { x: m.l + iw / 2, y: H - 4, class: "axis-label",
                 "text-anchor": "middle" }, svg).textContent = spec.xLabel;
  }
  if (spec.yLabel) {
    const t = el("text", { x: 12, y: m.t + ih / 2, class: "axis-label",
                           "text-anchor": "middle",
                           transform: `rotate(-90 12 ${m.t + ih / 2})` }, svg);
    t.textContent = spec.yLabel;
  }

  // target line
  if (spec.targetY != null) {
    const y = Y(spec.targetY);
    el("line", { x1: m.l, x2: m.l + iw, y1: y, y2: y, class: "target-line" }, svg);
    el("text", { x: m.l + iw - 4, y: y - 4, class: "target-label",
                 "text-anchor": "end" }, svg).textContent =
      spec.targetLabel || `target ${fmt(spec.targetY)}`;
  }

  // series
  for (const s of spec.series) {
    const pts = s.x.map((x, i) => [x, s.y[i]])
      .filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]));
    if (!pts.length) continue;
    if (s.markers) {
      pts.forEach(([px, py], pi) => {
        const fill = (s.pointColors && s.pointColors[pi]) || s.color;
        const c = el("circle", { cx: X(px), cy: Y(py),
                                 r: s.onPointClick ? 3.5 : 2.6, fill,
                                 "fill-opacity": 0.9 }, svg);
        if (s.onPointClick) {
          // generous invisible hit area — 3.5 px is a hover target, not
          // a click target
          const hit = el("circle", { cx: X(px), cy: Y(py), r: 9,
                                     fill: "transparent",
                                     style: "cursor:pointer" }, svg);
          for (const node of (c && [c, hit]) || []) {
            node.addEventListener("click", () => s.onPointClick(pi));
            node.style.cursor = "pointer";
          }
        }
      });
    }
    if (s.markers !== "only") {
      const d = pts.map((p, i) =>
        `${i ? "L" : "M"}${X(p[0]).toFixed(1)} ${Y(p[1]).toFixed(1)}`).join(" ");
      el("path", { d, fill: "none", stroke: s.color, "stroke-width": 2,
                   "stroke-linejoin": "round",
                   ...(s.dash ? { "stroke-dasharray": s.dash } : {}) }, svg);
    }
  }

  // legend (only for >= 2 series)
  if (spec.series.length >= 2) {
    let lx = m.l + 2;
    for (const s of spec.series) {
      const g = el("g", {}, svg);
      el("rect", { x: lx, y: 2, width: 10, height: 3, rx: 1.5,
                   fill: s.color }, g);
      const t = el("text", { x: lx + 14, y: 7, class: "legend-t" }, g);
      t.textContent = s.name;
      lx += 22 + s.name.length * 5.6;
    }
  }

  // hover crosshair + tooltip — decoration only: it must never swallow
  // clicks meant for the (clickable) data points beneath it
  const hover = el("g", { style: "display:none",
                          "pointer-events": "none" }, svg);
  const cross = el("line", { y1: m.t, y2: m.t + ih, class: "crosshair" }, hover);
  const tipG = document.createElement("div");
  tipG.className = "chart-tip";
  tipG.style.display = "none";
  container.style.position = "relative";
  container.appendChild(tipG);
  const dots = spec.series.map(s =>
    el("circle", { r: 3.5, fill: s.color, stroke: "var(--surface-1)",
                   "stroke-width": 1.5 }, hover));

  svg.addEventListener("pointermove", (ev) => {
    const r = svg.getBoundingClientRect();
    const px = (ev.clientX - r.left) * (W / r.width);
    if (px < m.l || px > m.l + iw) { hover.style.display = "none";
      tipG.style.display = "none"; return; }
    const xv = x0 + ((px - m.l) / iw) * (x1 - x0);
    hover.style.display = "";
    let rows = [];
    spec.series.forEach((s, si) => {
      if (!s.x.length) { dots[si].style.display = "none"; return; }
      let bi = 0, bd = Infinity;
      for (let i = 0; i < s.x.length; i++) {
        const d = Math.abs(s.x[i] - xv);
        if (d < bd) { bd = d; bi = i; }
      }
      const sx = s.x[bi], sy = s.y[bi];
      if (!Number.isFinite(sy)) { dots[si].style.display = "none"; return; }
      dots[si].style.display = "";
      dots[si].setAttribute("cx", X(sx));
      dots[si].setAttribute("cy", Y(sy));
      rows.push(`<span class="tip-swatch" style="background:${s.color}"></span>` +
                `${s.name}&nbsp;<b>${fmt(sy)}</b>`);
      cross.setAttribute("x1", X(sx));
      cross.setAttribute("x2", X(sx));
    });
    tipG.innerHTML = `<div class="tip-x">${spec.xLabel || "x"} = ${fmt(xv)}</div>` +
                     rows.map(r => `<div>${r}</div>`).join("");
    tipG.style.display = "";
    const cx = (ev.clientX - r.left);
    tipG.style.left = Math.min(cx + 14, r.width - 150) + "px";
    tipG.style.top = "8px";
  });
  svg.addEventListener("pointerleave", () => {
    hover.style.display = "none";
    tipG.style.display = "none";
  });
}

/** Small airfoil outline preview. */
export function airfoilPreview(container, coords, color = null) {
  color = color ||
    getComputedStyle(document.documentElement).getPropertyValue("--steel").trim() ||
    "#9AA5BC";
  container.innerHTML = "";
  if (!coords || !coords.length) return;
  const xs = coords.map(p => p[0]), ys = coords.map(p => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(...ys), y1 = Math.max(...ys);
  const W = 120, H = 34, pad = 3;
  const sc = Math.min((W - 2 * pad) / (x1 - x0 || 1), (H - 2 * pad) / (y1 - y0 || 1));
  const ox = (W - (x1 - x0) * sc) / 2, oy = (H - (y1 - y0) * sc) / 2;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H },
                 container);
  const d = coords.map((p, i) =>
    `${i ? "L" : "M"}${(ox + (p[0] - x0) * sc).toFixed(1)} ` +
    `${(H - oy - (p[1] - y0) * sc).toFixed(1)}`).join(" ") + " Z";
  el("path", { d, fill: color + "22", stroke: color, "stroke-width": 1 }, svg);
}
