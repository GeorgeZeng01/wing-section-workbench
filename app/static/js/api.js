/* Thin API client. All calls return parsed JSON or throw ApiError. */

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* keep statusText */ }
    throw new ApiError(detail, res.status);
  }
  return res.json();
}

export const api = {
  health: () => request("GET", "/api/health"),
  airfoils: (q, limit = 60) =>
    request("GET", `/api/airfoils?q=${encodeURIComponent(q)}&limit=${limit}`),
  airfoil: (spec) => request("GET", `/api/airfoil?spec=${encodeURIComponent(spec)}`),
  uploadAirfoil: (name, datText) =>
    request("POST", "/api/airfoils/upload", { name, dat_text: datText }),
  geometry: (config) => request("POST", "/api/geometry", { config }),
  analyze: (config) => request("POST", "/api/analyze", { config }),
  polar: (body) => request("POST", "/api/polar", body),
  optimize: (config, options) => request("POST", "/api/optimize", { config, options }),
  optimizeCurrent: () => request("GET", "/api/optimize/current"),
  optimizeStatus: (id) => request("GET", `/api/optimize/${id}`),
  optimizeCancel: (id) => request("POST", `/api/optimize/${id}/cancel`),
  screen: (body) => request("POST", "/api/screen", body),
  sweep: (config, variable, values) =>
    request("POST", "/api/sweep", { config, variable, values }),
  presets: () => request("GET", "/api/presets"),
  rulePresets: () => request("GET", "/api/rule-presets"),
  rulePresetsSave: (presets) => request("PUT", "/api/rule-presets", { presets }),
  session: () => request("GET", "/api/session"),
  sessionSave: (state) => request("POST", "/api/session", { state }),
  exportSave: (fmt, config, options = {}) =>
    request("POST", `/api/export/${fmt}/save`, { config, ...options }),
  exportCfd: (config, meshSize) =>
    request("POST", "/api/export/cfd/save", { config, mesh_size: meshSize }),
  exportReveal: (path) => request("POST", "/api/export/reveal", { path }),
  ransAvailability: () => request("GET", "/api/rans/availability"),
  ransCurrent: () => request("GET", "/api/rans/current"),
  ransStart: (config, meshSize, maxIters) =>
    request("POST", "/api/rans/start",
            { config, mesh_size: meshSize, max_iters: maxIters }),
  ransStatus: (id) => request("GET", `/api/rans/${id}`),
  ransCancel: (id) => request("POST", `/api/rans/${id}/cancel`),
  ransStop: (id) => request("POST", `/api/rans/${id}/stop`),
  ransQueueStart: (items, meshSize, maxIters = 10000) =>
    request("POST", "/api/rans-queue/start",
            { items, mesh_size: meshSize, max_iters: maxIters }),
  ransQueueCurrent: () => request("GET", "/api/rans-queue/current"),
  ransQueueCancel: () => request("POST", "/api/rans-queue/cancel"),
};

export async function downloadExport(fmt, config, options = {}) {
  const res = await fetch(`/api/export/${fmt}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config, ...options }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* keep statusText */ }
    throw new ApiError(detail, res.status);
  }
  const blob = await res.blob();
  const cd = res.headers.get("Content-Disposition") || "";
  const m = cd.match(/filename="([^"]+)"/);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = m ? m[1] : `export.${fmt}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
