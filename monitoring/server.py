"""
Lightweight HTTP monitoring server.

GET /         → dashboard HTML
GET /status   → JSON snapshot of bot state
GET /history  → JSON price + portfolio history + recent trades
GET /metrics  → Prometheus-compatible text metrics
"""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional

from prometheus_client import (
    Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
)

logger = logging.getLogger(__name__)

_SAMPLE_INTERVAL = 5     # seconds between history data points
_MAX_HISTORY     = 360   # 30 min @ 5s samples
_MAX_TRADES      = 50

# ------------------------------------------------------------------ #
# Prometheus metrics
# ------------------------------------------------------------------ #

TRADES_TOTAL    = Counter("gridbot_trades_total",    "Total filled orders",             ["side"])
DRAWDOWN_GAUGE  = Gauge("gridbot_drawdown_percent",  "Current drawdown % of peak")
OPEN_ORDERS_GAUGE = Gauge("gridbot_open_orders",     "Active grid orders")
UPTIME_GAUGE    = Gauge("gridbot_uptime_seconds",    "Bot uptime in seconds")
LAST_PRICE_GAUGE = Gauge("gridbot_last_price_usdt",  "Latest BTC price")
REGIME_GAUGE    = Gauge("gridbot_regime_ranging",    "1 if AI regime is ranging")
PORTFOLIO_GAUGE = Gauge("gridbot_portfolio_usdt",    "Estimated portfolio value")

# ------------------------------------------------------------------ #
# Dashboard HTML
# ------------------------------------------------------------------ #

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Grid Bot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;500;600;700&family=JetBrains+Mono:ital,wght@0,400;0,500;1,400&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080f1e;
  --surface:#0d1628;
  --surface2:#111e35;
  --border:rgba(148,163,184,0.07);
  --border2:rgba(148,163,184,0.14);
  --text:#dde4f0;
  --muted:#4a5a7a;
  --dim:#1e2d47;
  --green:#00e5a0;
  --green-d:rgba(0,229,160,0.12);
  --red:#ff6b8a;
  --red-d:rgba(255,107,138,0.12);
  --blue:#4fc3f7;
  --purple:#a78bfa;
  --yellow:#fbbf24;
  --orange:#fb923c;
  --font-ui:'Syne',sans-serif;
  --font-mono:'JetBrains Mono',monospace;
}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:var(--font-ui);font-size:13px;line-height:1.5;-webkit-font-smoothing:antialiased}

/* ── Header ── */
header{
  display:flex;align-items:center;gap:1.5rem;
  padding:.75rem 1.5rem;
  background:var(--surface);
  border-bottom:1px solid var(--border2);
  flex-wrap:wrap;
  position:sticky;top:0;z-index:10;
}
.state-indicator{display:flex;align-items:center;gap:.5rem;min-width:130px}
#state-dot{
  width:8px;height:8px;border-radius:50%;flex-shrink:0;
  transition:background .4s;
}
#state-dot.pulse{animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 currentColor}50%{box-shadow:0 0 0 4px transparent}}
#state-text{
  font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  font-family:var(--font-ui);transition:color .4s;
}
#hdr-price{
  font-family:var(--font-mono);font-size:1.15rem;font-weight:500;
  letter-spacing:-.02em;transition:color .2s;
}
.hdr-badge{
  font-size:.6rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  padding:.2rem .6rem;border-radius:3px;border:1px solid;
}
.badge-paper{color:#94a3b8;border-color:rgba(148,163,184,.2)}
.badge-live{color:var(--green);border-color:var(--green-d);background:var(--green-d)}
.badge-dry{color:var(--yellow);border-color:rgba(251,191,36,.2)}
.badge-ranging{color:var(--green);border-color:rgba(0,229,160,.2);background:var(--green-d)}
.badge-trending{color:var(--yellow);border-color:rgba(251,191,36,.2);background:rgba(251,191,36,.1)}
.badge-hv{color:var(--red);border-color:rgba(255,107,138,.2);background:var(--red-d)}
.badge-unknown{color:var(--muted);border-color:rgba(74,90,122,.3)}
.hdr-spacer{flex:1}
#hdr-uptime,#hdr-tick{
  font-family:var(--font-mono);font-size:.7rem;color:var(--muted);
}
#hdr-tick::before{content:'↻ ';opacity:.5}

/* ── Main grid ── */
.layout{
  display:grid;
  grid-template-columns:1fr 340px;
  grid-template-rows:auto auto;
  min-height:calc(100vh - 49px);
}
@media(max-width:900px){
  .layout{grid-template-columns:1fr}
  .sidebar{border-left:none;border-top:1px solid var(--border)}
  .trades{border-top:1px solid var(--border)}
}

/* ── Price panel ── */
.price-panel{
  padding:1.25rem;
  border-right:1px solid var(--border);
  display:flex;flex-direction:column;gap:.75rem;
}
.panel-label{
  font-size:.6rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);margin-bottom:.25rem;
}
.chart-wrap{position:relative}
#price-wrap{height:400px}
#equity-wrap{height:150px}

/* ── Sidebar ── */
.sidebar{
  padding:1.25rem;display:flex;flex-direction:column;gap:.75rem;
  border-right:1px solid var(--border);
}
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:.5rem}
.stat{
  background:var(--surface2);border:1px solid var(--border2);border-radius:6px;
  padding:.75rem .875rem;
}
.stat.full{grid-column:span 2}
.stat-label{font-size:.6rem;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:.3rem}
.stat-value{font-family:var(--font-mono);font-size:1.05rem;font-weight:500;transition:color .3s}
.stat-sub{font-family:var(--font-mono);font-size:.7rem;color:var(--muted);margin-top:.2rem}
.dd-track{height:2px;background:var(--dim);border-radius:1px;margin-top:.5rem}
.dd-fill{height:100%;border-radius:1px;transition:width .6s,background .6s;width:0}
.equity-panel{
  background:var(--surface2);border:1px solid var(--border2);border-radius:6px;
  padding:.875rem;flex:1;min-height:180px;display:flex;flex-direction:column;
}

/* ── Trades ── */
.trades{
  grid-column:1/-1;
  border-top:1px solid var(--border);
  padding:1.25rem;
}
.trades-header{
  display:flex;align-items:center;justify-content:space-between;margin-bottom:.875rem;
}
#trade-count{font-family:var(--font-mono);font-size:.7rem;color:var(--muted)}
table{width:100%;border-collapse:collapse}
thead th{
  font-size:.6rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);padding:.4rem .75rem;text-align:left;
  border-bottom:1px solid var(--border2);
}
thead th:last-child,thead th:nth-child(3),thead th:nth-child(4){text-align:right}
tbody tr{border-bottom:1px solid var(--border);transition:background .15s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:var(--surface2)}
td{padding:.5rem .75rem;font-family:var(--font-mono);font-size:.8rem}
td:last-child,td:nth-child(3),td:nth-child(4){text-align:right}
.side-buy{color:var(--green)}
.side-sell{color:var(--red)}
.pos{color:var(--green)}
.neg{color:var(--red)}
.td-muted{color:var(--muted)}
.empty-state{
  text-align:center;padding:3rem;color:var(--muted);
  font-size:.8rem;letter-spacing:.04em;
}
.empty-state::before{display:block;font-size:1.5rem;margin-bottom:.5rem;content:'⋯'}

/* ── Utility ── */
.c-green{color:var(--green)}
.c-red{color:var(--red)}
.c-yellow{color:var(--yellow)}
.c-orange{color:var(--orange)}
.c-muted{color:var(--muted)}
</style>
</head>
<body>

<header>
  <div class="state-indicator">
    <div id="state-dot"></div>
    <span id="state-text">—</span>
  </div>
  <span id="hdr-price">—</span>
  <span id="hdr-mode" class="hdr-badge badge-paper">paper</span>
  <span id="hdr-regime" class="hdr-badge badge-unknown">—</span>
  <span class="hdr-spacer"></span>
  <span id="hdr-uptime">—</span>
  <span id="hdr-tick">—</span>
</header>

<div class="layout">

  <section class="price-panel">
    <div>
      <div class="panel-label">Price &amp; Grid Levels</div>
      <div class="chart-wrap" id="price-wrap">
        <canvas id="price-chart"></canvas>
      </div>
    </div>
  </section>

  <aside class="sidebar">
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-label">Portfolio</div>
        <div class="stat-value" id="s-portfolio">—</div>
        <div class="stat-sub">USDT</div>
      </div>
      <div class="stat">
        <div class="stat-label">Drawdown</div>
        <div class="stat-value" id="s-drawdown">—</div>
        <div class="dd-track"><div class="dd-fill" id="dd-fill"></div></div>
      </div>
      <div class="stat">
        <div class="stat-label">Open Orders</div>
        <div class="stat-value" id="s-orders">—</div>
      </div>
      <div class="stat">
        <div class="stat-label">Losses</div>
        <div class="stat-value" id="s-losses">—</div>
        <div class="stat-sub">consecutive / limit</div>
      </div>
      <div class="stat full">
        <div class="stat-label">Cooldown</div>
        <div class="stat-value" id="s-cooldown">—</div>
      </div>
    </div>

    <div class="equity-panel">
      <div class="panel-label">Portfolio Value</div>
      <div class="chart-wrap" id="equity-wrap" style="flex:1;position:relative">
        <canvas id="equity-chart"></canvas>
      </div>
    </div>
  </aside>

  <section class="trades">
    <div class="trades-header">
      <span class="panel-label" style="margin:0">Recent Fills</span>
      <span id="trade-count"></span>
    </div>
    <div id="trades-container">
      <div class="empty-state" id="trades-empty">No fills yet</div>
      <table id="trades-table" style="display:none">
        <thead><tr>
          <th>Time</th>
          <th>Side</th>
          <th>Price</th>
          <th>Qty</th>
          <th>P&amp;L</th>
        </tr></thead>
        <tbody id="trades-body"></tbody>
      </table>
    </div>
  </section>

</div>

<script>
'use strict';

/* ── Formatters ── */
const fmtPrice  = n => '$' + (+n).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
const fmtPct    = n => (+n).toFixed(2) + '%';
const fmtQty    = n => (+n).toFixed(5);
const fmtUptime = s => {
  s = Math.floor(s);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  return [h,m,sec].map(v=>String(v).padStart(2,'0')).join(':');
};
const fmtTime   = ts => new Date(ts).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
const el        = id => document.getElementById(id);

/* ── State ── */
const STATE_COLORS = {
  running:'#00e5a0', paused:'#fbbf24', cooldown:'#fb923c',
  emergency_stop:'#ff6b8a', starting:'#60a5fa', stopping:'#94a3b8'
};
function levelColor(status, alpha) {
  const a = alpha || 1;
  if (status === 'BUY_OPEN')    return `rgba(0,229,160,${a})`;
  if (status === 'SELL_OPEN')   return `rgba(255,107,138,${a})`;
  if (status === 'BUY_FILLED')  return `rgba(0,229,160,${a * 0.35})`;
  if (status === 'SELL_FILLED') return `rgba(255,107,138,${a * 0.35})`;
  return `rgba(74,90,122,${a * 0.3})`;
}

/* ── Chart.js defaults ── */
Chart.defaults.color = '#4a5a7a';
Chart.defaults.font.family = "'JetBrains Mono', monospace";
Chart.defaults.font.size   = 10;

/* ── Price chart ── */
let priceChart;
function initPriceChart() {
  const ctx = el('price-chart').getContext('2d');
  priceChart = new Chart(ctx, {
    type: 'line',
    data: { datasets: [
      {
        label: 'glow',
        data: [],
        borderColor: 'rgba(79,195,247,0.18)',
        borderWidth: 7,
        pointRadius: 0,
        tension: 0.15,
        fill: false,
        order: 2,
      },
      {
        label: 'BTC Price',
        data: [],
        borderColor: '#4fc3f7',
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.15,
        fill: false,
        order: 1,
      }
    ]},
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: {
          type: 'linear',
          ticks: {
            maxTicksLimit: 6,
            callback: v => fmtTime(v),
            color: '#4a5a7a',
          },
          grid: { color: 'rgba(255,255,255,0.03)' },
          border: { color: 'rgba(148,163,184,0.1)' },
        },
        y: {
          ticks: {
            callback: v => '$' + v.toLocaleString(),
            color: '#4a5a7a',
            maxTicksLimit: 6,
          },
          grid: { color: 'rgba(255,255,255,0.03)' },
          border: { color: 'rgba(148,163,184,0.1)' },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(13,22,40,0.95)',
          borderColor: 'rgba(148,163,184,0.15)',
          borderWidth: 1,
          titleColor: '#4a5a7a',
          bodyColor: '#dde4f0',
          bodyFont: { family: "'JetBrains Mono', monospace", size: 11 },
          callbacks: {
            label: ctx => ' ' + fmtPrice(ctx.parsed.y),
            title: ctx => fmtTime(ctx[0].parsed.x),
          },
          filter: item => item.datasetIndex === 1,
        },
        annotation: { annotations: {} },
      },
    },
  });
}

/* ── Equity chart ── */
let equityChart;
function initEquityChart() {
  const ctx = el('equity-chart').getContext('2d');
  equityChart = new Chart(ctx, {
    type: 'line',
    data: { datasets: [{
      label: 'Portfolio',
      data: [],
      borderColor: '#a78bfa',
      backgroundColor: 'rgba(167,139,250,0.1)',
      fill: true,
      borderWidth: 1.5,
      pointRadius: 0,
      tension: 0.3,
    }]},
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      scales: {
        x: {
          type: 'linear',
          ticks: { maxTicksLimit: 4, callback: v => fmtTime(v), color: '#4a5a7a' },
          grid: { display: false },
          border: { color: 'rgba(148,163,184,0.1)' },
        },
        y: {
          ticks: { callback: v => '$' + v.toFixed(0), color: '#4a5a7a', maxTicksLimit: 4 },
          grid: { color: 'rgba(255,255,255,0.03)' },
          border: { color: 'rgba(148,163,184,0.1)' },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(13,22,40,0.95)',
          borderColor: 'rgba(148,163,184,0.15)',
          borderWidth: 1,
          bodyFont: { family: "'JetBrains Mono', monospace", size: 11 },
          callbacks: {
            label: ctx => ' ' + fmtPrice(ctx.parsed.y),
            title: ctx => fmtTime(ctx[0].parsed.x),
          },
        },
      },
    },
  });
}

/* ── Update header ── */
function updateHeader(s) {
  const color = STATE_COLORS[s.state] || '#64748b';
  const dot = el('state-dot');
  dot.style.background = color;
  dot.style.color = color;
  dot.className = s.state === 'running' ? 'pulse' : '';

  el('state-text').textContent = (s.state || '—').replace(/_/g,' ').toUpperCase();
  el('state-text').style.color = color;

  el('hdr-price').textContent = (s.symbol || 'BTCUSDT') + '  ' + (s.last_price ? fmtPrice(s.last_price) : '—');
  el('hdr-price').style.color = color;

  const modeEl = el('hdr-mode');
  modeEl.textContent = s.mode || '—';
  modeEl.className = 'hdr-badge badge-' + (s.mode || 'paper');

  const regime = s.ai_regime || 'unknown';
  const regimeEl = el('hdr-regime');
  regimeEl.textContent = regime.replace(/_/g,' ');
  regimeEl.className = 'hdr-badge ' + (regime === 'ranging' ? 'badge-ranging' : regime === 'trending' ? 'badge-trending' : regime === 'high_volatility' ? 'badge-hv' : 'badge-unknown');

  el('hdr-uptime').textContent = s.uptime_seconds != null ? fmtUptime(s.uptime_seconds) : '—';
  el('hdr-tick').textContent = new Date().toLocaleTimeString();
}

/* ── Update stat cards ── */
function updateCards(s) {
  const pv = s.portfolio_value;
  el('s-portfolio').textContent = pv ? fmtPrice(pv) : '—';

  const dd = s.drawdown_percent || 0;
  const ddEl = el('s-drawdown');
  ddEl.textContent = fmtPct(dd);
  ddEl.style.color = dd < 3 ? 'var(--green)' : dd < 6 ? 'var(--yellow)' : 'var(--red)';
  const fill = el('dd-fill');
  fill.style.width = Math.min(dd / 8 * 100, 100) + '%';
  fill.style.background = dd < 3 ? 'var(--green)' : dd < 6 ? 'var(--yellow)' : 'var(--red)';

  el('s-orders').textContent = s.open_orders != null ? s.open_orders : '—';

  const cl = s.consecutive_losses;
  const lossEl = el('s-losses');
  lossEl.textContent = cl != null ? cl + ' / 3' : '—';
  lossEl.style.color = cl >= 2 ? 'var(--red)' : cl >= 1 ? 'var(--yellow)' : 'var(--text)';

  const cd = s.cooldown_remaining || 0;
  const cdEl = el('s-cooldown');
  if (cd > 0) {
    const m = Math.floor(cd / 60), sec = Math.ceil(cd % 60);
    cdEl.textContent = m + 'm ' + String(sec).padStart(2,'0') + 's';
    cdEl.style.color = 'var(--orange)';
  } else {
    cdEl.textContent = '—';
    cdEl.style.color = 'var(--muted)';
  }
}

/* ── Update price chart ── */
function updatePriceChart(s, priceHistory) {
  if (!priceChart) return;
  const pts = (priceHistory || []).map(([ts, p]) => ({ x: ts, y: p }));
  priceChart.data.datasets[0].data = pts;
  priceChart.data.datasets[1].data = pts;

  // Grid level annotations
  const annotations = {};
  (s.grid_levels || []).forEach(lv => {
    const active = lv.status === 'BUY_OPEN' || lv.status === 'SELL_OPEN';
    annotations['gl_' + lv.idx] = {
      type: 'line',
      scaleID: 'y',
      value: lv.price,
      borderColor: levelColor(lv.status, 1),
      borderWidth: active ? 1 : 0.5,
      borderDash: [3, 5],
      label: {
        display: active,
        content: '$' + Math.round(lv.price).toLocaleString(),
        position: 'end',
        xAdjust: -6,
        color: levelColor(lv.status, 1),
        backgroundColor: 'rgba(8,15,30,0.8)',
        font: { family: "'JetBrains Mono', monospace", size: 9 },
        padding: { x: 3, y: 2 },
        borderRadius: 2,
      },
    };
  });
  priceChart.options.plugins.annotation.annotations = annotations;

  // Zoom Y axis to actual price range so small movements are visible.
  // Minimum visible window = 0.3% of price (~$220 at $73k BTC) so even
  // quiet markets show wiggles rather than a flat line.
  if (pts.length > 1) {
    const prices = pts.map(p => p.y);
    const lo = Math.min(...prices);
    const hi = Math.max(...prices);
    const mid = (hi + lo) / 2;
    const range = Math.max(hi - lo, mid * 0.003);
    const pad = range * 0.3;
    priceChart.options.scales.y.min = mid - range / 2 - pad;
    priceChart.options.scales.y.max = mid + range / 2 + pad;
  }

  priceChart.update('none');
}

/* ── Update equity chart ── */
function updateEquityChart(portfolioHistory) {
  if (!equityChart) return;
  const pts = (portfolioHistory || []).map(([ts, v]) => ({ x: ts, y: v }));
  equityChart.data.datasets[0].data = pts;
  equityChart.update('none');
}

/* ── Update trades table ── */
function updateTrades(trades) {
  const tbody = el('trades-body');
  const tbl   = el('trades-table');
  const empty = el('trades-empty');
  const count = el('trade-count');

  if (!trades || trades.length === 0) {
    tbl.style.display = 'none';
    empty.style.display = '';
    count.textContent = '';
    return;
  }
  tbl.style.display = '';
  empty.style.display = 'none';
  count.textContent = trades.length + ' fills';

  tbody.innerHTML = trades.slice(0, 20).map(t => {
    const isSell = t.side === 'SELL';
    const pnlHtml = (isSell && t.pnl)
      ? '<span class="' + (t.pnl >= 0 ? 'pos' : 'neg') + '">' + (t.pnl >= 0 ? '+' : '') + (+t.pnl).toFixed(4) + ' USDT</span>'
      : '<span class="td-muted">—</span>';
    return '<tr>'
      + '<td class="td-muted">' + fmtTime(t.ts) + '</td>'
      + '<td class="side-' + t.side.toLowerCase() + '">' + t.side + '</td>'
      + '<td>' + fmtPrice(t.price) + '</td>'
      + '<td class="td-muted">' + fmtQty(t.qty) + ' BTC</td>'
      + '<td>' + pnlHtml + '</td>'
      + '</tr>';
  }).join('');
}

/* ── Polling ── */
let latestStatus  = {};
let latestHistory = { price_history: [], portfolio_history: [], trades: [] };

async function pollStatus() {
  try {
    const r = await fetch('/status');
    latestStatus = await r.json();
    updateHeader(latestStatus);
    updateCards(latestStatus);
    updatePriceChart(latestStatus, latestHistory.price_history);
  } catch(e) { console.warn('status poll failed', e); }
}

async function pollHistory() {
  try {
    const r = await fetch('/history');
    latestHistory = await r.json();
    updateEquityChart(latestHistory.portfolio_history);
    updateTrades(latestHistory.trades);
    updatePriceChart(latestStatus, latestHistory.price_history);
  } catch(e) { console.warn('history poll failed', e); }
}

/* ── Boot ── */
window.addEventListener('DOMContentLoaded', () => {
  initPriceChart();
  initEquityChart();
  pollStatus();
  pollHistory();
  setInterval(pollStatus,  3000);
  setInterval(pollHistory, 15000);
});
</script>
</body>
</html>"""


# ------------------------------------------------------------------ #
# Server
# ------------------------------------------------------------------ #

def _respond(writer, body: bytes, content_type: str) -> None:
    header = (
        f"HTTP/1.1 200 OK\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Cache-Control: no-cache\r\n\r\n"
    ).encode()
    writer.write(header)
    writer.write(body)


class MonitoringServer:
    def __init__(self, host: str, port: int):
        self._host = host
        self._port = port
        self._start_time = time.time()
        self._server: Optional[asyncio.Server] = None

        self._state_snapshot: dict = {}
        self._price_history:     deque = deque(maxlen=_MAX_HISTORY)   # (ts_ms, price)
        self._portfolio_history: deque = deque(maxlen=_MAX_HISTORY)   # (ts_ms, value)
        self._trade_history:     deque = deque(maxlen=_MAX_TRADES)    # {ts, side, price, qty, pnl}
        self._last_sample: float = 0.0

    # ------------------------------------------------------------------ #
    # Called by the bot
    # ------------------------------------------------------------------ #

    def update(self, snapshot: dict) -> None:
        self._state_snapshot = snapshot
        DRAWDOWN_GAUGE.set(snapshot.get("drawdown_percent", 0))
        OPEN_ORDERS_GAUGE.set(snapshot.get("open_orders", 0))
        LAST_PRICE_GAUGE.set(snapshot.get("last_price", 0))
        PORTFOLIO_GAUGE.set(snapshot.get("portfolio_value", 0))
        REGIME_GAUGE.set(1 if snapshot.get("ai_regime") == "ranging" else 0)

        now = time.time()
        if now - self._last_sample >= _SAMPLE_INTERVAL:
            price = snapshot.get("last_price", 0)
            portfolio = snapshot.get("portfolio_value", 0)
            ts_ms = int(now * 1000)
            if price > 0:
                self._price_history.append((ts_ms, price))
            if portfolio > 0:
                self._portfolio_history.append((ts_ms, portfolio))
            self._last_sample = now

    def record_trade(
        self,
        side: str,
        price: float = 0.0,
        qty: float = 0.0,
        pnl: float = 0.0,
    ) -> None:
        TRADES_TOTAL.labels(side=side).inc()
        self._trade_history.append({
            "ts":    int(time.time() * 1000),
            "side":  side,
            "price": price,
            "qty":   qty,
            "pnl":   pnl,
        })

    # ------------------------------------------------------------------ #
    # HTTP server
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port
        )
        logger.info("Monitoring server on %s:%s", self._host, self._port)
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server:
            self._server.close()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            data = await reader.read(2048)
            req  = data.decode(errors="ignore").split("\r\n")[0]
            path = req.split(" ")[1] if len(req.split(" ")) > 1 else "/"

            if path in ("/", "/index.html"):
                _respond(writer, _DASHBOARD_HTML.encode(), "text/html; charset=utf-8")

            elif path.startswith("/history"):
                body = json.dumps({
                    "price_history":     list(self._price_history),
                    "portfolio_history": list(self._portfolio_history),
                    "trades":            list(reversed(self._trade_history)),
                }).encode()
                _respond(writer, body, "application/json")

            elif path.startswith("/status"):
                UPTIME_GAUGE.set(time.time() - self._start_time)
                body = json.dumps({
                    **self._state_snapshot,
                    "uptime_seconds": time.time() - self._start_time,
                }).encode()
                _respond(writer, body, "application/json")

            elif path.startswith("/metrics"):
                body = generate_latest()
                _respond(writer, body, CONTENT_TYPE_LATEST)

            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")

            await writer.drain()
        except Exception as e:
            logger.debug("Monitoring request error: %s", e)
        finally:
            writer.close()
