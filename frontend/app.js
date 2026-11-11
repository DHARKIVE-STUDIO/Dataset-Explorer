// Dataset Explorer frontend. No build step. All server-provided text goes through esc().
const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

const PAGE = 50;
const NULL_OPS = new Set(["is_null", "not_null"]);
const OPS_BY_TYPE = {
  numeric: ["eq", "neq", "gt", "gte", "lt", "lte", "is_null", "not_null"],
  datetime: ["eq", "neq", "gt", "gte", "lt", "lte", "is_null", "not_null"],
  text: ["contains", "eq", "neq", "is_null", "not_null"],
  categorical: ["eq", "neq", "contains", "is_null", "not_null"],
  boolean: ["eq", "neq", "is_null", "not_null"],
  empty: ["is_null", "not_null"],
};
const OP_LABELS = {
  eq: "equals", neq: "not equals", gt: "greater than", gte: "at least",
  lt: "less than", lte: "at most", contains: "contains", is_null: "is empty", not_null: "is not empty",
};

const state = {
  id: null, meta: null, profile: null, types: {},
  page: 0, total: 0, sortBy: null, sortDir: "asc", filters: [],
  chart: null, statsLoaded: false, suggestions: [],
};

async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(path, options);
  } catch (_) {
    throw new Error("Can't reach the server. Check that it is running.");
  }
  let body = null;
  try { body = await res.json(); } catch (_) { /* non-JSON error page */ }
  if (!res.ok) {
    throw new Error(body?.error?.message || `Request failed with HTTP ${res.status}.`);
  }
  return body;
}

function showBanner(message, kind = "error") {
  const b = $("#banner");
  b.textContent = message;
  b.className = `banner ${kind}`;
  b.hidden = false;
}
function clearBanner() { $("#banner").hidden = true; }

function setInline(sel, message) {
  const el = $(sel);
  el.textContent = message || "";
  el.hidden = !message;
}

function fmt(v) {
  if (typeof v !== "number") return esc(v);
  return Number.isInteger(v) ? v.toLocaleString() : esc(Number(v.toPrecision(6)).toString());
}

// ---------- Datasets list and upload ----------

async function loadDatasets() {
  const ul = $("#dataset-list");
  try {
    const { datasets } = await api("/api/datasets");
    if (!datasets.length) {
      ul.innerHTML = `<li class="muted small">No datasets yet.</li>`;
      return;
    }
    ul.innerHTML = datasets.map((d) => `
      <li><button class="ds-item ${d.id === state.id ? "active" : ""}" data-id="${esc(d.id)}">
        <span>${esc(d.name)}</span>
        <small>${d.row_count.toLocaleString()} rows · ${d.column_count} columns</small>
      </button></li>`).join("");
  } catch (err) {
    ul.innerHTML = `<li class="inline-error">${esc(err.message)}</li>`;
  }
}

async function uploadFile(file) {
  const status = $("#upload-status");
  status.hidden = false;
  status.className = "status";
  status.textContent = `Uploading ${file.name}...`;
  const form = new FormData();
  form.append("file", file);
  try {
    const meta = await api("/api/datasets", { method: "POST", body: form });
    status.textContent = `Saved ${meta.name}: ${meta.row_count.toLocaleString()} rows, ${meta.column_count} columns.`;
    await loadDatasets();
    await openDataset(meta.id);
  } catch (err) {
    status.className = "status error";
    status.textContent = err.message;
  }
}

function setupUpload() {
  const zone = $("#dropzone");
  const input = $("#file-input");
  input.addEventListener("change", () => input.files[0] && uploadFile(input.files[0]));
  ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => {
    e.preventDefault(); zone.classList.add("hover");
  }));
  ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, (e) => {
    e.preventDefault(); zone.classList.remove("hover");
  }));
  zone.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files[0];
    if (file) uploadFile(file);
  });
  $("#dataset-list").addEventListener("click", (e) => {
    const btn = e.target.closest(".ds-item");
    if (btn) openDataset(btn.dataset.id);
  });
}

// ---------- Opening a dataset ----------

async function openDataset(id) {
  clearBanner();
  try {
    const [meta, profile] = await Promise.all([
      api(`/api/datasets/${encodeURIComponent(id)}`),
      api(`/api/datasets/${encodeURIComponent(id)}/profile`),
    ]);
    Object.assign(state, {
      id, meta, profile, page: 0, sortBy: null, sortDir: "asc",
      filters: [], statsLoaded: false, suggestions: [],
    });
    state.types = Object.fromEntries(profile.columns.map((c) => [c.name, c.type]));

    $("#empty-state").hidden = true;
    $("#dataset-view").hidden = false;
    $("#ds-name").textContent = meta.name;
    $("#ds-meta").textContent =
      `${meta.row_count.toLocaleString()} rows · ${meta.column_count} columns · ` +
      `${(meta.size_bytes / 1024).toFixed(1)} KB · uploaded ${new Date(meta.created_at).toLocaleString()}`;

    $("#filters").innerHTML = "";
    $("#insights-out").innerHTML = "";
    $("#ask-out").innerHTML = "";
    $("#ask-input").value = "";
    renderProfile();
    populateChartControls();
    addFilterRow();
    showTab("preview");
    loadDatasets();
  } catch (err) {
    showBanner(err.message);
  }
}

async function deleteDataset() {
  if (!state.id) return;
  if (!confirm(`Delete "${state.meta.name}"? The file and its insights are removed for good.`)) return;
  try {
    await api(`/api/datasets/${encodeURIComponent(state.id)}`, { method: "DELETE" });
    state.id = null;
    $("#dataset-view").hidden = true;
    $("#empty-state").hidden = false;
    showBanner("Dataset deleted.", "info");
    loadDatasets();
  } catch (err) {
    showBanner(err.message);
  }
}

// ---------- Tabs ----------

function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => { p.hidden = p.dataset.panel !== name; });
  if (name === "stats" && !state.statsLoaded) loadStats();
  if (name === "charts") drawChart();
}

// ---------- Preview: filters, sorting, paging ----------

function typeOf(column) {
  return state.types[column] || "text";
}

function addFilterRow(preset = {}) {
  const cols = Object.keys(state.types);
  const row = document.createElement("div");
  row.className = "filter-row";
  row.innerHTML = `
    <select class="f-col">${cols.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("")}</select>
    <select class="f-op"></select>
    <input class="f-val" placeholder="value" />
    <button type="button" class="ghost f-remove" aria-label="Remove filter">Remove</button>`;
  $("#filters").appendChild(row);

  const colSel = row.querySelector(".f-col");
  const opSel = row.querySelector(".f-op");
  const valInput = row.querySelector(".f-val");

  const refreshOps = (keep) => {
    const ops = OPS_BY_TYPE[typeOf(colSel.value)] || OPS_BY_TYPE.text;
    opSel.innerHTML = ops.map((o) => `<option value="${o}">${OP_LABELS[o]}</option>`).join("");
    if (keep && ops.includes(keep)) opSel.value = keep;
    valInput.disabled = NULL_OPS.has(opSel.value);
  };

  if (preset.column) colSel.value = preset.column;
  colSel.addEventListener("change", () => refreshOps());
  opSel.addEventListener("change", () => { valInput.disabled = NULL_OPS.has(opSel.value); });
  row.querySelector(".f-remove").addEventListener("click", () => row.remove());
  refreshOps(preset.op);
  if (preset.value !== undefined) valInput.value = preset.value;
}

function collectFilters() {
  const out = [];
  for (const row of document.querySelectorAll(".filter-row")) {
    const column = row.querySelector(".f-col").value;
    const op = row.querySelector(".f-op").value;
    const value = row.querySelector(".f-val").value;
    if (NULL_OPS.has(op)) {
      out.push({ column, op });
    } else if (value.trim() === "") {
      throw new Error(`The filter on "${column}" needs a value. Fill it in or remove the filter.`);
    } else {
      out.push({ column, op, value });
    }
  }
  return out;
}

async function applyFilters() {
  try {
    state.filters = collectFilters();
    setInline("#filter-error", "");
  } catch (err) {
    setInline("#filter-error", err.message);
    return;
  }
  state.page = 0;
  loadRows();
}

async function loadRows() {
  if (!state.id) return;
  const params = new URLSearchParams({ offset: state.page * PAGE, limit: PAGE });
  if (state.filters.length) params.set("filters", JSON.stringify(state.filters));
  if (state.sortBy) {
    params.set("sort_by", state.sortBy);
    params.set("sort_dir", state.sortDir);
  }
  try {
    const data = await api(`/api/datasets/${encodeURIComponent(state.id)}/rows?${params}`);
    setInline("#rows-error", "");
    renderRows(data);
  } catch (err) {
    setInline("#rows-error", err.message);
  }
}

function renderRows(data) {
  const head = data.columns.map((c, i) => {
    const arrow = state.sortBy === c ? (state.sortDir === "asc" ? " ▲" : " ▼") : "";
    return `<th data-col="${esc(c)}" class="${state.sortBy === c ? "sorted" : ""}">` +
      `${esc(c)}${arrow} <span class="type">${esc(data.types[i])}</span></th>`;
  }).join("");
  const body = data.rows.map((r) =>
    `<tr>${r.map((v) => (v === null ? `<td class="null">null</td>` : `<td title="${esc(v)}">${esc(v)}</td>`)).join("")}</tr>`
  ).join("");
  $("#rows-table").innerHTML =
    `<thead><tr>${head}</tr></thead><tbody>${body ||
      `<tr><td colspan="${data.columns.length}" class="muted">No rows match these filters.</td></tr>`}</tbody>`;

  state.total = data.total;
  const pages = Math.max(1, Math.ceil(data.total / PAGE));
  $("#row-count").textContent = `${data.total.toLocaleString()} matching rows`;
  $("#page-info").textContent = `Page ${state.page + 1} of ${pages}`;
  $("#prev").disabled = state.page === 0;
  $("#next").disabled = state.page + 1 >= pages;
}

// ---------- Profile ----------

function detailsFor(c) {
  if (c.type === "numeric" && c.min !== undefined) {
    return `min ${fmt(c.min)} · median ${fmt(c.median)} · max ${fmt(c.max)} · mean ${fmt(c.mean)}`;
  }
  if (c.type === "datetime" && c.min) {
    return `${esc(c.min.slice(0, 10))} to ${esc(c.max.slice(0, 10))}`;
  }
  return "";
}

function renderProfile() {
  const p = state.profile;
  const tile = (label, value) => `<div class="tile"><b>${value}</b><span>${label}</span></div>`;
  $("#profile-summary").innerHTML = `<div class="stat-grid">
    ${tile("Rows", p.row_count.toLocaleString())}
    ${tile("Columns", p.column_count)}
    ${tile("Duplicate rows", p.duplicate_rows.toLocaleString())}
    ${tile("Issues flagged", p.issues.length)}
  </div>`;

  $("#issues").innerHTML = p.issues.length
    ? p.issues.map((i) => `<div class="issue ${esc(i.severity)}">
        <span class="sev">${esc(i.severity)}</span>${i.column ? `<strong>${esc(i.column)}</strong>: ` : ""}${esc(i.message)}
      </div>`).join("")
    : `<p class="muted">The checks found no quality issues.</p>`;

  $("#columns-table").innerHTML = `<thead><tr>
      <th>Column</th><th>Type</th><th>Missing</th><th>Unique</th><th>Details</th>
    </tr></thead><tbody>${p.columns.map((c) => `<tr>
      <td>${esc(c.name)}</td>
      <td><span class="badge t-${esc(c.type)}">${esc(c.type)}</span></td>
      <td>${c.missing.toLocaleString()} (${c.missing_pct}%)</td>
      <td>${c.unique.toLocaleString()}</td>
      <td class="muted">${detailsFor(c)}</td>
    </tr>`).join("")}</tbody>`;
}

// ---------- Statistics ----------

async function loadStats() {
  try {
    const { stats } = await api(`/api/datasets/${encodeURIComponent(state.id)}/stats`);
    state.statsLoaded = true;
    setInline("#stats-error", "");
    $("#stats-out").innerHTML = Object.entries(stats).map(([name, s]) => {
      const rows = Object.entries(s).filter(([k]) => k !== "top").map(([k, v]) =>
        `<tr><th>${esc(k.replace(/_/g, " "))}</th><td>${v === null ? "–" : fmt(v)}</td></tr>`).join("");
      const top = s.top
        ? `<ol class="top">${s.top.map((t) => `<li><span>${esc(t.value)}</span><b>${t.count}</b></li>`).join("")}</ol>`
        : "";
      return `<div class="card stat-card"><h3>${esc(name)} <span class="badge t-${esc(state.types[name])}">${esc(state.types[name])}</span></h3>
        <table class="kv">${rows}</table>${top}</div>`;
    }).join("") || `<p class="muted">No columns to summarize.</p>`;
  } catch (err) {
    setInline("#stats-error", err.message);
  }
}

// ---------- Charts ----------

function populateChartControls() {
  const all = Object.keys(state.types);
  const numeric = all.filter((c) => state.types[c] === "numeric");
  $("#c-x").innerHTML = all.map((c) => `<option value="${esc(c)}">${esc(c)} (${esc(state.types[c])})</option>`).join("");
  $("#c-y").innerHTML = numeric.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
}

function selectedYs() {
  return [...$("#c-y").selectedOptions].map((o) => o.value);
}

function setYSelection(ys) {
  [...$("#c-y").options].forEach((o) => { o.selected = ys.includes(o.value); });
}

const COLORS = ["#2f5bd3", "#c0392b", "#2f855a", "#b7791f", "#6b3fc4", "#0e7490"];

function baseOptions() {
  return { responsive: true, maintainAspectRatio: false };
}

function chartConfig(d) {
  if (d.kind === "histogram") {
    return {
      type: "bar",
      data: { labels: d.labels, datasets: [{ label: "count", data: d.values, backgroundColor: COLORS[0] }] },
      options: baseOptions(),
    };
  }
  if (d.kind === "scatter") {
    return {
      type: "scatter",
      data: { datasets: [{ label: `${d.ys[0]} vs ${d.x}`, data: d.points, backgroundColor: COLORS[0] }] },
      options: {
        ...baseOptions(),
        scales: { x: { title: { display: true, text: d.x } }, y: { title: { display: true, text: d.ys[0] } } },
      },
    };
  }
  const datasets = d.series.map((s, i) => ({
    label: d.agg === "count" ? "count" : `${d.agg} of ${s.name}`,
    data: s.values,
    yAxisID: s.axis === "right" ? "y1" : "y",
    backgroundColor: COLORS[i % COLORS.length],
    borderColor: COLORS[i % COLORS.length],
    tension: 0.2,
  }));
  const scales = {
    y: { title: { display: true, text: d.scale === "indexed" ? "Index (start = 100)" : "" } },
  };
  if (d.scale === "dual") {
    scales.y.title.text = d.series[0].name;
    scales.y1 = {
      position: "right",
      grid: { drawOnChartArea: false },
      title: { display: true, text: d.series[1].name },
    };
  }
  return {
    type: d.kind === "line" ? "line" : "bar",
    data: { labels: d.labels, datasets },
    options: { ...baseOptions(), scales },
  };
}

async function drawHeatmap() {
  $(".chart-box").hidden = true;
  const box = $("#heatmap");
  box.hidden = false;
  box.innerHTML = `<p class="muted">Computing correlations...</p>`;
  try {
    const ys = selectedYs();
    const query = ys.length ? `?columns=${encodeURIComponent(ys.join(","))}` : "";
    const h = await api(`/api/datasets/${encodeURIComponent(state.id)}/heatmap${query}`);
    const cell = (v) => {
      if (v === null) return `<td class="null">n/a</td>`;
      const a = Math.min(Math.abs(v), 1);
      const bg = v >= 0 ? `rgba(47, 91, 211, ${a.toFixed(2)})` : `rgba(192, 57, 43, ${a.toFixed(2)})`;
      const color = a > 0.6 ? "#fff" : "#1d2330";
      return `<td style="background:${bg};color:${color}" title="r = ${v}">${v.toFixed(2)}</td>`;
    };
    const head = h.columns.map((c) => `<th>${esc(c)}</th>`).join("");
    const body = h.matrix.map((row, i) =>
      `<tr><th>${esc(h.columns[i])}</th>${row.map(cell).join("")}</tr>`).join("");
    box.innerHTML = `
      <p class="muted small">Pearson correlation between numeric columns. Blue is positive, red is negative,
      and darker means stronger. Pairs need at least 10 rows where both values exist. To choose columns, select them in the Y box. Leave it empty to use all numeric columns.</p>
      ${h.truncated ? `<p class="inline-error">Only the first 15 numeric columns are shown.</p>` : ""}
      <table><thead><tr><th></th>${head}</tr></thead><tbody>${body}</tbody></table>`;
  } catch (err) {
    box.innerHTML = `<p class="inline-error">${esc(err.message)}</p>`;
  }
}

async function drawChart() {
  if (!state.id) return;
  setInline("#chart-note", "");
  setInline("#chart-info", "");
  if ($("#c-kind").value === "heatmap") return drawHeatmap();
  $("#heatmap").hidden = true;
  $(".chart-box").hidden = false;
  if (typeof Chart === "undefined") {
    setInline("#chart-note", "The charting library did not load (it comes from a CDN). Tables still work.");
    return;
  }

  const ys = selectedYs();
  const params = new URLSearchParams({
    kind: $("#c-kind").value,
    x: $("#c-x").value,
    agg: $("#c-agg").value,
    scale: $("#c-scale").value,
    bins: $("#c-bins").value || "20",
  });
  if (ys.length) params.set("y", ys.join(","));

  let data;
  try {
    data = await api(`/api/datasets/${encodeURIComponent(state.id)}/chart?${params}`);
  } catch (err) {
    setInline("#chart-note", err.message);
    return;
  }

  if (state.chart) state.chart.destroy();
  state.chart = new Chart($("#chart").getContext("2d"), chartConfig(data));

  const info = [];
  if (data.series && data.series.length > 1) info.push(`Scale: ${data.scale}.`);
  if (data.note) info.push(data.note);
  if (info.length) setInline("#chart-info", info.join(" "));
  if (data.truncated) {
    setInline("#chart-note", "The chart shows a subset of the data (too many groups or points to draw).");
  }
}

// Used by both the Insights suggestions and the chat assistant.
function applySpec(spec) {
  $("#c-kind").value = spec.kind;
  if (spec.x) $("#c-x").value = spec.x;
  setYSelection(spec.ys ?? (spec.y ? [spec.y] : []));
  $("#c-agg").value = spec.agg && spec.agg !== "null" ? spec.agg : "count";
  $("#c-scale").value = spec.scale || "auto";
  showTab("charts");
}

async function askAssistant() {
  if (!state.id) return;
  const question = $("#ask-input").value.trim();
  setInline("#ask-error", "");
  if (!question) {
    setInline("#ask-error", "Type a question first.");
    return;
  }
  $("#ask-out").innerHTML = `<p class="muted">Thinking...</p>`;
  try {
    const r = await api(`/api/datasets/${encodeURIComponent(state.id)}/assistant`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    renderAnswer(r);
  } catch (err) {
    $("#ask-out").innerHTML = "";
    setInline("#ask-error", err.message);
  }
}

function renderAnswer(r) {
  const badge = r.generated_by === "ai"
    ? `<span class="badge ai">AI-generated</span>`
    : `<span class="badge rules">Rule-based, no AI</span>`;
  const note = r.generated_by !== "ai" && r.fallback_reason
    ? `<p class="insight-note">AI was not used: ${esc(r.fallback_reason)}</p>` : "";
  let chartLine = `<p class="muted">No chart fits this question and dataset.</p>`;
  if (r.chart) {
    const measure = r.chart.agg === "count" || !r.chart.ys.length
      ? "row counts"
      : `${r.chart.agg} of ${r.chart.ys.join(", ")}`;
    chartLine = r.chart.kind === "heatmap"
      ? `<p><b>Correlation heatmap</b> of the numeric columns. Drawn below.</p>`
      : `<p><b>${esc(r.chart.kind)}</b>: ${esc(measure)} by ${esc(r.chart.x)}. Drawn below.</p>`;
  }
  $("#ask-out").innerHTML = `<div class="card">
    <div class="insight-head">${badge}<span class="muted small">${esc(r.model || "rule engine")}</span></div>
    <p>${esc(r.answer)}</p>${note}${chartLine}
  </div>`;
  if (r.chart) applySpec(r.chart);
}

// ---------- Insights (AI or rule-based, always labeled) ----------

async function loadInsights(refresh = false) {
  const out = $("#insights-out");
  out.innerHTML = `<p class="muted">${refresh ? "Regenerating insights..." : "Generating insights..."}</p>`;
  try {
    const d = await api(`/api/datasets/${encodeURIComponent(state.id)}/insights${refresh ? "?refresh=true" : ""}`);
    renderInsights(d);
  } catch (err) {
    out.innerHTML = `<div class="banner error">${esc(err.message)}</div>`;
  }
}

function renderInsights(d) {
  const isAI = d.generated_by === "ai";
  const badge = isAI
    ? `<span class="badge ai">AI-generated</span>`
    : `<span class="badge rules">Rule-based, no AI</span>`;
  const note = !isAI && d.fallback_reason
    ? `<p class="insight-note">AI was not used: ${esc(d.fallback_reason)}</p>` : "";
  const patterns = d.patterns.map((p) => `<li>${esc(p)}</li>`).join("");
  state.suggestions = d.suggested_charts;
  const charts = d.suggested_charts.map((c, i) => `
    <div class="suggest">
      <div>
        <b>${esc(c.kind)}</b>: ${esc(c.x)}${c.y ? ` × ${esc(c.y)}` : ""}
        <span class="muted">(${esc(c.agg)})</span>
        <p class="muted small">${esc(c.reason)}</p>
      </div>
      <button data-suggest="${i}">Draw</button>
    </div>`).join("");

  $("#insights-out").innerHTML = `
    <div class="card">
      <div class="insight-head">
        ${badge}
        <span class="muted small">${esc(d.model || "rule engine")}</span>
      </div>
      <p>${esc(d.summary)}</p>
      ${note}
      <h3>Patterns</h3>
      <ul>${patterns}</ul>
      <h3>Suggested charts</h3>
      ${charts || `<p class="muted">No chart suggestions.</p>`}
    </div>`;
}

function setupInsights() {
  $("#gen-insights").addEventListener("click", () => loadInsights(false));
  $("#refresh-insights").addEventListener("click", () => loadInsights(true));
  $("#insights-out").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-suggest]");
    if (!btn) return;
    const s = state.suggestions[Number(btn.dataset.suggest)];
    if (!s) return;
    applySpec(s);
  });
}

// ---------- Wiring ----------

function setupTable() {
  $("#rows-table").addEventListener("click", (e) => {
    const th = e.target.closest("th[data-col]");
    if (!th) return;
    const col = th.dataset.col;
    if (state.sortBy === col) {
      state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    } else {
      state.sortBy = col;
      state.sortDir = "asc";
    }
    state.page = 0;
    loadRows();
  });
}

function setupControls() {
  $("#tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-tab]");
    if (btn) showTab(btn.dataset.tab);
  });
  $("#add-filter").addEventListener("click", () => addFilterRow());
  $("#apply-filters").addEventListener("click", applyFilters);
  $("#clear-filters").addEventListener("click", () => {
    $("#filters").innerHTML = "";
    setInline("#filter-error", "");
    state.filters = [];
    state.page = 0;
    addFilterRow();
    loadRows();
  });
  $("#prev").addEventListener("click", () => { state.page = Math.max(0, state.page - 1); loadRows(); });
  $("#next").addEventListener("click", () => { state.page += 1; loadRows(); });
  $("#c-draw").addEventListener("click", drawChart);
  $("#delete-btn").addEventListener("click", deleteDataset);
  $("#ask-btn").addEventListener("click", askAssistant);
  $("#ask-input").addEventListener("keydown", (e) => { if (e.key === "Enter") askAssistant(); });
}

async function checkApi() {
  try {
    await api("/api/health");
    $("#api-status").textContent = "";
  } catch (err) {
    $("#api-status").textContent = err.message;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("#max-mb").textContent = "50";
  setupUpload();
  setupTable();
  setupControls();
  setupInsights();
  checkApi();
  loadDatasets();
});
