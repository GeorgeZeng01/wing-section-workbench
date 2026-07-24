/* Section viewport: renders the stack as a dimensioned technical drawing.
   World frame: stack units (main chord = 1), y up. Screen: SVG px, y down.
   Zoom (wheel), pan (drag), fit (button / double-click). */

const NS = "http://www.w3.org/2000/svg";
const SERIES = ["#3987e5", "#c98500", "#9085e9", "#199e70"];
const STEEL = "#8fa0c0";

function el(tag, attrs = {}, parent = null) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}

export class Viewport {
  constructor(svg) {
    this.svg = svg;
    this.geo = null;
    this.chordMm = 350;
    this.frame = "installed";
    this.showDims = true;
    this.showRules = true;
    this.xcp = null;           // center of pressure, stack units, or null
    this.view = null;          // {s, ox, oy}
    this._bindNav();
  }

  setData(geo, chordMm) {
    const hadData = !!this.geo;
    this.geo = geo;
    this.chordMm = chordMm;
    if (!hadData) this.fit(); else this.render();
  }

  setFrame(f) { this.frame = f; this.fit(); }
  setDims(v) { this.showDims = v; this.render(); }
  setRules(v) { this.showRules = v; this.render(); }
  setCp(x) { this.xcp = x; this.render(); }

  elements() {
    if (!this.geo) return [];
    return this.frame === "installed" ? this.geo.installed : this.geo.design;
  }

  bbox() {
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const e of this.elements()) {
      for (const [x, y] of e.coords) {
        if (x < x0) x0 = x; if (x > x1) x1 = x;
        if (y < y0) y0 = y; if (y > y1) y1 = y;
      }
    }
    if (this.frame === "installed") y0 = Math.min(y0, 0);
    return { x0, x1, y0, y1 };
  }

  fit() {
    if (!this.geo) return;
    const { x0, x1, y0, y1 } = this.bbox();
    const W = this.svg.clientWidth || 700, H = this.svg.clientHeight || 420;
    const s = Math.min((W * 0.62) / (x1 - x0 || 1), (H * 0.60) / (y1 - y0 || 1));
    this.view = {
      s,
      ox: (W - (x1 - x0) * s) / 2 - x0 * s,
      // bias upward a touch: the overall-length dimension needs headroom
      oy: (H + (y1 - y0) * s) / 2 + y0 * s - H * 0.03,
    };
    this.render();
  }

  P(x, y) { return [this.view.ox + x * this.view.s, this.view.oy - y * this.view.s]; }

  _bindNav() {
    const svg = this.svg;
    svg.addEventListener("wheel", (ev) => {
      if (!this.view) return;
      ev.preventDefault();
      const r = svg.getBoundingClientRect();
      const px = ev.clientX - r.left, py = ev.clientY - r.top;
      const f = ev.deltaY < 0 ? 1.15 : 1 / 1.15;
      const v = this.view;
      v.ox = px - (px - v.ox) * f;
      v.oy = py - (py - v.oy) * f;
      v.s *= f;
      this.render();
    }, { passive: false });
    let drag = null;
    svg.addEventListener("pointerdown", (ev) => {
      if (!this.view) return;
      drag = { x: ev.clientX, y: ev.clientY, ox: this.view.ox, oy: this.view.oy };
      svg.setPointerCapture(ev.pointerId);
    });
    svg.addEventListener("pointermove", (ev) => {
      if (!drag) return;
      this.view.ox = drag.ox + (ev.clientX - drag.x);
      this.view.oy = drag.oy + (ev.clientY - drag.y);
      this.render();
    });
    svg.addEventListener("pointerup", () => { drag = null; });
    svg.addEventListener("dblclick", () => this.fit());
    this._lastSize = [0, 0];
    new ResizeObserver(() => {
      if (!this.geo) return;
      const w = svg.clientWidth, h = svg.clientHeight;
      const [lw, lh] = this._lastSize;
      this._lastSize = [w, h];
      // container size changed (layout shift): refit so the drawing and its
      // ground line stay in view; tiny changes just re-render
      if (Math.abs(w - lw) > 4 || Math.abs(h - lh) > 4) this.fit();
      else if (this.view) this.render();
    }).observe(svg);
  }

  // ---- drawing helpers (px space) ----

  _arrow(g, x, y, dir) {   // dir: unit vector along the dimension line
    const n = [-dir[1], dir[0]];
    const p = (a, b) => `${a.toFixed(1)},${b.toFixed(1)}`;
    el("polygon", {
      points: [p(x, y),
               p(x - 7 * dir[0] + 2.2 * n[0], y - 7 * dir[1] + 2.2 * n[1]),
               p(x - 7 * dir[0] - 2.2 * n[0], y - 7 * dir[1] - 2.2 * n[1])].join(" "),
      fill: STEEL,
    }, g);
  }

  _dimLabel(g, x, y, text, anchor = "middle") {
    const t = el("text", { x, y, class: "dim-label", "text-anchor": anchor }, g);
    t.textContent = text;
    const w = text.length * 6.4 + 8;
    const bx = anchor === "middle" ? x - w / 2 : anchor === "start" ? x - 4 : x - w + 4;
    el("rect", { x: bx, y: y - 10, width: w, height: 14, rx: 3,
                 class: "dim-label-bg" }, g, );
    g.appendChild(t);  // re-append text above its backing rect
  }

  _dimV(g, xPx, yA, yB, label, side = 1) {
    const x = xPx + side * 34;
    el("line", { x1: xPx + side * 6, y1: yA, x2: x + side * 6, y2: yA, class: "dim-ext" }, g);
    el("line", { x1: xPx + side * 6, y1: yB, x2: x + side * 6, y2: yB, class: "dim-ext" }, g);
    el("line", { x1: x, y1: yA, x2: x, y2: yB, class: "dim-line" }, g);
    this._arrow(g, x, Math.min(yA, yB), [0, -1]);
    this._arrow(g, x, Math.max(yA, yB), [0, 1]);
    this._dimLabel(g, x + side * 8, (yA + yB) / 2 + 4, label,
                   side > 0 ? "start" : "end");
  }

  _dimH(g, xA, xB, yPx, label) {
    const y = yPx - 30;
    el("line", { x1: xA, y1: yPx - 6, x2: xA, y2: y - 6, class: "dim-ext" }, g);
    el("line", { x1: xB, y1: yPx - 6, x2: xB, y2: y - 6, class: "dim-ext" }, g);
    el("line", { x1: xA, y1: y, x2: xB, y2: y, class: "dim-line" }, g);
    this._arrow(g, Math.min(xA, xB), y, [-1, 0]);
    this._arrow(g, Math.max(xA, xB), y, [1, 0]);
    this._dimLabel(g, (xA + xB) / 2, y - 7, label);
  }

  _ruleLabel(svg, x, y, text, viol, anchor = "start") {
    const t = el("text", { x, y, class: `rule-label${viol ? " viol" : ""}`,
                           "text-anchor": anchor }, svg);
    t.textContent = text;
  }

  _drawRules(svg) {
    const r = this.geo.rules;
    const env = r.envelope || {};
    const mm = this.chordMm;
    const viol = {};                       // edge -> violation record
    for (const v of r.violations || []) viol[v.edge] = v;
    const { x0, x1, y1 } = this.bbox();
    const cls = (edge) => `rule-line${viol[edge] ? " viol" : ""}`;
    // box anchor: the stack's leading extent plus the user's offset — the
    // length rule constrains extent, not position, so the box just frames
    // the stack where it sits
    const xL = x0 + (env.x_offset_mm || 0) / mm;
    const len = env.max_length_mm != null ? env.max_length_mm / mm : null;
    const hTop = env.max_height_mm != null ? env.max_height_mm / mm : null;
    const clr = env.min_ground_clearance_mm != null
      ? env.min_ground_clearance_mm / mm : null;
    const xR = len != null ? xL + len : Math.max(x1, xL);
    const yTop = hTop != null ? hTop : y1 * 1.15;
    const padC = 14 / this.view.s;          // 14 px in world units
    const [pxL, pyG] = this.P(xL, 0);
    const [pxR] = this.P(xR, 0);
    const [, pyT] = this.P(0, yTop);
    if (len != null) {
      el("line", { x1: pxL, y1: pyG, x2: pxL, y2: pyT, class: cls("length") }, svg);
      el("line", { x1: pxR, y1: pyG, x2: pxR, y2: pyT, class: cls("length") }, svg);
      if (viol.length) {
        this._ruleLabel(svg, pxR + 6, (pyG + pyT) / 2,
                        `max length +${viol.length.by_mm.toFixed(1)} mm`, true);
      }
    }
    if (hTop != null) {
      const [hx0] = this.P(xL - padC, 0);
      const [hx1] = this.P(xR + padC, 0);
      el("line", { x1: hx0, y1: pyT, x2: hx1, y2: pyT, class: cls("top") }, svg);
      const v = viol.top;
      this._ruleLabel(svg, hx1 - 2, pyT - 5,
                      v ? `max height +${v.by_mm.toFixed(1)} mm`
                        : `${env.max_height_mm} mm`, !!v, "end");
    }
    if (clr != null) {
      const [cx0, cy] = this.P(xL - padC, clr);
      const [cx1] = this.P(xR + padC, clr);
      el("line", { x1: cx0, y1: cy, x2: cx1, y2: cy,
                   class: `${cls("bottom")} rule-clearance` }, svg);
      const v = viol.bottom;
      this._ruleLabel(svg, cx0 + 2, cy + 14,
                      v ? `min clearance −${v.by_mm.toFixed(1)} mm`
                        : `clearance ${env.min_ground_clearance_mm} mm`, !!v);
    }
    if (env.preset_name && (len != null || hTop != null)) {
      this._ruleLabel(svg, pxL + 4, pyT - 5, env.preset_name, false);
    }
  }

  render() {
    const svg = this.svg;
    svg.innerHTML = "";
    if (!this.geo || !this.view) return;
    const W = svg.clientWidth, H = svg.clientHeight;
    const els = this.elements();
    const mm = this.chordMm;

    const defs = el("defs", {}, svg);
    const pat = el("pattern", { id: "gnd-hatch", width: 9, height: 9,
                                patternUnits: "userSpaceOnUse",
                                patternTransform: "rotate(45)" }, defs);
    el("line", { x1: 0, y1: 0, x2: 0, y2: 9, stroke: "#3a4763",
                 "stroke-width": 1 }, pat);

    // ground
    if (this.frame === "installed") {
      const [, gy] = this.P(0, 0);
      if (gy > -40 && gy < H + 200) {
        el("rect", { x: 0, y: gy, width: W, height: Math.min(26, H - gy + 26),
                     fill: "url(#gnd-hatch)", opacity: 0.8 }, svg);
        el("line", { x1: 0, y1: gy, x2: W, y2: gy, class: "ground-line" }, svg);
      }
    }

    // rule envelope: light dashed box behind the elements, violated edges
    // in the warning color with the overshoot called out in mm
    if (this.frame === "installed" && this.showRules && this.geo.rules) {
      this._drawRules(svg);
    }

    // elements
    els.forEach((e, i) => {
      const c = SERIES[i % SERIES.length];
      const d = e.coords.map((p, k) => {
        const [x, y] = this.P(p[0], p[1]);
        return `${k ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
      }).join(" ") + " Z";
      el("path", { d, fill: c + "30", stroke: c, "stroke-width": 1.6,
                   "stroke-linejoin": "round" }, svg);
    });

    // chord line of main element (reference)
    if (els.length) {
      const main = els[0].coords;
      let iLe = 0;
      for (let i = 1; i < main.length; i++) {
        if (main[i][0] < main[iLe][0]) iLe = i;
      }
      const te = [(main[0][0] + main[main.length - 1][0]) / 2,
                  (main[0][1] + main[main.length - 1][1]) / 2];
      const [ax, ay] = this.P(main[iLe][0], main[iLe][1]);
      const [bx, by] = this.P(te[0], te[1]);
      el("line", { x1: ax, y1: ay, x2: bx, y2: by, class: "chord-line" }, svg);
    }

    if (this.showDims) {
      const g = el("g", {}, svg);
      const { x0, x1, y1 } = this.bbox();

      // overall length dimension (above the stack)
      const [lx, ty] = this.P(x0, y1);
      const [rx] = this.P(x1, y1);
      this._dimH(g, lx, rx, ty, `${(x1 - x0) * mm >= 100
        ? ((x1 - x0) * mm).toFixed(0) : ((x1 - x0) * mm).toFixed(1)} mm`);

      if (this.frame === "installed") {
        // ride height: lowest point of the stack to ground
        let low = null;
        for (const e of els) {
          for (const p of e.coords) if (!low || p[1] < low[1]) low = p;
        }
        if (low) {
          const [px, pyA] = this.P(low[0], low[1]);
          const [, pyB] = this.P(low[0], 0);
          this._dimV(g, px, pyA, pyB, `h ${ (low[1] * mm).toFixed(1)} mm`,
                     low[0] > (x0 + x1) / 2 ? 1 : -1);
        }
        // center of pressure marker on the ground line
        if (this.xcp != null) {
          const [cx, cy] = this.P(this.xcp, 0);
          el("line", { x1: cx, y1: cy - 7, x2: cx, y2: cy + 7,
                       class: "cp-mark" }, g);
          el("circle", { cx, cy, r: 4.5, class: "cp-mark", fill: "none" }, g);
          this._dimLabel(g, cx, cy + 22, `CP ${(this.xcp * mm).toFixed(0)} mm`);
        }
      }

      // slot callouts: leader from each flap's leading edge out to the right
      // margin, stacked so consecutive flaps never collide
      let flapNo = 0;
      const x1r = this.P(x1, 0)[0];
      els.forEach((e, i) => {
        if (e.slot_gap == null) return;
        let iLe = 0;
        for (let k = 1; k < e.coords.length; k++) {
          if (e.coords[k][0] < e.coords[iLe][0]) iLe = k;
        }
        const [px, py] = this.P(e.coords[iLe][0], e.coords[iLe][1]);
        const lx = Math.min(x1r + 46, (this.svg.clientWidth || W) - 168);
        const ly = py - 26 - 20 * flapNo++;
        el("polyline", { points: `${px},${py} ${lx - 8},${ly + 4} ${lx - 2},${ly + 4}`,
                         class: "leader" }, svg);
        this._dimLabel(svg, lx + 2, ly + 8,
                       `E${i + 1}  gap ${(e.slot_gap * 100).toFixed(1)}%  ·  ovl ` +
                       `${(e.slot_overlap * 100).toFixed(1)}%`, "start");
      });
      svg.appendChild(g);
    }

    // scale bar (bottom left): a tidy round-number bar
    const targetPx = 90;
    const mmPerPx = mm / this.view.s;
    const raw = targetPx * mmPerPx;
    const pow = Math.pow(10, Math.floor(Math.log10(raw)));
    let niceMm = pow;
    for (const k of [1, 2, 5, 10]) { if (pow * k >= raw) { niceMm = pow * k; break; } }
    const barPx = niceMm / mmPerPx;
    const by = H - 18, bx = W - 20 - barPx;
    el("line", { x1: bx, y1: by, x2: bx + barPx, y2: by, class: "scalebar" }, svg);
    el("line", { x1: bx, y1: by - 4, x2: bx, y2: by + 4, class: "scalebar" }, svg);
    el("line", { x1: bx + barPx, y1: by - 4, x2: bx + barPx, y2: by + 4,
                 class: "scalebar" }, svg);
    const t = el("text", { x: bx + barPx / 2, y: by - 7, class: "dim-label",
                           "text-anchor": "middle" }, svg);
    t.textContent = niceMm >= 1000 ? `${niceMm / 1000} m` : `${niceMm} mm`;
  }
}

export { SERIES };
