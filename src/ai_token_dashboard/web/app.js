// Dashboard logic

const fmt = n => (n ?? 0).toLocaleString();
const fmtDate = ts => new Date(ts * 1000).toLocaleDateString();
const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString();
const fmtDateTime = ts => new Date(ts * 1000).toLocaleString();

function esc(s) {
  return String(s ?? "")
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

let DISPLAY_CURRENCY = "EUR";
let DISPLAY_RATE = 1.0;

function moneyFormatter(currency) {
  try {
    return new Intl.NumberFormat(navigator.language, {
      style: "currency", currency, maximumFractionDigits: 2,
    });
  } catch {
    return { format: v => `${currency} ${(v ?? 0).toFixed(2)}` };
  }
}
let _mf = moneyFormatter(DISPLAY_CURRENCY);

function fmtCost(amount) { return _mf.format(amount ?? 0); }

function costOf(row) {
  if (row && typeof row.cost === "number") return row.cost;
  if (row && typeof row.cost_usd === "number") return row.cost_usd * DISPLAY_RATE;
  return 0;
}

function applyCurrencyFromResponse(resp) {
  if (!resp) return;
  if (resp.currency && resp.currency !== DISPLAY_CURRENCY) {
    DISPLAY_CURRENCY = resp.currency;
    _mf = moneyFormatter(DISPLAY_CURRENCY);
  }
  if (typeof resp.rate === "number") DISPLAY_RATE = resp.rate;
}

// ── Date range state ──────────────────────────────────────────────────────────

let RANGE = { preset: "today", sinceTs: null, untilTs: null };

function dateToTs(dateStr, endOfDay = false) {
  if (!dateStr) return null;
  const d = new Date(dateStr);
  if (endOfDay) { d.setHours(23, 59, 59, 999); }
  return d.getTime() / 1000;
}

function startOfDay(ts) {
  const d = new Date(ts * 1000);
  d.setHours(0, 0, 0, 0);
  return d.getTime() / 1000;
}

function applyPreset(preset, customFrom = null, customTo = null) {
  const now = Date.now() / 1000;
  RANGE.preset = preset;
  switch (preset) {
    case "today":
      RANGE.sinceTs = startOfDay(now);
      RANGE.untilTs = null;
      break;
    case "7d":
      RANGE.sinceTs = now - 7 * 86400;
      RANGE.untilTs = null;
      break;
    case "30d":
      RANGE.sinceTs = now - 30 * 86400;
      RANGE.untilTs = null;
      break;
    case "all":
      RANGE.sinceTs = 0;
      RANGE.untilTs = null;
      break;
    case "custom":
      RANGE.sinceTs = dateToTs(customFrom, false) ?? (now - 7 * 86400);
      RANGE.untilTs = dateToTs(customTo, true);
      break;
  }
  updateRangeLabel();
}

function updateRangeLabel() {
  const el = document.getElementById("range-label");
  if (!el) return;
  if (RANGE.preset === "all") { el.textContent = "All time"; return; }
  if (RANGE.preset === "custom") {
    const from = RANGE.sinceTs ? fmtDate(RANGE.sinceTs) : "–";
    const to = RANGE.untilTs ? fmtDate(RANGE.untilTs) : "now";
    el.textContent = `${from} → ${to}`;
    return;
  }
  el.textContent = "";
}

function rangeParams() {
  let p = `since_ts=${RANGE.sinceTs ?? 0}`;
  if (RANGE.untilTs) p += `&until_ts=${RANGE.untilTs}`;
  return p;
}

// ── DOM refs ──────────────────────────────────────────────────────────────────

const els = {
  todayTokens:  document.getElementById("today-tokens"),
  todayEvents:  document.getElementById("today-events"),
  todayCost:    document.getElementById("today-cost"),
  cacheSavings: document.getElementById("cache-savings"),
  liveCount:    document.getElementById("live-count"),
  wsDot:        document.getElementById("ws-dot"),
  wsLabel:      document.getElementById("ws-label"),
  bySource:     document.querySelector("#by-source-table tbody"),
  byModel:      document.querySelector("#by-model-table tbody"),
  byProject:    document.querySelector("#by-project-table tbody"),
  recent:       document.querySelector("#recent-table tbody"),
  suggestions:  document.getElementById("suggestions"),
  savingsChip:  document.getElementById("savings-estimate"),
};

let liveCount = 0;
let chart;

const PALETTE = ["#7aa2f7","#bb9af7","#7dcfff","#9ece6a","#e0af68","#f7768e","#2ac3de","#ff9e64"];

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}

function totalOf(t) {
  return (t.input_tokens||0) + (t.output_tokens||0) + (t.cache_read_tokens||0) + (t.cache_write_tokens||0);
}

// ── Refresh functions ─────────────────────────────────────────────────────────

async function refreshCards() {
  const r = await fetchJSON(`/api/stats/range?${rangeParams()}`);
  applyCurrencyFromResponse(r);
  const t = r.totals;
  els.todayTokens.textContent = fmt(totalOf(t));
  els.todayEvents.textContent = fmt(t.event_count);
  els.todayCost.textContent   = fmtCost(costOf(t));

  const totalIn = (t.input_tokens||0) + (t.cache_read_tokens||0) + (t.cache_write_tokens||0);
  const cacheHit = t.cache_read_tokens || 0;
  els.cacheSavings.textContent = (totalIn > 0 && cacheHit > 0)
    ? `${((cacheHit / totalIn) * 100).toFixed(1)}% cache hit`
    : "–";
}

async function refreshBySource() {
  const r = await fetchJSON(`/api/stats/by_source?${rangeParams()}`);
  applyCurrencyFromResponse(r);
  if (!r.rows.length) {
    els.bySource.innerHTML = `<tr><td colspan="4" class="muted">No data for selected range</td></tr>`;
    return;
  }
  // Build DOM nodes to avoid innerHTML with untrusted data
  const frag = document.createDocumentFragment();
  r.rows.forEach(row => {
    const tr = document.createElement("tr");
    const tdSource = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = `source-tag ${esc(row.source)}`;
    tag.textContent = row.source;
    tdSource.appendChild(tag);
    const tdTokens = document.createElement("td");
    tdTokens.textContent = fmt(row.tokens);
    const tdCost = document.createElement("td");
    tdCost.textContent = fmtCost(costOf(row));
    const tdEvents = document.createElement("td");
    tdEvents.textContent = fmt(row.event_count);
    tr.append(tdSource, tdTokens, tdCost, tdEvents);
    frag.appendChild(tr);
  });
  els.bySource.replaceChildren(frag);
}

async function refreshByModel() {
  const r = await fetchJSON(`/api/stats/by_model?${rangeParams()}`);
  applyCurrencyFromResponse(r);
  if (!r.rows.length) {
    els.byModel.innerHTML = `<tr><td colspan="5" class="muted">No data for selected range</td></tr>`;
    return;
  }
  const frag = document.createDocumentFragment();
  r.rows.forEach(row => {
    const tr = document.createElement("tr");
    [
      row.model,
      fmt(row.input_tokens),
      fmt(row.output_tokens),
      fmt((row.cache_read_tokens||0)+(row.cache_write_tokens||0)),
      fmtCost(costOf(row)),
    ].forEach(val => {
      const td = document.createElement("td");
      td.textContent = val;
      tr.appendChild(td);
    });
    frag.appendChild(tr);
  });
  els.byModel.replaceChildren(frag);
}

async function refreshRecent() {
  const r = await fetchJSON(`/api/events/recent?limit=50&${rangeParams()}`);
  applyCurrencyFromResponse(r);
  if (!r.events.length) {
    els.recent.innerHTML = `<tr><td colspan="6" class="muted">No events in selected range</td></tr>`;
    return;
  }
  const frag = document.createDocumentFragment();
  r.events.forEach(ev => {
    const tr = document.createElement("tr");
    const tdTime = document.createElement("td");
    tdTime.textContent = fmtDateTime(ev.timestamp);
    const tdSource = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = `source-tag ${esc(ev.source)}`;
    tag.textContent = ev.source;
    tdSource.appendChild(tag);
    [tdTime, tdSource].forEach(td => tr.appendChild(td));
    [ev.model, fmt(ev.input_tokens), fmt(ev.output_tokens), fmtCost(costOf(ev))].forEach(val => {
      const td = document.createElement("td");
      td.textContent = val;
      tr.appendChild(td);
    });
    frag.appendChild(tr);
  });
  els.recent.replaceChildren(frag);
}

async function refreshByProject() {
  const r = await fetchJSON(`/api/stats/by_project?${rangeParams()}`);
  applyCurrencyFromResponse(r);
  if (!r.rows.length) {
    els.byProject.innerHTML = `<tr><td colspan="5" class="muted">No data for selected range</td></tr>`;
    return;
  }
  const frag = document.createDocumentFragment();
  r.rows.forEach(row => {
    const tr = document.createElement("tr");
    const tdProject = document.createElement("td");
    tdProject.className = "project-path";
    tdProject.textContent = row.project_display ?? row.project;
    tdProject.title = row.project;
    const tdSource = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = `source-tag ${esc(row.source)}`;
    tag.textContent = row.source;
    tdSource.appendChild(tag);
    [tdProject, tdSource].forEach(td => tr.appendChild(td));
    [fmt(row.tokens), fmtCost(costOf(row)), fmt(row.event_count)].forEach(val => {
      const td = document.createElement("td");
      td.textContent = val;
      tr.appendChild(td);
    });
    frag.appendChild(tr);
  });
  els.byProject.replaceChildren(frag);
}

async function refreshSuggestions() {
  try {
    const r = await fetchJSON("/api/suggestions?days=7");
    applyCurrencyFromResponse(r);
    const totalSaving = r.estimated_monthly_savings ?? (r.estimated_monthly_savings_usd * DISPLAY_RATE);
    els.savingsChip.textContent = totalSaving > 0
      ? `~${fmtCost(totalSaving)}/mo savings possible`
      : `${r.count} tips`;
    if (!r.items.length) {
      els.suggestions.innerHTML = `<div class="muted">No actionable tips right now.</div>`;
      return;
    }
    // Suggestions come from our own advisor logic, but escape for safety
    els.suggestions.innerHTML = r.items.map(s => {
      const saving = s.estimated_monthly_savings ?? (s.estimated_monthly_savings_usd * DISPLAY_RATE);
      return `
      <div class="suggestion sev-${esc(s.severity)}">
        <div class="sug-head">
          <span class="sug-sev">${esc(s.severity)}</span>
          <span class="sug-title">${esc(s.title)}</span>
          ${saving > 0 ? `<span class="sug-save">~${fmtCost(saving)}/mo</span>` : ""}
        </div>
        <div class="sug-body">${esc(s.body)}</div>
        <div class="sug-action"><strong>Try:</strong> ${esc(s.action)}</div>
      </div>`;
    }).join("");
  } catch (e) {
    els.suggestions.innerHTML = `<div class="muted">Couldn't load suggestions.</div>`;
  }
}

async function refreshChart() {
  const spanSeconds = (RANGE.untilTs ?? (Date.now() / 1000)) - RANGE.sinceTs;
  const spanHours = spanSeconds / 3600;

  let bucketMin;
  if (spanHours <= 2)        bucketMin = 5;
  else if (spanHours <= 24)  bucketMin = 15;
  else if (spanHours <= 168) bucketMin = 120;
  else if (spanHours <= 720) bucketMin = 720;
  else                       bucketMin = 1440;

  const r = await fetchJSON(`/api/stats/series?bucket_minutes=${bucketMin}&${rangeParams()}`);

  const sources = [...new Set(r.rows.map(x => x.source))];
  const buckets = [...new Set(r.rows.map(x => x.bucket_ts))].sort((a,b)=>a-b);
  const bySource = Object.fromEntries(sources.map(s => [s, Object.fromEntries(buckets.map(b=>[b,0]))]));
  r.rows.forEach(row => { bySource[row.source][row.bucket_ts] = row.tokens; });

  const datasets = sources.map((s, i) => ({
    label: s,
    data: buckets.map(b => bySource[s][b]),
    backgroundColor: PALETTE[i % PALETTE.length] + "cc",
    borderColor: PALETTE[i % PALETTE.length],
    borderWidth: 1,
    fill: true,
    tension: 0.2,
    pointRadius: 0,
  }));

  const labelFn = bucketMin >= 1440
    ? b => new Date(b * 1000).toLocaleDateString([], {month:"short", day:"numeric"})
    : bucketMin >= 120
      ? b => new Date(b * 1000).toLocaleString([], {month:"short", day:"numeric", hour:"2-digit"})
      : b => new Date(b * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});

  const labels = buckets.map(labelFn);

  if (!chart) {
    chart = new Chart(document.getElementById("series-chart"), {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        interaction: { intersect: false, mode: "index" },
        plugins: {
          legend: { labels: { color: "#e6e8ee" } },
          tooltip: { callbacks: { label: c => `${c.dataset.label}: ${fmt(c.parsed.y)}` } },
        },
        scales: {
          x: { stacked: true, ticks: { color: "#8b93a7", maxTicksLimit: 12 }, grid: { color: "#242833" } },
          y: { stacked: true, ticks: { color: "#8b93a7" }, grid: { color: "#242833" } },
        },
      },
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets = datasets;
    chart.update();
  }
}

// ── Preset buttons ────────────────────────────────────────────────────────────

document.querySelectorAll(".preset").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".preset").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    const preset = btn.dataset.preset;
    const customEl = document.getElementById("custom-range");
    if (preset === "custom") {
      customEl.style.display = "flex";
      const now = new Date();
      const week = new Date(now - 7 * 86400000);
      document.getElementById("date-to").value = now.toISOString().slice(0,10);
      document.getElementById("date-from").value = week.toISOString().slice(0,10);
      return;
    }
    customEl.style.display = "none";
    applyPreset(preset);
    refreshAll();
  });
});

document.getElementById("apply-custom").addEventListener("click", () => {
  const from = document.getElementById("date-from").value;
  const to   = document.getElementById("date-to").value;
  applyPreset("custom", from, to);
  refreshAll();
});

// ── WebSocket ─────────────────────────────────────────────────────────────────

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen    = () => { els.wsDot.classList.replace("disconnected","connected"); els.wsLabel.textContent = "live"; };
  ws.onclose   = () => {
    els.wsDot.classList.replace("connected","disconnected");
    els.wsLabel.textContent = "reconnecting…";
    setTimeout(connectWS, 2000);
  };
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "event") { applyCurrencyFromResponse(msg); onLiveEvent(msg.data); }
  };
}

function onLiveEvent(ev) {
  liveCount += 1;
  els.liveCount.textContent = liveCount;
  const tr = document.createElement("tr");
  tr.className = "flash";
  const tdTime = document.createElement("td");
  tdTime.textContent = fmtDateTime(ev.timestamp);
  const tdSource = document.createElement("td");
  const tag = document.createElement("span");
  tag.className = `source-tag ${esc(ev.source)}`;
  tag.textContent = ev.source;
  tdSource.appendChild(tag);
  tr.append(tdTime, tdSource);
  [ev.model, fmt(ev.input_tokens), fmt(ev.output_tokens), fmtCost(costOf(ev))].forEach(val => {
    const td = document.createElement("td");
    td.textContent = val;
    tr.appendChild(td);
  });
  els.recent.prepend(tr);
  while (els.recent.rows.length > 50) els.recent.deleteRow(-1);
  refreshCards().catch(console.warn);
}

// ── Boot ──────────────────────────────────────────────────────────────────────

async function refreshAll() {
  await Promise.all([
    refreshCards(), refreshBySource(), refreshByModel(),
    refreshByProject(), refreshRecent(), refreshChart(), refreshSuggestions(),
  ]);
}

applyPreset("today");
refreshAll().catch(console.error);
connectWS();
setInterval(() => refreshAll().catch(console.warn), 30_000);
