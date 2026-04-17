// Dashboard logic: fetch summaries, render chart & tables, subscribe to /ws.

const fmt = n => (n ?? 0).toLocaleString();
const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString();

// Display currency is set by the server on every response; default EUR.
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

function fmtCost(amount) {
  return _mf.format(amount ?? 0);
}

// Prefer the converted `cost` field when present; fall back to USD * rate.
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

const els = {
  todayTokens: document.getElementById("today-tokens"),
  todayEvents: document.getElementById("today-events"),
  todayCost:   document.getElementById("today-cost"),
  weekTokens:  document.getElementById("week-tokens"),
  weekCost:    document.getElementById("week-cost"),
  liveCount:   document.getElementById("live-count"),
  wsDot:       document.getElementById("ws-dot"),
  wsLabel:     document.getElementById("ws-label"),
  rangeSelect: document.getElementById("range-select"),
  bySource:    document.querySelector("#by-source-table tbody"),
  byModel:     document.querySelector("#by-model-table tbody"),
  recent:      document.querySelector("#recent-table tbody"),
  suggestions: document.getElementById("suggestions"),
  savingsChip: document.getElementById("savings-estimate"),
};

let liveCount = 0;
let chart;

const PALETTE = ["#7aa2f7","#bb9af7","#7dcfff","#9ece6a","#e0af68","#f7768e","#2ac3de","#ff9e64"];

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}

async function refreshCards() {
  const today = await fetchJSON("/api/stats/today");
  applyCurrencyFromResponse(today);
  els.todayTokens.textContent = fmt(totalOf(today.totals));
  els.todayEvents.textContent = fmt(today.totals.event_count);
  els.todayCost.textContent   = fmtCost(costOf(today.totals));

  const week = await fetchJSON("/api/stats/range?days=7");
  applyCurrencyFromResponse(week);
  els.weekTokens.textContent = fmt(totalOf(week.totals));
  els.weekCost.textContent   = fmtCost(costOf(week.totals));
}

function totalOf(t) {
  return (t.input_tokens||0) + (t.output_tokens||0) + (t.cache_read_tokens||0) + (t.cache_write_tokens||0);
}

async function refreshBySource() {
  const r = await fetchJSON("/api/stats/by_source?days=7");
  applyCurrencyFromResponse(r);
  els.bySource.innerHTML = r.rows.map(row => `
    <tr>
      <td><span class="source-tag ${row.source}">${row.source}</span></td>
      <td>${fmt(row.tokens)}</td>
      <td>${fmtCost(costOf(row))}</td>
      <td>${fmt(row.event_count)}</td>
    </tr>`).join("");
}

async function refreshByModel() {
  const r = await fetchJSON("/api/stats/by_model?days=7");
  applyCurrencyFromResponse(r);
  els.byModel.innerHTML = r.rows.map(row => `
    <tr>
      <td>${row.model}</td>
      <td>${fmt(row.input_tokens)}</td>
      <td>${fmt(row.output_tokens)}</td>
      <td>${fmt((row.cache_read_tokens||0)+(row.cache_write_tokens||0))}</td>
      <td>${fmtCost(costOf(row))}</td>
    </tr>`).join("");
}

async function refreshRecent() {
  const r = await fetchJSON("/api/events/recent?limit=25");
  applyCurrencyFromResponse(r);
  els.recent.innerHTML = r.events.map(ev => `
    <tr>
      <td>${fmtTime(ev.timestamp)}</td>
      <td><span class="source-tag ${ev.source}">${ev.source}</span></td>
      <td>${ev.model}</td>
      <td>${fmt(ev.input_tokens)}</td>
      <td>${fmt(ev.output_tokens)}</td>
      <td>${fmtCost(costOf(ev))}</td>
    </tr>`).join("");
}

async function refreshSuggestions() {
  try {
    const r = await fetchJSON("/api/suggestions?days=7");
    applyCurrencyFromResponse(r);
    const totalSaving = r.estimated_monthly_savings ?? (r.estimated_monthly_savings_usd * DISPLAY_RATE);
    els.savingsChip.textContent = totalSaving > 0
      ? `~${fmtCost(totalSaving)}/mo savings possible`
      : `${r.count} tips`;
    if (r.items.length === 0) {
      els.suggestions.innerHTML = `<div class="muted">No actionable tips right now — your usage looks tidy.</div>`;
      return;
    }
    els.suggestions.innerHTML = r.items.map(s => {
      const saving = s.estimated_monthly_savings ?? (s.estimated_monthly_savings_usd * DISPLAY_RATE);
      return `
      <div class="suggestion sev-${s.severity}">
        <div class="sug-head">
          <span class="sug-sev">${s.severity}</span>
          <span class="sug-title">${s.title}</span>
          ${saving > 0 ? `<span class="sug-save">~${fmtCost(saving)}/mo</span>` : ""}
        </div>
        <div class="sug-body">${s.body}</div>
        <div class="sug-action"><strong>Try:</strong> ${s.action}</div>
      </div>`;
    }).join("");
  } catch (e) {
    els.suggestions.innerHTML = `<div class="muted">Couldn't load suggestions: ${e}</div>`;
  }
}

async function refreshChart() {
  const hours = parseFloat(els.rangeSelect.value);
  const bucketMin = hours <= 2 ? 5 : hours <= 24 ? 15 : hours <= 168 ? 120 : 720;
  const r = await fetchJSON(`/api/stats/series?hours=${hours}&bucket_minutes=${bucketMin}`);
  // Pivot rows [{bucket_ts, source, tokens}] into a stacked dataset.
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

  const labels = buckets.map(b => new Date(b * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}));

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
          x: { stacked: true, ticks: { color: "#8b93a7" }, grid: { color: "#242833" } },
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

els.rangeSelect.addEventListener("change", refreshChart);

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
    if (msg.type === "event") {
      applyCurrencyFromResponse(msg);
      onLiveEvent(msg.data);
    }
  };
}

function onLiveEvent(ev) {
  liveCount += 1;
  els.liveCount.textContent = liveCount;
  // Prepend to recent table and flash.
  const row = document.createElement("tr");
  row.className = "flash";
  row.innerHTML = `
    <td>${fmtTime(ev.timestamp)}</td>
    <td><span class="source-tag ${ev.source}">${ev.source}</span></td>
    <td>${ev.model}</td>
    <td>${fmt(ev.input_tokens)}</td>
    <td>${fmt(ev.output_tokens)}</td>
    <td>${fmtCost(costOf(ev))}</td>`;
  els.recent.prepend(row);
  while (els.recent.rows.length > 25) els.recent.deleteRow(-1);
  // Refresh cards cheaply.
  refreshCards().catch(console.warn);
}

async function initialLoad() {
  await Promise.all([
    refreshCards(), refreshBySource(), refreshByModel(),
    refreshRecent(), refreshChart(), refreshSuggestions(),
  ]);
}

initialLoad().catch(console.error);
connectWS();
// Periodic full refresh keeps numbers honest if WS drops.
setInterval(() => initialLoad().catch(console.warn), 30_000);
