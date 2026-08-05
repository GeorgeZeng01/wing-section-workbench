/* Thin API client. All calls return parsed JSON or throw ApiError. */

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

/* A range violation arrives as the validator's LIST of error objects, one
   per offending field — it has to read as a sentence naming the field, not
   as a serialized array, because it is shown verbatim in a toast. */
function detailOf(j, fallback) {
  const d = j?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    const lines = d
      .map((e) => {
        if (!e || !e.msg) return null;
        const loc = Array.isArray(e.loc) ? e.loc[e.loc.length - 1] : null;
        return loc != null ? `${loc}: ${e.msg}` : String(e.msg);
      })
      .filter(Boolean);
    if (lines.length) return lines.join("; ");
  }
  return d == null ? fallback : JSON.stringify(d);
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
      detail = detailOf(await res.json(), res.statusText);
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
  ansysPresets: () => request("GET", "/api/ansys-presets"),
  ansysPresetsSave: (presets) =>
    request("PUT", "/api/ansys-presets", { presets }),
  pinnedDesigns: () => request("GET", "/api/pinned-designs"),
  // the first wrapper to carry the rev token: auto-mirrored pins race
  // across windows far more than manual preset saves do, and the client
  // resolves a 409 by re-GET + re-apply rather than by parsing the body
  // (detailOf flattens object details to a string)
  pinnedDesignsSave: (pins, rev) =>
    request("PUT", "/api/pinned-designs",
            rev == null ? { pins } : { pins, rev }),
  session: () => request("GET", "/api/session"),
  sessionSave: (state) => request("POST", "/api/session", { state }),
  exportSave: (fmt, config, options = {}) =>
    request("POST", `/api/export/${fmt}/save`, { config, ...options }),
  exportCfd: (config, meshSize) =>
    request("POST", "/api/export/cfd/save", { config, mesh_size: meshSize }),
  exportFluentMesh: (config, meshSize) =>
    request("POST", "/api/export/fluent-mesh/save",
            { config, mesh_size: meshSize }),
  // overrides: the same nine ANSYS 2D knobs the run endpoint takes, so a
  // bundle meshed for a hand check is the mesh the tab would build
  exportFluent2dMesh: (config, sizing, overrides = {}) =>
    request("POST", "/api/export/fluent2d-mesh/save",
            { config, sizing, ...overrides }),
  exportReveal: (path) => request("POST", "/api/export/reveal", { path }),
  ransAvailability: () => request("GET", "/api/rans/availability"),
  fluent2dAvailability: () => request("GET", "/api/fluent2d/availability"),
  ransCurrent: () => request("GET", "/api/rans/current"),
  // settings: the ANSYS 2D overrides (edge/first-layer/layers/growth,
  // domain extents, stage budgets). Only the fluent2d engine accepts
  // them; an absent key means "take the sizing recipe's value"
  ransStart: (config, meshSize, maxIters, nRanks = 1,
              engine = "openfoam", mesher = "fluent",
              conventions = "default", settings = {}) =>
    request("POST", "/api/rans/start",
            { config, mesh_size: meshSize, max_iters: maxIters,
              n_ranks: nRanks, engine, mesher, conventions, ...settings }),
  ransStatus: (id) => request("GET", `/api/rans/${id}`),
  ransCancel: (id) => request("POST", `/api/rans/${id}/cancel`),
  ransStop: (id) => request("POST", `/api/rans/${id}/stop`),
  ransExportFluent: (id) =>
    request("POST", `/api/rans/${id}/export/fluent`),
  ransQueueStart: (items, meshSize, maxIters = 10000, nRanks = 1,
                   maxConcurrent = 1, engine = "openfoam") =>
    request("POST", "/api/rans-queue/start",
            { items, mesh_size: meshSize, max_iters: maxIters,
              n_ranks: nRanks, max_concurrent: maxConcurrent, engine }),
  ransQueueCurrent: () => request("GET", "/api/rans-queue/current"),
  ransQueueCancel: () => request("POST", "/api/rans-queue/cancel"),
  // adjoint polish (final stage): seeds from a finished fluent2d run;
  // status/cancel ride the shared /api/rans/{id} endpoints
  polishStart: (runId, opts = {}) =>
    request("POST", "/api/polish/start", { run_id: runId, ...opts }),
  polishReverify: (id) => request("POST", `/api/polish/${id}/reverify`),
  polishExportDxf: (id) =>
    request("POST", `/api/polish/${id}/export/dxf`),
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
      detail = detailOf(await res.json(), res.statusText);
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
