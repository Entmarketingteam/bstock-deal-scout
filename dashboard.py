"""B-Stock analytics dashboard — sortable/filterable UI + resale tracker."""
from __future__ import annotations

import json
from typing import Any


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def build_analytics(listings: list[dict], history_map: dict[str, list], resales: list[dict]) -> dict:
    """Aggregate analytics from raw DB data."""
    total = len(listings)
    with_manifest = sum(1 for l in listings if l.get("has_manifest"))
    quality_scores = [_safe_float(l.get("lot_quality_score")) for l in listings if l.get("lot_quality_score")]
    avg_quality = round(sum(quality_scores) / len(quality_scores), 1) if quality_scores else 0

    # Closed = time_remaining is null/empty and has bid history
    closed = [l for l in listings if not l.get("time_remaining") and history_map.get(l["auction_id"])]
    active = [l for l in listings if l.get("time_remaining")]

    # Winning bid % of MSRP for closed lots
    win_pcts: list[float] = []
    for l in closed:
        snaps = history_map.get(l["auction_id"], [])
        if snaps:
            final_bid = _safe_float(snaps[-1].get("current_bid"))
            msrp = _safe_float(l.get("msrp"))
            if final_bid and msrp:
                win_pcts.append(round(final_bid / msrp * 100, 1))
    avg_win_pct = round(sum(win_pcts) / len(win_pcts), 1) if win_pcts else 0

    # By storefront
    sf_map: dict[str, dict] = {}
    for l in listings:
        sf = l.get("storefront") or "Unknown"
        e = sf_map.setdefault(sf, {"storefront": sf, "count": 0, "closed": 0, "win_pcts": [], "bid_counts": [], "quality": []})
        e["count"] += 1
        snaps = history_map.get(l["auction_id"], [])
        if not l.get("time_remaining") and snaps:
            e["closed"] += 1
            final_bid = _safe_float(snaps[-1].get("current_bid"))
            msrp = _safe_float(l.get("msrp"))
            if final_bid and msrp:
                e["win_pcts"].append(round(final_bid / msrp * 100, 1))
            bc = _safe_int(snaps[-1].get("bid_count"))
            if bc:
                e["bid_counts"].append(bc)
        if l.get("lot_quality_score"):
            e["quality"].append(_safe_float(l.get("lot_quality_score")))
    sf_stats = []
    for sf, e in sf_map.items():
        sf_stats.append({
            "storefront": sf,
            "total_lots": e["count"],
            "closed_lots": e["closed"],
            "avg_win_pct_msrp": round(sum(e["win_pcts"]) / len(e["win_pcts"]), 1) if e["win_pcts"] else None,
            "avg_bid_count": round(sum(e["bid_counts"]) / len(e["bid_counts"]), 1) if e["bid_counts"] else None,
            "avg_quality": round(sum(e["quality"]) / len(e["quality"]), 1) if e["quality"] else None,
        })

    # Resale ROI accuracy
    resale_stats = []
    for r in resales:
        buy = _safe_float(r.get("buy_price"))
        sell = _safe_float(r.get("sell_price"))
        if buy and sell:
            profit = sell - buy
            roi = round((profit / buy) * 100, 1)
            resale_stats.append({**r, "profit": round(profit, 2), "roi_pct": roi})

    return {
        "overview": {
            "total_tracked": total,
            "active": len(active),
            "closed": len(closed),
            "with_manifest": with_manifest,
            "avg_quality": avg_quality,
            "avg_win_pct_msrp": avg_win_pct,
        },
        "by_storefront": sf_stats,
        "resale_stats": resale_stats,
    }


def render_dashboard(listings: list[dict], history_map: dict[str, list],
                     resales: list[dict], secret: str) -> str:
    analytics = build_analytics(listings, history_map, resales)

    # Enrich listings with final bid from history
    enriched = []
    for l in listings:
        snaps = history_map.get(l["auction_id"], [])
        final_bid = None
        final_bid_count = None
        if snaps:
            last = snaps[-1]
            final_bid = last.get("current_bid")
            final_bid_count = last.get("bid_count")
        is_closed = not l.get("time_remaining")
        msrp = _safe_float(l.get("msrp"))
        bid = _safe_float(final_bid or l.get("current_bid"))
        win_pct = round(bid / msrp * 100, 1) if bid and msrp else None
        enriched.append({
            **l,
            "final_bid": final_bid,
            "final_bid_count": final_bid_count,
            "win_pct_msrp": win_pct,
            "is_closed": is_closed,
            "snap_count": len(snaps),
        })

    # Build history chart data per lot (for sparklines)
    chart_data: dict[str, list] = {}
    for aid, snaps in history_map.items():
        chart_data[aid] = [
            {"t": s.get("snapped_at", "")[:16], "b": _safe_float(s.get("current_bid")), "n": _safe_int(s.get("bid_count"))}
            for s in snaps if s.get("current_bid") is not None
        ]

    ov = analytics["overview"]
    sf_stats = analytics["by_storefront"]
    resale_stats = analytics["resale_stats"]

    listings_json = json.dumps(enriched)
    chart_json = json.dumps(chart_data)
    sf_json = json.dumps(sf_stats)
    resales_json = json.dumps(resales)

    total_resale_profit = sum(r.get("profit", 0) for r in resale_stats)
    avg_resale_roi = (
        round(sum(r.get("roi_pct", 0) for r in resale_stats) / len(resale_stats), 1)
        if resale_stats else 0
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>B-Stock Deal Scout — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f0f;color:#e8e8e8;font-size:14px}}
a{{color:inherit;text-decoration:none}}
/* Layout */
.topbar{{background:#1a1a1a;border-bottom:1px solid #2a2a2a;padding:14px 32px;display:flex;align-items:center;justify-content:space-between}}
.topbar-title{{font-size:18px;font-weight:800;letter-spacing:-0.5px}}
.topbar-sub{{font-size:12px;color:#666;margin-top:2px}}
.main{{padding:24px 32px;max-width:1600px;margin:0 auto}}
/* Stats bar */
.stats-bar{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:28px}}
.stat-card{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:16px 20px}}
.stat-val{{font-size:28px;font-weight:800;line-height:1}}
.stat-label{{font-size:11px;color:#666;text-transform:uppercase;margin-top:4px;letter-spacing:0.5px}}
.stat-card.green .stat-val{{color:#4ade80}}
.stat-card.yellow .stat-val{{color:#fbbf24}}
.stat-card.blue .stat-val{{color:#60a5fa}}
/* Tabs */
.tabs{{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid #2a2a2a;padding-bottom:0}}
.tab{{padding:10px 20px;cursor:pointer;font-weight:600;font-size:13px;color:#666;border-bottom:2px solid transparent;margin-bottom:-1px}}
.tab.active{{color:#fff;border-bottom-color:#4ade80}}
.tab:hover:not(.active){{color:#aaa}}
.tab-content{{display:none}}.tab-content.active{{display:block}}
/* Toolbar */
.toolbar{{display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap;align-items:center}}
.toolbar input,.toolbar select{{background:#1a1a1a;border:1px solid #333;border-radius:6px;padding:7px 12px;color:#e8e8e8;font-size:13px;outline:none}}
.toolbar input:focus,.toolbar select:focus{{border-color:#555}}
.toolbar input{{min-width:220px}}
.count-badge{{font-size:12px;color:#666;margin-left:auto}}
/* Table */
.tbl-wrap{{overflow-x:auto;border:1px solid #2a2a2a;border-radius:10px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:#1a1a1a;padding:10px 14px;text-align:left;font-weight:600;color:#888;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;cursor:pointer;user-select:none;white-space:nowrap;border-bottom:1px solid #2a2a2a}}
th:hover{{color:#ccc}}
th .sort-arrow{{margin-left:4px;opacity:0.4}}
th.sorted .sort-arrow{{opacity:1;color:#4ade80}}
td{{padding:10px 14px;border-bottom:1px solid #1e1e1e;vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#161616}}
.pill{{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600}}
.pill-green{{background:#14532d;color:#4ade80}}
.pill-yellow{{background:#451a03;color:#fbbf24}}
.pill-red{{background:#450a0a;color:#f87171}}
.pill-blue{{background:#1e3a5f;color:#60a5fa}}
.pill-gray{{background:#1a1a1a;color:#666;border:1px solid #333}}
.score{{font-weight:700}}
.score-high{{color:#4ade80}}
.score-mid{{color:#fbbf24}}
.score-low{{color:#f87171}}
/* Charts section */
.charts-grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:28px}}
.chart-card{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:20px}}
.chart-card h3{{font-size:13px;font-weight:700;color:#aaa;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:16px}}
.chart-wrap{{position:relative;height:220px}}
@media(max-width:900px){{.charts-grid{{grid-template-columns:1fr}}}}
/* History panel */
.history-panel{{background:#111;border:1px solid #2a2a2a;border-radius:8px;padding:16px;margin-top:8px;display:none}}
.history-panel.open{{display:block}}
.history-panel h4{{font-size:12px;color:#666;margin-bottom:10px;text-transform:uppercase}}
.history-chart-wrap{{height:120px;position:relative}}
/* Resale form */
.resale-form{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:24px;margin-bottom:24px}}
.resale-form h3{{font-size:15px;font-weight:700;margin-bottom:16px}}
.form-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.form-field label{{display:block;font-size:11px;color:#666;text-transform:uppercase;margin-bottom:5px;letter-spacing:0.5px}}
.form-field input,.form-field select,.form-field textarea{{width:100%;background:#111;border:1px solid #333;border-radius:6px;padding:8px 12px;color:#e8e8e8;font-size:13px;outline:none}}
.form-field input:focus,.form-field select:focus,.form-field textarea:focus{{border-color:#4ade80}}
.btn{{padding:9px 20px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;border:none}}
.btn-primary{{background:#4ade80;color:#0a0a0a}}
.btn-primary:hover{{background:#22c55e}}
.btn-sm{{padding:5px 12px;font-size:11px}}
/* Storefront table */
.sf-table td:first-child{{font-weight:600}}
.empty-state{{padding:48px;text-align:center;color:#444;font-size:13px}}
</style>
</head>
<body>

<div class="topbar">
  <div>
    <div class="topbar-title">B-Stock Deal Scout</div>
    <div class="topbar-sub">Analytics Dashboard · Live data from Supabase</div>
  </div>
  <div style="font-size:12px;color:#444">Auto-refreshes on page load · <a href="/lookbook-report" style="color:#4ade80">Lookbook →</a></div>
</div>

<div class="main">

<!-- Stats bar -->
<div class="stats-bar">
  <div class="stat-card blue"><div class="stat-val">{ov['total_tracked']}</div><div class="stat-label">Lots Tracked</div></div>
  <div class="stat-card green"><div class="stat-val">{ov['active']}</div><div class="stat-label">Active Now</div></div>
  <div class="stat-card"><div class="stat-val">{ov['closed']}</div><div class="stat-label">Closed (w/ history)</div></div>
  <div class="stat-card"><div class="stat-val">{ov['with_manifest']}</div><div class="stat-label">Have Manifest</div></div>
  <div class="stat-card yellow"><div class="stat-val">{ov['avg_quality']}</div><div class="stat-label">Avg Quality Score</div></div>
  <div class="stat-card green"><div class="stat-val">{f"{ov['avg_win_pct_msrp']}%" if ov['avg_win_pct_msrp'] else "—"}</div><div class="stat-label">Avg Win % of MSRP</div></div>
  <div class="stat-card green"><div class="stat-val">${total_resale_profit:,.0f}</div><div class="stat-label">Logged Resale Profit</div></div>
  <div class="stat-card yellow"><div class="stat-val">{f"{avg_resale_roi}%" if resale_stats else "—"}</div><div class="stat-label">Avg Actual ROI</div></div>
</div>

<!-- Tabs -->
<div class="tabs">
  <div class="tab active" onclick="switchTab('lots')">Active Lots</div>
  <div class="tab" onclick="switchTab('closed')">Closed Auctions</div>
  <div class="tab" onclick="switchTab('analytics')">Analytics</div>
  <div class="tab" onclick="switchTab('resale')">Resale Tracker</div>
</div>

<!-- TAB: Active Lots -->
<div id="tab-lots" class="tab-content active">
  <div class="toolbar">
    <input type="text" id="lots-search" placeholder="Search title, storefront..." oninput="filterTable('lots')">
    <select id="lots-filter-manifest" onchange="filterTable('lots')">
      <option value="">All lots</option>
      <option value="yes">Has manifest</option>
      <option value="no">No manifest</option>
    </select>
    <select id="lots-filter-condition" onchange="filterTable('lots')">
      <option value="">All conditions</option>
      <option value="new">New / Overstock</option>
      <option value="returns">Customer Returns</option>
      <option value="salvage">Salvage</option>
    </select>
    <select id="lots-sort" onchange="sortTable('lots')">
      <option value="quality_desc">Quality ↓</option>
      <option value="discount_desc">Discount % ↓</option>
      <option value="bid_asc">Bid ↑</option>
      <option value="msrp_desc">MSRP ↓</option>
      <option value="time_asc">Closing soon</option>
    </select>
    <span class="count-badge" id="lots-count"></span>
  </div>
  <div class="tbl-wrap">
    <table id="lots-table">
      <thead><tr>
        <th>Title</th>
        <th>Storefront</th>
        <th>Condition</th>
        <th>MSRP</th>
        <th>Current Bid</th>
        <th>B-Stock Fee</th>
        <th>Est. Freight</th>
        <th>Total Landed</th>
        <th>Per Unit</th>
        <th>Discount</th>
        <th>Quality</th>
        <th>Rec Max Bid</th>
        <th>Time Left</th>
        <th>Manifest</th>
        <th>Actions</th>
      </tr></thead>
      <tbody id="lots-tbody"></tbody>
    </table>
  </div>
</div>

<!-- TAB: Closed Auctions -->
<div id="tab-closed" class="tab-content">
  <div class="toolbar">
    <input type="text" id="closed-search" placeholder="Search..." oninput="filterTable('closed')">
    <select id="closed-sort" onchange="sortTable('closed')">
      <option value="win_pct_asc">Win % of MSRP ↑ (best deals)</option>
      <option value="win_pct_desc">Win % of MSRP ↓</option>
      <option value="bid_count_asc">Least competitive</option>
      <option value="bid_count_desc">Most competitive</option>
      <option value="msrp_desc">MSRP ↓</option>
    </select>
    <span class="count-badge" id="closed-count"></span>
  </div>
  <div class="tbl-wrap">
    <table>
      <thead><tr>
        <th>Title</th>
        <th>Storefront</th>
        <th>MSRP</th>
        <th>Final Bid</th>
        <th>Win % of MSRP</th>
        <th>Bid Count</th>
        <th>Snaps Captured</th>
        <th>Condition</th>
        <th>Units</th>
        <th>Bid History</th>
      </tr></thead>
      <tbody id="closed-tbody"></tbody>
    </table>
  </div>
</div>

<!-- TAB: Analytics -->
<div id="tab-analytics" class="tab-content">
  <div class="charts-grid">
    <div class="chart-card">
      <h3>Winning Bid % of MSRP by Storefront</h3>
      <div class="chart-wrap"><canvas id="chart-win-pct"></canvas></div>
      <div style="font-size:11px;color:#444;margin-top:8px">Lower = you bought cheaper. Accumulates as auctions close.</div>
    </div>
    <div class="chart-card">
      <h3>Avg Bid Count by Storefront</h3>
      <div class="chart-wrap"><canvas id="chart-bid-count"></canvas></div>
      <div style="font-size:11px;color:#444;margin-top:8px">Lower = less competition = easier to win at low price.</div>
    </div>
    <div class="chart-card">
      <h3>Avg Quality Score by Storefront</h3>
      <div class="chart-wrap"><canvas id="chart-quality"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Lots Tracked vs Closed</h3>
      <div class="chart-wrap"><canvas id="chart-coverage"></canvas></div>
    </div>
  </div>

  <div class="tbl-wrap">
    <table class="sf-table">
      <thead><tr>
        <th>Storefront</th>
        <th>Total Lots</th>
        <th>Closed w/ Data</th>
        <th>Avg Win % of MSRP</th>
        <th>Avg Bid Count at Close</th>
        <th>Avg Quality Score</th>
      </tr></thead>
      <tbody id="sf-tbody"></tbody>
    </table>
  </div>
</div>

<!-- TAB: Resale Tracker -->
<div id="tab-resale" class="tab-content">
  <div class="resale-form">
    <h3>Log a Completed Sale</h3>
    <div class="form-grid">
      <div class="form-field">
        <label>Auction ID</label>
        <input type="text" id="rs-auction-id" placeholder="e.g. khl3365">
      </div>
      <div class="form-field">
        <label>Total Buy Price (bid + fees + freight)</label>
        <input type="number" id="rs-buy" placeholder="1250.00" step="0.01">
      </div>
      <div class="form-field">
        <label>Sell Price</label>
        <input type="number" id="rs-sell" placeholder="2800.00" step="0.01">
      </div>
      <div class="form-field">
        <label>Sell Channel</label>
        <select id="rs-channel">
          <option value="facebook_marketplace">Facebook Marketplace</option>
          <option value="ebay">eBay</option>
          <option value="contractor_direct">Contractor Direct</option>
          <option value="showroom">Showroom / Trade</option>
          <option value="craigslist">Craigslist</option>
          <option value="other">Other</option>
        </select>
      </div>
      <div class="form-field">
        <label>Days to Sell</label>
        <input type="number" id="rs-days" placeholder="14" min="0">
      </div>
      <div class="form-field">
        <label>Notes</label>
        <input type="text" id="rs-notes" placeholder="Optional notes">
      </div>
    </div>
    <div style="margin-top:16px;display:flex;align-items:center;gap:12px">
      <button class="btn btn-primary" onclick="logSale()">Log Sale</button>
      <span id="rs-status" style="font-size:12px;color:#666"></span>
    </div>
  </div>

  <div class="tbl-wrap">
    <table>
      <thead><tr>
        <th>Auction ID</th>
        <th>Buy Price</th>
        <th>Sell Price</th>
        <th>Profit</th>
        <th>ROI</th>
        <th>Channel</th>
        <th>Days to Sell</th>
        <th>Notes</th>
        <th>Date</th>
      </tr></thead>
      <tbody id="resale-tbody"></tbody>
    </table>
  </div>
</div>

</div><!-- /main -->

<div id="history-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.8);z-index:1000;align-items:center;justify-content:center">
  <div style="background:#1a1a1a;border:1px solid #333;border-radius:12px;padding:24px;width:600px;max-width:90vw">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <div id="modal-title" style="font-weight:700;font-size:15px"></div>
      <button onclick="closeModal()" style="background:none;border:none;color:#666;font-size:20px;cursor:pointer">×</button>
    </div>
    <div style="position:relative;height:200px"><canvas id="modal-chart"></canvas></div>
    <div id="modal-stats" style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-top:16px;font-size:12px"></div>
  </div>
</div>

<script>
const LISTINGS = {listings_json};
const CHART_DATA = {chart_json};
const SF_STATS = {sf_json};
const RESALES = {resales_json};
const SECRET = {json.dumps(secret)};

let modalChart = null;

// ── Tab switching ──────────────────────────────────────────────────
function switchTab(name) {{
  document.querySelectorAll('.tab').forEach((t,i) => {{
    const names = ['lots','closed','analytics','resale'];
    t.classList.toggle('active', names[i] === name);
  }});
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'analytics') renderCharts();
}}

// ── Lots table ─────────────────────────────────────────────────────
const active = LISTINGS.filter(l => l.time_remaining);
const closed = LISTINGS.filter(l => l.is_closed);
let filteredLots = [...active];
let filteredClosed = [...closed];

function scoreClass(s) {{
  if (!s) return '';
  if (s >= 7.5) return 'score-high';
  if (s >= 5) return 'score-mid';
  return 'score-low';
}}

function verdictPill(v) {{
  if (!v) return '<span class="pill pill-gray">—</span>';
  if (v.includes('BID ✅')) return '<span class="pill pill-green">BID</span>';
  if (v.includes('⚠️')) return '<span class="pill pill-yellow">NEAR LIMIT</span>';
  if (v.includes('❌')) return '<span class="pill pill-red">PASS</span>';
  return '<span class="pill pill-gray">' + v.slice(0,12) + '</span>';
}}

function fmt(n, prefix='$') {{
  if (n == null) return '—';
  return prefix + parseFloat(n).toLocaleString('en-US', {{maximumFractionDigits:0}});
}}

function renderLotsTable() {{
  const tbody = document.getElementById('lots-tbody');
  if (!filteredLots.length) {{
    tbody.innerHTML = '<tr><td colspan="15" class="empty-state">No active lots found</td></tr>';
    document.getElementById('lots-count').textContent = '0 lots';
    return;
  }}
  document.getElementById('lots-count').textContent = filteredLots.length + ' lots';
  tbody.innerHTML = filteredLots.map(l => {{
    const msrp = parseFloat(l.msrp||0);
    const bid = parseFloat(l.current_bid||0);
    const fee = bid * (l.storefront?.toLowerCase().includes('winston') ? 0.10 : 0.15);
    const ship = parseFloat(l.shipping_estimate||300);
    const total = bid + fee + ship;
    const units = parseInt(l.unit_count||1);
    const perUnit = units ? (total/units) : 0;
    const discount = msrp ? Math.round((1 - total/msrp)*100) : 0;
    const qs = parseFloat(l.lot_quality_score||0);
    const rec = l.recommended_max_bid;
    const cond = (l.condition||'').toLowerCase();
    const condBadge = cond.includes('new')||cond.includes('overstock')
      ? '<span class="pill pill-green">New</span>'
      : cond.includes('salvage')
      ? '<span class="pill pill-red">Salvage</span>'
      : cond.includes('return')
      ? '<span class="pill pill-yellow">Returns</span>'
      : '<span style="color:#666">—</span>';
    return `<tr>
      <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{l.title||''}}">
        ${{l.title ? l.title.slice(0,55) + (l.title.length>55?'…':'') : '—'}}
      </td>
      <td style="white-space:nowrap;color:#888">${{l.storefront||'—'}}</td>
      <td>${{condBadge}}</td>
      <td>${{fmt(l.msrp)}}</td>
      <td style="font-weight:700">${{fmt(l.current_bid)}}</td>
      <td style="color:#888">${{fmt(fee)}}</td>
      <td style="color:#888">${{fmt(ship)}}</td>
      <td style="font-weight:700;color:#fbbf24">${{fmt(total)}}</td>
      <td>${{fmt(perUnit)}}</td>
      <td><span class="pill ${{discount>=75?'pill-green':discount>=50?'pill-yellow':'pill-gray'}}">${{discount}}% off</span></td>
      <td><span class="score ${{scoreClass(qs)}}">${{qs||'—'}}</span></td>
      <td style="color:#4ade80">${{rec ? fmt(rec) : '—'}}</td>
      <td style="font-size:12px;color:#888">${{l.time_remaining||'—'}}</td>
      <td>${{l.has_manifest ? '<span class="pill pill-green">✓</span>' : '<span class="pill pill-gray">—</span>'}}</td>
      <td><button class="btn btn-sm btn-primary" onclick="showHistory('${{l.auction_id}}','${{(l.title||'').replace(/'/g,'').slice(0,40)}}')">History</button></td>
    </tr>`;
  }}).join('');
}}

function renderClosedTable() {{
  const tbody = document.getElementById('closed-tbody');
  if (!filteredClosed.length) {{
    tbody.innerHTML = '<tr><td colspan="10" class="empty-state">No closed lots with history yet — data accumulates as auctions close</td></tr>';
    document.getElementById('closed-count').textContent = '0 closed';
    return;
  }}
  document.getElementById('closed-count').textContent = filteredClosed.length + ' closed';
  tbody.innerHTML = filteredClosed.map(l => {{
    const winPct = l.win_pct_msrp;
    const pctClass = winPct ? (winPct < 10 ? 'score-high' : winPct < 25 ? 'score-mid' : 'score-low') : '';
    const bidCount = l.final_bid_count;
    const bcClass = bidCount != null ? (bidCount <= 2 ? 'score-high' : bidCount <= 5 ? 'score-mid' : 'score-low') : '';
    return `<tr>
      <td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{l.title?.slice(0,55)||'—'}}</td>
      <td style="color:#888">${{l.storefront||'—'}}</td>
      <td>${{fmt(l.msrp)}}</td>
      <td style="font-weight:700">${{fmt(l.final_bid||l.current_bid)}}</td>
      <td><span class="score ${{pctClass}}">${{winPct != null ? winPct+'%' : '—'}}</span></td>
      <td><span class="score ${{bcClass}}">${{bidCount != null ? bidCount : '—'}}</span></td>
      <td style="color:#666">${{l.snap_count}} snaps</td>
      <td style="color:#888">${{l.condition||'—'}}</td>
      <td style="color:#888">${{l.unit_count||'—'}}</td>
      <td><button class="btn btn-sm btn-primary" onclick="showHistory('${{l.auction_id}}','${{(l.title||'').replace(/'/g,'').slice(0,40)}}')">Chart</button></td>
    </tr>`;
  }}).join('');
}}

function filterTable(which) {{
  if (which === 'lots') {{
    const q = document.getElementById('lots-search').value.toLowerCase();
    const mf = document.getElementById('lots-filter-manifest').value;
    const cf = document.getElementById('lots-filter-condition').value;
    filteredLots = active.filter(l => {{
      const matchQ = !q || (l.title||'').toLowerCase().includes(q) || (l.storefront||'').toLowerCase().includes(q);
      const matchM = !mf || (mf==='yes' ? l.has_manifest : !l.has_manifest);
      const cond = (l.condition||'').toLowerCase();
      const matchC = !cf || (
        cf==='new' ? (cond.includes('new') || cond.includes('overstock')) :
        cf==='returns' ? cond.includes('return') :
        cf==='salvage' ? cond.includes('salvage') : true
      );
      return matchQ && matchM && matchC;
    }});
    sortTable('lots');
  }} else {{
    const q = document.getElementById('closed-search').value.toLowerCase();
    filteredClosed = closed.filter(l => !q || (l.title||'').toLowerCase().includes(q) || (l.storefront||'').toLowerCase().includes(q));
    sortTable('closed');
  }}
}}

function sortTable(which) {{
  if (which === 'lots') {{
    const s = document.getElementById('lots-sort').value;
    filteredLots.sort((a,b) => {{
      if (s==='quality_desc') return (parseFloat(b.lot_quality_score||0)) - (parseFloat(a.lot_quality_score||0));
      if (s==='discount_desc') {{
        const da = parseFloat(a.msrp||0) ? (1 - parseFloat(a.current_bid||0)/parseFloat(a.msrp)) : 0;
        const db = parseFloat(b.msrp||0) ? (1 - parseFloat(b.current_bid||0)/parseFloat(b.msrp)) : 0;
        return db - da;
      }}
      if (s==='bid_asc') return parseFloat(a.current_bid||0) - parseFloat(b.current_bid||0);
      if (s==='msrp_desc') return parseFloat(b.msrp||0) - parseFloat(a.msrp||0);
      if (s==='time_asc') return (a.time_remaining||'zzz').localeCompare(b.time_remaining||'zzz');
      return 0;
    }});
    renderLotsTable();
  }} else {{
    const s = document.getElementById('closed-sort').value;
    filteredClosed.sort((a,b) => {{
      if (s==='win_pct_asc') return (a.win_pct_msrp||999) - (b.win_pct_msrp||999);
      if (s==='win_pct_desc') return (b.win_pct_msrp||0) - (a.win_pct_msrp||0);
      if (s==='bid_count_asc') return (a.final_bid_count??999) - (b.final_bid_count??999);
      if (s==='bid_count_desc') return (b.final_bid_count??0) - (a.final_bid_count??0);
      if (s==='msrp_desc') return parseFloat(b.msrp||0) - parseFloat(a.msrp||0);
      return 0;
    }});
    renderClosedTable();
  }}
}}

// ── Bid history modal ──────────────────────────────────────────────
function showHistory(aid, title) {{
  const snaps = CHART_DATA[aid] || [];
  document.getElementById('modal-title').textContent = title || aid;
  document.getElementById('history-modal').style.display = 'flex';
  if (modalChart) {{ modalChart.destroy(); modalChart = null; }}
  if (!snaps.length) {{
    document.getElementById('modal-stats').innerHTML = '<div style="color:#666;grid-column:span 3">No bid history captured yet</div>';
    return;
  }}
  const labels = snaps.map(s => s.t.slice(11));
  const bids = snaps.map(s => s.b);
  const counts = snaps.map(s => s.n);
  const ctx = document.getElementById('modal-chart').getContext('2d');
  modalChart = new Chart(ctx, {{
    type: 'line',
    data: {{
      labels,
      datasets: [
        {{label:'Bid $', data:bids, borderColor:'#4ade80', backgroundColor:'rgba(74,222,128,0.1)', tension:0.3, yAxisID:'y'}},
        {{label:'Bid Count', data:counts, borderColor:'#60a5fa', backgroundColor:'rgba(96,165,250,0.1)', tension:0.3, yAxisID:'y2'}}
      ]
    }},
    options: {{
      responsive:true, maintainAspectRatio:false,
      plugins:{{legend:{{labels:{{color:'#888',font:{{size:11}}}}}}}},
      scales:{{
        x:{{ticks:{{color:'#555',maxTicksLimit:8}},grid:{{color:'#1e1e1e'}}}},
        y:{{ticks:{{color:'#888',callback:v=>'$'+v.toLocaleString()}},grid:{{color:'#1e1e1e'}}}},
        y2:{{position:'right',ticks:{{color:'#60a5fa'}},grid:{{display:false}}}}
      }}
    }}
  }});
  const first = bids[0]||0, last = bids[bids.length-1]||0;
  document.getElementById('modal-stats').innerHTML = `
    <div><div style="font-size:10px;color:#666;text-transform:uppercase">First Snap Bid</div><div style="font-size:16px;font-weight:700;color:#4ade80">$${{first.toLocaleString()}}</div></div>
    <div><div style="font-size:10px;color:#666;text-transform:uppercase">Last Bid</div><div style="font-size:16px;font-weight:700;color:#fbbf24">$${{last.toLocaleString()}}</div></div>
    <div><div style="font-size:10px;color:#666;text-transform:uppercase">Snapshots</div><div style="font-size:16px;font-weight:700">${{snaps.length}}</div></div>
  `;
}}
function closeModal() {{
  document.getElementById('history-modal').style.display = 'none';
  if (modalChart) {{ modalChart.destroy(); modalChart = null; }}
}}

// ── Analytics charts ───────────────────────────────────────────────
let chartsRendered = false;
function renderCharts() {{
  if (chartsRendered) return;
  chartsRendered = true;
  const sfs = SF_STATS.filter(s => s.total_lots > 0);
  const labels = sfs.map(s => s.storefront.slice(0,20));
  const COLORS = ['#4ade80','#60a5fa','#fbbf24','#f87171','#a78bfa','#34d399'];

  function makeBar(id, label, data, colorIdx) {{
    const ctx = document.getElementById(id)?.getContext('2d');
    if (!ctx) return;
    new Chart(ctx, {{
      type: 'bar',
      data: {{ labels, datasets: [{{label, data, backgroundColor: data.map((_,i) => COLORS[i%COLORS.length]+'99'), borderColor: data.map((_,i)=>COLORS[i%COLORS.length]), borderWidth:1}}] }},
      options: {{
        responsive:true, maintainAspectRatio:false,
        plugins:{{legend:{{display:false}}}},
        scales:{{
          x:{{ticks:{{color:'#666',font:{{size:11}}}},grid:{{color:'#1e1e1e'}}}},
          y:{{ticks:{{color:'#666'}},grid:{{color:'#1e1e1e'}}}}
        }}
      }}
    }});
  }}

  makeBar('chart-win-pct', 'Avg Win % of MSRP', sfs.map(s => s.avg_win_pct_msrp));
  makeBar('chart-bid-count', 'Avg Bid Count at Close', sfs.map(s => s.avg_bid_count));
  makeBar('chart-quality', 'Avg Quality Score', sfs.map(s => s.avg_quality));

  // Coverage doughnut
  const covCtx = document.getElementById('chart-coverage')?.getContext('2d');
  if (covCtx) {{
    new Chart(covCtx, {{
      type: 'doughnut',
      data: {{
        labels: ['Active','Closed w/ History','No History'],
        datasets: [{{
          data: [{ov['active']},{ov['closed']},{ov['total_tracked']-ov['active']-ov['closed']}],
          backgroundColor:['#4ade8099','#60a5fa99','#33333399'],
          borderColor:['#4ade80','#60a5fa','#444'],
          borderWidth:1
        }}]
      }},
      options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{position:'bottom',labels:{{color:'#888',font:{{size:11}}}}}}}}}}
    }});
  }}

  // Storefront table
  const sfTbody = document.getElementById('sf-tbody');
  sfTbody.innerHTML = sfs.map(s => `<tr>
    <td>${{s.storefront}}</td>
    <td>${{s.total_lots}}</td>
    <td>${{s.closed_lots}}</td>
    <td><span class="score ${{s.avg_win_pct_msrp!=null?(s.avg_win_pct_msrp<10?'score-high':s.avg_win_pct_msrp<25?'score-mid':'score-low'):''}}">${{s.avg_win_pct_msrp!=null?s.avg_win_pct_msrp+'%':'—'}}</span></td>
    <td><span class="score ${{s.avg_bid_count!=null?(s.avg_bid_count<=2?'score-high':s.avg_bid_count<=5?'score-mid':'score-low'):''}}">${{s.avg_bid_count??'—'}}</span></td>
    <td><span class="score ${{s.avg_quality!=null?(s.avg_quality>=7.5?'score-high':s.avg_quality>=5?'score-mid':'score-low'):''}}">${{s.avg_quality??'—'}}</span></td>
  </tr>`).join('');
}}

// ── Resale tracker ─────────────────────────────────────────────────
function renderResaleTable() {{
  const tbody = document.getElementById('resale-tbody');
  if (!RESALES.length) {{
    tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No sales logged yet — use the form above to track your first flip</td></tr>';
    return;
  }}
  tbody.innerHTML = RESALES.map(r => {{
    const buy = parseFloat(r.buy_price||0), sell = parseFloat(r.sell_price||0);
    const profit = sell - buy;
    const roi = buy ? Math.round((profit/buy)*100) : 0;
    return `<tr>
      <td style="font-family:monospace;font-size:12px">${{r.auction_id}}</td>
      <td>${{fmt(r.buy_price)}}</td>
      <td>${{fmt(r.sell_price)}}</td>
      <td class="${{profit>0?'score-high':'score-low'}}" style="font-weight:700">${{fmt(profit)}}</td>
      <td class="${{roi>0?'score-high':'score-low'}}" style="font-weight:700">${{roi}}%</td>
      <td style="color:#888">${{(r.sell_channel||'—').replace(/_/g,' ')}}</td>
      <td style="color:#888">${{r.days_to_sell!=null?r.days_to_sell+' days':'—'}}</td>
      <td style="color:#666;font-size:12px">${{r.notes||'—'}}</td>
      <td style="color:#444;font-size:11px">${{(r.created_at||'').slice(0,10)}}</td>
    </tr>`;
  }}).join('');
}}

async function logSale() {{
  const aid = document.getElementById('rs-auction-id').value.trim();
  const buy = document.getElementById('rs-buy').value;
  const sell = document.getElementById('rs-sell').value;
  const channel = document.getElementById('rs-channel').value;
  const days = document.getElementById('rs-days').value;
  const notes = document.getElementById('rs-notes').value;
  if (!aid || !buy || !sell) {{ document.getElementById('rs-status').textContent = '⚠ auction ID, buy price, and sell price required'; return; }}
  document.getElementById('rs-status').textContent = 'Saving...';
  const resp = await fetch('/log-sale', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json','X-Trigger-Secret':SECRET}},
    body: JSON.stringify({{auction_id:aid, buy_price:parseFloat(buy), sell_price:parseFloat(sell), sell_channel:channel, days_to_sell:days?parseInt(days):null, notes}})
  }});
  if (resp.ok) {{
    document.getElementById('rs-status').textContent = '✓ Logged!';
    setTimeout(() => location.reload(), 800);
  }} else {{
    const e = await resp.json();
    document.getElementById('rs-status').textContent = '✗ ' + (e.detail||'Error');
  }}
}}

// ── Init ───────────────────────────────────────────────────────────
renderLotsTable();
renderClosedTable();
renderResaleTable();
</script>
</body>
</html>"""
