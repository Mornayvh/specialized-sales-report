"""
Sales Dashboard — Streamlit read-only mirror of the Node app.

Reads snapshot.sqlite ONLY. That file is a deliberately-sanitised export (see
export-snapshot.js) — tokens, employee names, and sync operational state are
excluded before it is ever committed. This app never talks to Lightspeed,
never holds a credential, and has no access to the machine that produces it.

The snapshot is refreshed manually: run `npm run refresh` on the source
machine, then commit and push snapshot.sqlite. The "Data as of" line in the
band header states exactly how fresh the figures are — this is a periodic
snapshot, not a live feed.

Revenue basis mirrors server.js exactly (same formulas, ported 1:1):
  - ex-VAT, net of discounts; only completed, non-voided sales
  - line revenue = calc_subtotal - calc_line_discount - calc_transaction_discount
  - COGS = quantity * (fifo_cost if > 0 else avg_cost)
  - period basis = COALESCE(complete_time, sale_time)
  - attach rate, stock turn/cover and dead-stock formulas below are ported
    1:1 from the /api/dashboard and /api/stock handlers in server.js.
Off-Lightspeed Adjustments are included in headline figures and always
disclosed separately, since the Lightspeed-only figure is what ties to the
shop's own report.

Visual design ported from index.html (the Node app's dashboard front end):
same type system (Barlow / Barlow Condensed), same colour discipline (steel
navy is the only series colour; clay is reserved EXCLUSIVELY for unfavourable
values and is never decorative), same panel/table/bar conventions. Streamlit
renders one continuous scrolling page rather than the two fixed print pages
index.html paginates to — there is no print-CSS equivalent for a Streamlit
app — but every panel, KPI tile, bar list and table is rebuilt to match.
Native Streamlit chrome that cannot be fully re-skinned (the period-preset
and item-form radios, the date pickers) is restyled as closely as Streamlit's
own component internals allow.
"""
import html as html_lib
import hmac
import sqlite3
import datetime as dt
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).parent / "snapshot.sqlite"

st.set_page_config(page_title="Sales Dashboard", layout="wide")


def esc(s):
    return html_lib.escape(str(s), quote=True)


class Raw(str):
    """A table cell value that is already-safe HTML — skip escaping."""


# ---------------------------------------------------------------- styling
#
# Ported from index.html's :root palette and component classes 1:1 (same
# variable names, same values) so the two front ends stay in visual lockstep.
STYLE = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600;700&family=Barlow+Condensed:wght@500;600;700&display=swap');
:root {
  --bg:        #f2f2f3;
  --text:      #1d1f20;
  --muted:     #5d5d60;
  --muted-2:   #7a7a7d;
  --divider:   rgba(29,31,32,0.16);
  --rule:      #b7b7ba;
  --hair:      rgba(29,31,32,0.07);
  --track:     #e7e7ea;
  --acc:       #5980a6;
  --acc-300:   #b5cbe0;
  --acc-400:   #94bce3;
  --acc-600:   #4b7099;
  --acc-700:   #416180;
  --acc-800:   #34506b;
  --acc-900:   #1d2d3d;
  --clay:      #8c1f34;
}
html, body, [class*="css"] { font-family: "Barlow", system-ui, -apple-system, sans-serif; }
.stApp { background: #d9d9dc; }
[data-testid="stAppViewContainer"] { background: #d9d9dc; }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stMainBlockContainer"] {
  max-width: 1120px; margin: 18px auto 36px; padding: 22px 28px 28px;
  background: var(--bg); color: var(--text);
  box-shadow: 0 2px 14px rgba(0,0,0,0.16);
}
h1, h2, h3 { font-family: "Barlow Condensed", system-ui, sans-serif; }
p, span, div, td, th, label { color: var(--text); }

/* ---------- header band ---------- */
.band {
  background: var(--acc-900); color: #f5f5f8;
  margin: -22px -28px 18px; padding: 14px 28px 15px;
  display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; flex-wrap: wrap;
}
.band h1 { font-size: 34px; font-weight: 600; line-height: 1.05; letter-spacing: -0.02em; margin: 4px 0 2px; color: #fff !important; }
/* Streamlit wraps markdown headings in an auto-generated inner <span> for its
   anchor-link feature; that span is an unstyled type selector match for the
   `span { color: var(--text) }` rule below, which otherwise wins over the
   inherited white since inheritance always loses to any direct rule. */
.band h1 span { color: #fff !important; }
.kick { font-size: 9px; letter-spacing: 0.2em; text-transform: uppercase; color: var(--acc-300); }
.bandnum { font-family: "Barlow Condensed", sans-serif; font-size: 17px; font-variant-numeric: tabular-nums; color: #fff; }
.bandsub { font-size: 10px; color: var(--acc-300); font-variant-numeric: tabular-nums; }
.pdf-btn {
  font-family: "Barlow Condensed", sans-serif; font-weight: 600; font-size: 11px;
  letter-spacing: 0.05em; text-transform: uppercase; padding: 7px 14px;
  border: 1px solid var(--acc-400); border-radius: 0; background: var(--acc-400);
  color: var(--acc-900); cursor: pointer;
}
.pdf-btn:hover { background: #fff; border-color: #fff; }

.p2head { display: flex; align-items: baseline; justify-content: space-between; border-bottom: 1px solid var(--rule); padding: 22px 0 6px; margin-bottom: 4px; }
.p2head h2 { font-size: 20px; font-weight: 600; margin: 0; letter-spacing: -0.01em; }

.frame { border: 1px solid var(--divider); background: var(--bg); margin-bottom: 18px; }
.ph { display: flex; gap: 10px; align-items: baseline; justify-content: space-between; padding: 7px 10px 6px; border-bottom: 1px solid var(--divider); }
.ph h3 { font-size: 14px; font-weight: 600; line-height: 1.1; margin: 0; letter-spacing: 0.01em; }
.meta { font-size: 9.5px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); }
.pb { padding: 8px 10px 9px; }
.k { font-size: 9.5px; letter-spacing: 0.11em; text-transform: uppercase; color: var(--acc-700); }
.v { font-family: "Barlow Condensed", sans-serif; font-weight: 600; font-size: 20px; line-height: 1.1; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; margin-top: 2px; }
.f { font-size: 10px; color: var(--muted); margin-top: 2px; font-variant-numeric: tabular-nums; }
.note { font-size: 11px; line-height: 1.5; color: var(--muted); }
.empty { color: var(--muted-2); font-size: 11px; padding: 14px 0; text-align: center; }

.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 11px; margin-bottom: 12px; }
.kpis .frame { padding: 9px 11px 10px; }
.kpis .v { font-size: 20px; }
.kpis .frame:first-child .v { font-size: 22px; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 11px; }

.stats { display: flex; margin-bottom: 7px; flex-wrap: wrap; }
.stat { padding: 0 12px; border-left: 1px solid var(--divider); }
.stat:first-child { border-left: 0; padding-left: 0; }
.stat .v { font-size: 15px; }

table.dt { width: 100%; border-collapse: collapse; font-size: 11px; }
table.dt th {
  font-family: "Barlow Condensed", sans-serif; font-weight: 600; font-size: 9.5px;
  letter-spacing: 0.09em; text-transform: uppercase; color: var(--muted);
  padding: 3px 5px; border-bottom: 1px solid var(--rule); white-space: nowrap; text-align: right;
}
table.dt th:first-child, table.dt td:first-child { text-align: left; }
table.dt td { padding: 2.6px 5px; border-bottom: 1px solid var(--hair); font-variant-numeric: tabular-nums; text-align: right; }
table.dt tbody tr:last-child td { border-bottom: 0; }
td.name { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-variant-numeric: normal; }
td.rank { color: var(--muted-2); font-family: "Barlow Condensed", sans-serif; width: 18px; }

/* Base sizing for bar-row value/margin text — NOT scoped to .brow, since the
   drill-down panel builds these divs outside a .brow wrapper (Streamlit
   columns) and must still match every other panel's 11px figures exactly. */
.nm, .rv, .mg { font-size: 11px; font-variant-numeric: tabular-nums; }
.rv, .mg { text-align: right; }
.mg { color: var(--muted); }
.nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.brow { display: grid; grid-template-columns: 130px 1fr 90px 50px; gap: 8px; align-items: center; padding: 2px 0; }
.brow.total { margin-top: 8px; padding: 8px 0 2px; border-top: 1.5px solid var(--rule); }
.brow.total .nm, .brow.total .rv, .brow.total .mg { font-size: 13px; font-weight: 700; }
.brow.total .nm { font-family: "Barlow Condensed", sans-serif; letter-spacing: 0.06em; text-transform: uppercase; }
.brow.total .mg { color: var(--text); }
.track { background: var(--track); height: 9px; }
.fill { height: 9px; background: var(--acc-400); }

.crumbs { font-size: 10px; color: var(--muted-2); }
.crumbs .cur { font-weight: 700; color: var(--text); }

footer.notes { border-top: 1px solid var(--divider); padding-top: 10px; margin-top: 14px; display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

/* ---------- sidebar, restyled to match aside.filters ---------- */
section[data-testid="stSidebar"] { background: var(--bg); border-right: 1px solid var(--divider); }
section[data-testid="stSidebar"] * { font-family: "Barlow", system-ui, sans-serif; color: var(--text); }
section[data-testid="stSidebar"] h1, section[data-testid="stSidebar"] h2, section[data-testid="stSidebar"] h3 {
  font-family: "Barlow Condensed", sans-serif; text-transform: uppercase; letter-spacing: 0.05em; font-size: 14px;
}
section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] strong {
  font-family: "Barlow Condensed", sans-serif; font-size: 14px; letter-spacing: 0.01em;
}

/* Period preset — vertical segmented control (mirrors .segv). */
div[data-testid="stElementContainer"]:has(.marker-preset) + div[data-testid="stElementContainer"] div[role="radiogroup"] {
  display: flex; flex-direction: column; border: 1px solid var(--divider); gap: 0;
}
div[data-testid="stElementContainer"]:has(.marker-preset) + div[data-testid="stElementContainer"] div[role="radiogroup"] label {
  margin: 0 !important; padding: 4px 8px !important; border-bottom: 1px solid var(--divider);
  font-family: "Barlow Condensed", sans-serif; font-size: 11px; letter-spacing: 0.05em; text-transform: uppercase;
  color: var(--muted); min-height: 0 !important;
}
div[data-testid="stElementContainer"]:has(.marker-preset) + div[data-testid="stElementContainer"] div[role="radiogroup"] label:last-child { border-bottom: 0; }
div[data-testid="stElementContainer"]:has(.marker-preset) + div[data-testid="stElementContainer"] div[role="radiogroup"] label:hover { background: rgba(89,128,166,0.12); color: var(--acc-900); }
div[data-testid="stElementContainer"]:has(.marker-preset) + div[data-testid="stElementContainer"] div[role="radiogroup"] label:has(input:checked) { background: var(--acc-800); }
div[data-testid="stElementContainer"]:has(.marker-preset) + div[data-testid="stElementContainer"] div[role="radiogroup"] label:has(input:checked) p { color: #f4f4f6 !important; }
div[data-testid="stElementContainer"]:has(.marker-preset) + div[data-testid="stElementContainer"] div[role="radiogroup"] label div:not([data-testid="stMarkdownContainer"]):not(:has([data-testid="stMarkdownContainer"])) { display: none !important; }

/* Item-form toggle — horizontal segmented control (mirrors .segb). */
div[data-testid="stElementContainer"]:has(.marker-kind) + div[data-testid="stElementContainer"] div[role="radiogroup"] {
  display: inline-flex; flex-direction: row; border: 1px solid var(--divider); width: fit-content;
}
div[data-testid="stElementContainer"]:has(.marker-kind) + div[data-testid="stElementContainer"] div[role="radiogroup"] label {
  margin: 0 !important; padding: 2px 8px !important; border-right: 1px solid var(--divider); min-height: 0 !important;
  font-family: "Barlow Condensed", sans-serif; font-size: 10px; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted);
}
div[data-testid="stElementContainer"]:has(.marker-kind) + div[data-testid="stElementContainer"] div[role="radiogroup"] label:last-child { border-right: 0; }
div[data-testid="stElementContainer"]:has(.marker-kind) + div[data-testid="stElementContainer"] div[role="radiogroup"] label:has(input:checked) { background: var(--acc-800); }
div[data-testid="stElementContainer"]:has(.marker-kind) + div[data-testid="stElementContainer"] div[role="radiogroup"] label:has(input:checked) p { color: #f4f4f6 !important; }
div[data-testid="stElementContainer"]:has(.marker-kind) + div[data-testid="stElementContainer"] div[role="radiogroup"] label div:not([data-testid="stMarkdownContainer"]):not(:has([data-testid="stMarkdownContainer"])) { display: none !important; }

section[data-testid="stSidebar"] [data-testid="stDateInput"] input {
  border: 1px solid var(--divider); border-radius: 0; font-size: 11px; color: var(--text);
}

/* Bordered drill-down panel — the one container(border=True) frame in the app. */
div[data-testid="stVerticalBlock"][data-test-scroll-behavior] {
  border: 1px solid var(--divider) !important; border-radius: 0 !important; padding: 0 !important;
  background: var(--bg); gap: 2px !important;
}
div[data-testid="stVerticalBlock"][data-test-scroll-behavior] [data-testid="stElementContainer"] { margin: 0 !important; }

/* Drill / crumb buttons rendered as plain text links, matching .brow .drill and .crumbs button. */
div[data-testid="stButton"] button {
  background: none !important; border: 0 !important; padding: 0 !important; box-shadow: none !important;
  color: var(--acc-700) !important; font-family: "Barlow", sans-serif; font-size: 11px !important;
  text-decoration: underline; text-underline-offset: 2px; text-align: left !important; min-height: 0 !important;
}
div[data-testid="stButton"] button:hover { color: var(--acc-900) !important; }
div[data-testid="stButton"] button p { font-size: 11px !important; }

[data-testid="stHorizontalBlock"] { gap: 11px !important; margin-bottom: 6px; }
[data-testid="stDataFrame"] { font-family: "Barlow", sans-serif; }

/* ---------- access-code box on the login page ---------- */
div[data-testid="stTextInputRootElement"] {
  border: 1.5px solid var(--rule) !important; border-radius: 0 !important; background: #fff !important;
  box-shadow: none !important;
}
div[data-testid="stTextInputRootElement"]:focus-within { border-color: var(--acc-700) !important; }
div[data-testid="stTextInput"] input { padding: 9px 11px !important; font-size: 14px !important; }

/* ---------- print / Export to PDF ----------
   The button calls window.print() — pure client-side, no server round trip —
   and the browser's own "Save as PDF" print destination does the actual export.
   A server-rendered PDF was deliberately not built: Streamlit Community Cloud's
   free tier has no reliable way to run a headless-Chromium or Cairo/Pango PDF
   renderer, and the browser already renders this exact CSS perfectly. */
@media print {
  @page { size: A4 landscape; margin: 10mm; }
  [data-testid="stSidebar"], [data-testid="stHeader"], [data-testid="stToolbar"],
  [data-testid="stMainMenu"], .pdf-btn, .no-print { display: none !important; }
  .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] { background: #fff !important; }
  [data-testid="stMainBlockContainer"] { box-shadow: none !important; margin: 0 !important; max-width: 100% !important; }
  .frame, .kpis .frame { break-inside: avoid; }
  .p2head { break-before: page; }
}
</style>
"""
st.markdown(STYLE, unsafe_allow_html=True)


# ---------------------------------------------------------------- access gate
#
# The deployed app is reachable by anyone with the URL — Streamlit Community
# Cloud's free tier has no built-in viewer allow-list, only a paid one. This
# is real revenue/margin/budget data, so a simple shared-passphrase gate
# stands in for that: nothing below this function runs until it passes.
#
# The passphrase lives ONLY in Streamlit Cloud's secrets store (Settings ->
# Secrets in the app dashboard) or a local .streamlit/secrets.toml — never in
# the repo. hmac.compare_digest avoids leaking the passphrase length/prefix
# through response-timing differences.
def check_password():
    def on_submit():
        entered = st.session_state.get("password_input", "")
        expected = st.secrets.get("app_password", "")
        st.session_state["password_ok"] = bool(expected) and hmac.compare_digest(entered, expected)
        if "password_input" in st.session_state:
            del st.session_state["password_input"]  # never keep the passphrase in memory longer than needed

    if st.session_state.get("password_ok"):
        return True

    st.markdown(
        '''<div class="band">
          <div>
            <div class="kick">Specialized Paarl &middot; Lightspeed Retail</div>
            <h1>Sales Dashboard</h1>
          </div>
        </div>''',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="k" style="margin:4px 0 4px">Access code</div>', unsafe_allow_html=True)
    st.text_input("Access code", type="password", key="password_input", on_change=on_submit,
                  label_visibility="collapsed")
    if "password_ok" in st.session_state and not st.session_state["password_ok"]:
        st.error("Incorrect access code.")
    if not st.secrets.get("app_password"):
        st.warning(
            "No app_password is configured in Streamlit secrets — the gate cannot pass. "
            "Set it under this app's Settings -> Secrets on Streamlit Cloud."
        )
    return False


if not check_password():
    st.stop()

# ---------------------------------------------------------------- data access

@st.cache_resource
def get_conn():
    if not DB_PATH.exists():
        st.error(
            "snapshot.sqlite not found. Run `npm run refresh` on the source "
            "machine, then commit and push it."
        )
        st.stop()
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def q(sql, params=None):
    """Run a query, return a DataFrame."""
    return pd.read_sql_query(sql, get_conn(), params=params or {})


def q1(sql, params=None):
    """Run a query, return the single row as a dict (or None)."""
    cur = get_conn().execute(sql, params or {})
    row = cur.fetchone()
    return dict(row) if row else None


# Mirrors VALID_SALE_CTE in server.js exactly.
VALID_SALE_CTE = """
valid AS (
    SELECT sale_id, shop_id,
           COALESCE(complete_time, sale_time) AS ts,
           (calc_subtotal - calc_discount) AS sale_revenue
    FROM sales
    WHERE completed = 1 AND voided = 0
      AND (:from_date IS NULL OR substr(COALESCE(complete_time, sale_time), 1, 10) >= :from_date)
      AND (:to_date   IS NULL OR substr(COALESCE(complete_time, sale_time), 1, 10) <= :to_date)
),
lines AS (
    SELECT sl.sale_line_id, sl.sale_id, sl.item_id, sl.quantity,
           v.ts, v.shop_id,
           (sl.calc_subtotal - sl.calc_line_discount - sl.calc_transaction_discount) AS revenue,
           (sl.quantity * CASE WHEN sl.fifo_cost > 0 THEN sl.fifo_cost ELSE sl.avg_cost END) AS cogs,
           (sl.fifo_cost <= 0 AND sl.avg_cost <= 0) AS no_cost_on_record
    FROM sale_lines sl
    JOIN valid v ON v.sale_id = sl.sale_id
)
"""


def period_params(from_date, to_date):
    return {
        "from_date": from_date.isoformat() if from_date else None,
        "to_date": to_date.isoformat() if to_date else None,
    }


# ---------------------------------------------------------------- formatting

def money(v):
    v = 0 if v is None or (isinstance(v, float) and pd.isna(v)) else v
    return f"{round(v):,.0f}"


def zar(v):
    return f"ZAR {money(v)}"


def pct(v):
    return "—" if v is None or (isinstance(v, float) and pd.isna(v)) else f"{v:.1f}%"


def compact(v):
    v = v or 0
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.1f}bn"
    if a >= 1e6:
        return f"{v / 1e6:.1f}m"
    if a >= 1e3:
        return f"{v / 1e3:.0f}k"
    return f"{v:.0f}"


def margin_of(gp, rev):
    return (gp / rev * 100) if rev else None


def sply_delta(curr, prior, is_points=False):
    """Mirrors splyText() in index.html — signed delta text + colour, or None."""
    if prior is None or (isinstance(prior, float) and pd.isna(prior)):
        return None
    if not is_points and not prior:
        return None
    d = (curr - prior) if is_points else (curr - prior) / abs(prior) * 100
    if d is None or pd.isna(d):
        return None
    sign = "+" if d >= 0 else "−"
    suffix = "pp" if is_points else "%"
    color = "var(--acc-800)" if d >= 0 else "var(--clay)"
    return f"{sign}{abs(d):.1f}{suffix} vs SPLY", color


# ---------------------------------------------------------------- HTML builders

def kpi_tile(label, value, foot_plain=None, delta=None, bar_pct=None):
    bar_html = ""
    if bar_pct is not None:
        w = max(0, min(100, round(bar_pct)))
        bar_html = (f'<div style="margin-top:6px;height:6px;background:var(--track)">'
                    f'<div style="height:6px;background:var(--acc-600);width:{w}%"></div></div>')
    foot_bits = []
    if foot_plain:
        foot_bits.append(esc(foot_plain))
    if delta:
        text, color = delta
        foot_bits.append(f'<span style="color:{color}">{esc(text)}</span>')
    foot_html = f'<div class="f">{" &middot; ".join(foot_bits)}</div>' if foot_bits else ""
    return (f'<div class="frame"><div class="k">{esc(label)}</div><div class="v">{esc(value)}</div>'
            f'{bar_html}{foot_html}</div>')


def stats_html(items):
    parts = []
    for it in items:
        style = f' style="color:{it["color"]}"' if it.get("color") else ""
        foot = f'<div class="f">{esc(it["foot"])}</div>' if it.get("foot") else ""
        parts.append(f'<div class="stat"><div class="k">{esc(it["label"])}</div>'
                      f'<div class="v"{style}>{esc(it["value"])}</div>{foot}</div>')
    return f'<div class="stats">{"".join(parts)}</div>'


def prep_bars(rows, limit=10):
    """Totals over EVERY row, then cap the display — the total still reconciles."""
    total_rev = sum(r["revenue"] for r in rows)
    total_gp = sum(r["gross_profit"] for r in rows)
    if limit and len(rows) > limit:
        head, rest = rows[:limit - 1], rows[limit - 1:]
        rows = head + [{
            "label": f"OTHER ({len(rest)})",
            "revenue": sum(r["revenue"] for r in rest),
            "gross_profit": sum(r["gross_profit"] for r in rest),
        }]
    return rows, total_rev, total_gp


def bars_html(rows, total_rev, total_gp, empty_text="No sales in this period."):
    if not rows:
        return f'<div class="empty">{esc(empty_text)}</div>'
    max_rev = max([r["revenue"] for r in rows] + [1])
    parts = []
    for i, r in enumerate(rows):
        label = esc(r["label"])
        rev, gp = r["revenue"], r["gross_profit"]
        width = max(0.0, rev / max_rev * 100) if max_rev else 0.0
        color = "var(--acc-800)" if i == 0 else ("var(--acc-600)" if i < 3 else "var(--acc-400)")
        track_bg = "transparent" if rev < 0 else "var(--track)"
        val_style = ' style="color:var(--clay)"' if rev < 0 else ""
        parts.append(
            f'<div class="brow"><div class="nm" title="{label}">{label}</div>'
            f'<div class="track" style="background:{track_bg}"><div class="fill" style="width:{width:.2f}%;background:{color}"></div></div>'
            f'<div class="rv"{val_style}>{money(rev)}</div>'
            f'<div class="mg">{pct(margin_of(gp, rev))}</div></div>'
        )
    parts.append(
        f'<div class="brow total"><div class="nm">Total</div><div></div>'
        f'<div class="rv">{money(total_rev)}</div><div class="mg">{pct(margin_of(total_gp, total_rev))}</div></div>'
    )
    return "".join(parts)


def html_table(headers, rows, name_col=None, rank_col=None, empty_text="Nothing in this period."):
    if not rows:
        return f'<div class="empty">{esc(empty_text)}</div>'
    thead = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = []
    for r in rows:
        tds = []
        for i, c in enumerate(r):
            cls = "name" if i == name_col else ("rank" if i == rank_col else "")
            cls_attr = f' class="{cls}"' if cls else ""
            title_attr = f' title="{esc(c)}"' if i == name_col else ""
            val = c if isinstance(c, Raw) else esc(c)
            tds.append(f"<td{cls_attr}{title_attr}>{val}</td>")
        body.append(f"<tr>{''.join(tds)}</tr>")
    return f'<table class="dt"><thead><tr>{thead}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def budget_table_html(rows):
    ths = "".join(
        f'<th{" style=\"text-align:left;padding-left:10px\"" if h == "Variance" else ""}>{h}</th>'
        for h in ["Category", "Budget", "Actual", "Variance", "%"]
    )
    trs = []
    for r in rows:
        vpct = r["variance_pct"]
        w = min(100.0, abs(vpct or 0) / 25 * 100)
        neg_w = w if r["variance"] < 0 else 0.0
        pos_w = w if r["variance"] >= 0 else 0.0
        bar = (
            '<div style="display:grid;grid-template-columns:1fr 1fr;align-items:center;height:8px">'
            '<div style="display:flex;justify-content:flex-end;border-right:1px solid var(--rule);height:8px">'
            f'<div style="height:8px;background:var(--clay);width:{neg_w:.1f}%"></div></div>'
            '<div style="display:flex;height:8px">'
            f'<div style="height:8px;background:var(--acc-600);width:{pos_w:.1f}%"></div></div></div>'
        )
        pct_color = "var(--acc-800)" if r["variance"] >= 0 else "var(--clay)"
        if vpct is None or pd.isna(vpct):
            pct_text = "—"
        else:
            pct_text = f'{"+" if r["variance"] >= 0 else chr(0x2212)}{abs(vpct):.1f}%'
        trs.append(
            f'<tr><td class="name">{esc(r["category"])}</td><td>{money(r["budget"])}</td><td>{money(r["actual"])}</td>'
            f'<td style="padding-left:10px">{bar}</td>'
            f'<td style="color:{pct_color}">{pct_text}</td></tr>'
        )
    return f'<table class="dt"><thead><tr>{ths}</tr></thead><tbody>{"".join(trs)}</tbody></table>'


def frame(title, meta_text, body_html):
    meta_html = f'<div class="meta">{esc(meta_text)}</div>' if meta_text else ""
    return (f'<div class="frame"><div class="ph"><h3>{esc(title)}</h3>{meta_html}</div>'
            f'<div class="pb">{body_html}</div></div>')


# ---------------------------------------------------------------- adjustments

def get_adjustments(from_date, to_date):
    rows = q("SELECT * FROM adjustments", {})
    if rows.empty:
        return rows, 0.0, 0.0, 0.0
    if from_date:
        rows = rows[rows["date"] >= from_date.isoformat()]
    if to_date:
        rows = rows[rows["date"] <= to_date.isoformat()]
    revenue = rows["revenue"].sum()
    cost = rows["cost"].sum()
    return rows, revenue, cost, revenue - cost


# ---------------------------------------------------------------- sidebar / period

PRESETS = {
    "MTD": lambda today: (today.replace(day=1), today),
    "30d": lambda today: (today - dt.timedelta(days=29), today),
    "90d": lambda today: (today - dt.timedelta(days=89), today),
    "YTD": lambda today: (today.replace(month=1, day=1), today),
    "12m": lambda today: (today.replace(year=today.year - 1), today),
    "All time": lambda today: (None, None),
}

st.sidebar.markdown('<div style="font-family:\'Barlow Condensed\',sans-serif;font-size:16px;'
                     'font-weight:600;text-transform:none;letter-spacing:0;margin-bottom:6px">Filters</div>',
                     unsafe_allow_html=True)
# Lives in the sidebar deliberately — the top-right of the main content area is
# Streamlit's own reserved territory (header menu, deploy button, promotional
# toast nudges), all of which sit at a higher z-index and silently swallow clicks
# on anything placed there. The sidebar is a separate DOM region Streamlit never
# overlays, so this is the one placement guaranteed not to collide with its chrome.
st.sidebar.markdown(
    '<button class="pdf-btn" style="width:100%;margin-bottom:14px" onclick="window.print()">Export to PDF</button>',
    unsafe_allow_html=True,
)
today = dt.date.today()

if "preset" not in st.session_state:
    st.session_state.preset = "All time"

st.sidebar.markdown('<div class="k" style="margin-bottom:4px">Period preset</div>', unsafe_allow_html=True)
st.sidebar.markdown('<span class="marker-preset" style="display:none"></span>', unsafe_allow_html=True)
preset = st.sidebar.radio("Period preset", list(PRESETS.keys()),
                           index=list(PRESETS.keys()).index(st.session_state.preset),
                           label_visibility="collapsed")
st.session_state.preset = preset
default_from, default_to = PRESETS[preset](today)

st.sidebar.markdown('<div class="k" style="margin:12px 0 4px">Custom range</div>', unsafe_allow_html=True)
c1, c2 = st.sidebar.columns(2)
from_date = c1.date_input("From", value=default_from, format="YYYY-MM-DD") if default_from else c1.date_input("From", value=None, format="YYYY-MM-DD")
to_date = c2.date_input("To", value=default_to, format="YYYY-MM-DD") if default_to else c2.date_input("To", value=None, format="YYYY-MM-DD")

params = period_params(from_date, to_date)
showing = f"{from_date} to {to_date}" if (from_date or to_date) else "All time"
st.sidebar.markdown(
    f'<div style="border-top:1px solid var(--divider);margin-top:11px;padding-top:8px">'
    f'<div class="k" style="margin-bottom:2px">Showing</div>'
    f'<div style="font-family:\'Barlow Condensed\',sans-serif;font-size:14px;line-height:1.15">{esc(showing)}</div>'
    f'<div class="note" style="margin-top:6px">Scopes every panel except stock on hand, which is a point-in-time position.</div>'
    f'</div>',
    unsafe_allow_html=True,
)

meta_row = q1("SELECT value FROM snapshot_meta WHERE key = 'exported_at'")
counts = q1("SELECT (SELECT COUNT(*) FROM sales) AS sales, (SELECT COUNT(*) FROM sale_lines) AS sale_lines")
sync_caption = "Sync status unavailable"
if meta_row:
    stamp = meta_row["value"][:16].replace("T", " ")
    sync_caption = f"Data as of {stamp} UTC &middot; {counts['sales']:,} sales &middot; {counts['sale_lines']:,} lines &middot; refreshed manually, not live"

# ---------------------------------------------------------------- KPIs (+ SPLY)

def shift_year(d, delta):
    if d is None:
        return None
    try:
        return d.replace(year=d.year + delta)
    except ValueError:
        return d.replace(month=2, day=28, year=d.year + delta)  # 29 Feb clamp


kpi_sql = f"""
WITH {VALID_SALE_CTE}
SELECT
  (SELECT COALESCE(SUM(revenue), 0) FROM lines) AS revenue,
  (SELECT COALESCE(SUM(cogs), 0) FROM lines) AS cogs,
  (SELECT COALESCE(SUM(quantity), 0) FROM lines) AS units,
  (SELECT COUNT(*) FROM valid) AS transactions,
  (SELECT COALESCE(SUM(sale_revenue), 0) FROM valid) AS sale_level_revenue,
  (SELECT COALESCE(SUM(revenue), 0) FROM lines WHERE no_cost_on_record) AS zero_cost_revenue,
  (SELECT COALESCE(SUM(revenue), 0) FROM lines WHERE revenue < 0) AS returns_revenue,
  (SELECT COUNT(*) FROM lines WHERE revenue < 0) AS returns_lines
"""

kpis_raw = q1(kpi_sql, params)
adj_rows, adj_rev, adj_cost, adj_gp = get_adjustments(from_date, to_date)
kpis = dict(kpis_raw)
kpis["lightspeed_revenue"] = kpis["revenue"]
kpis["revenue"] += adj_rev
kpis["cogs"] += adj_cost
gp = kpis["revenue"] - kpis["cogs"]
margin = margin_of(gp, kpis["revenue"])
avg_sale = kpis["revenue"] / kpis["transactions"] if kpis["transactions"] else 0

sply = None
if from_date or to_date:
    sp_from, sp_to = shift_year(from_date, -1), shift_year(to_date, -1)
    sp_raw = q1(kpi_sql, period_params(sp_from, sp_to))
    _, sp_adj_rev, sp_adj_cost, _ = get_adjustments(sp_from, sp_to)
    sp_rev = sp_raw["revenue"] + sp_adj_rev
    sp_cogs = sp_raw["cogs"] + sp_adj_cost
    sp_gp = sp_rev - sp_cogs
    sp_margin = margin_of(sp_gp, sp_rev)
    sp_avg = sp_rev / sp_raw["transactions"] if sp_raw["transactions"] else None
    sply = {"revenue": sp_rev, "gp": sp_gp, "margin": sp_margin, "avg": sp_avg}

# ---------------------------------------------------------------- band header

date_bounds = q1(f"""
    SELECT substr(MIN(COALESCE(complete_time, sale_time)), 1, 10) lo,
           substr(MAX(COALESCE(complete_time, sale_time)), 1, 10) hi
    FROM sales WHERE completed = 1 AND voided = 0
""")
if from_date or to_date:
    period_text = f"{from_date or date_bounds['lo']} to {to_date or date_bounds['hi']}"
else:
    period_text = f"{date_bounds['lo']} to {date_bounds['hi']} (all time)"

st.markdown(
    f'''<div class="band">
      <div>
        <div class="kick">Specialized Paarl &middot; Lightspeed Retail</div>
        <h1>Sales Dashboard</h1>
        <div class="bandsub">{esc(preset)} &middot; {esc(period_text)} &middot; ZAR, ex-VAT and net of discounts</div>
      </div>
      <div style="text-align:right">
        <div class="kick">Revenue &middot; gross profit &middot; margin</div>
        <div class="bandnum">{zar(kpis['revenue'])} &middot; {zar(gp)} &middot; {pct(margin)}</div>
        <div class="bandsub">{sync_caption}</div>
      </div>
    </div>''',
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------- KPI tiles

tiles = [
    kpi_tile("Revenue (ex-VAT, net of discount)", zar(kpis["revenue"]),
             foot_plain=f"{kpis['transactions']:,} transactions",
             delta=sply_delta(kpis["revenue"], sply["revenue"]) if sply else None),
    kpi_tile("Gross profit", zar(gp),
             foot_plain=f"Revenue less ZAR {compact(kpis['cogs'])} COGS",
             delta=sply_delta(gp, sply["gp"]) if sply else None),
    kpi_tile("Gross margin", pct(margin), bar_pct=margin,
             delta=sply_delta(margin, sply["margin"], is_points=True) if sply else None),
    kpi_tile("Average sale value", zar(avg_sale),
             foot_plain=f"{kpis['units']:,.0f} units sold",
             delta=sply_delta(avg_sale, sply["avg"]) if sply else None),
]
st.markdown(f'<div class="kpis">{"".join(tiles)}</div>', unsafe_allow_html=True)

# ---------------------------------------------------------------- category & budget

by_category = q(f"""
    WITH {VALID_SALE_CTE}
    SELECT COALESCE(c.top_level_name, 'Uncategorised') AS category,
           SUM(l.revenue) AS revenue,
           SUM(l.revenue) - SUM(l.cogs) AS gross_profit,
           SUM(l.quantity) AS units
    FROM lines l
    LEFT JOIN items i ON i.item_id = l.item_id
    LEFT JOIN categories c ON c.category_id = i.category_id
    GROUP BY category
    HAVING SUM(l.revenue) <> 0
    ORDER BY revenue DESC
""", params)
if not adj_rows.empty:
    item_cat_map = q("""
        SELECT UPPER(TRIM(i.description)) d, COALESCE(c.top_level_name,'Uncategorised') cat
        FROM items i LEFT JOIN categories c ON c.category_id = i.category_id
    """).drop_duplicates("d").set_index("d")["cat"].to_dict()
    add = {}
    for _, r in adj_rows.iterrows():
        cat = item_cat_map.get(str(r["description"]).upper().strip(), "Unmatched (adjustments)")
        a = add.setdefault(cat, {"revenue": 0.0, "gross_profit": 0.0, "units": 0.0})
        a["revenue"] += r["revenue"]; a["gross_profit"] += r["revenue"] - r["cost"]; a["units"] += r["qty"]
    for cat, a in add.items():
        if cat in by_category["category"].values:
            idx = by_category.index[by_category["category"] == cat][0]
            by_category.loc[idx, "revenue"] += a["revenue"]
            by_category.loc[idx, "gross_profit"] += a["gross_profit"]
            by_category.loc[idx, "units"] += a["units"]
        else:
            by_category = pd.concat([by_category, pd.DataFrame([{**a, "category": cat}])], ignore_index=True)
    by_category = by_category[by_category["revenue"] != 0].sort_values("revenue", ascending=False)

cat_rows = [{"label": r["category"], "revenue": r["revenue"], "gross_profit": r["gross_profit"]}
            for _, r in by_category.iterrows()]
shown, tot_rev, tot_gp = prep_bars(cat_rows, limit=10)
cat_body = bars_html(shown, tot_rev, tot_gp)

budget_all = q("SELECT * FROM budget")
if budget_all.empty:
    budget_body = '<div class="empty">No budget data in the snapshot.</div>'
    budget_meta = "—"
else:
    categories = sorted(budget_all["category"].unique())
    months = sorted(budget_all["month"].unique())
    budget_first = f"{months[0]}-01"
    by, bm = (int(x) for x in months[-1].split("-"))
    budget_last = (dt.date(by, bm % 12 + 1, 1) - dt.timedelta(days=1)) if bm < 12 else dt.date(by, 12, 31)

    data_range = q1("""
        SELECT substr(MIN(COALESCE(complete_time, sale_time)), 1, 10) lo,
               substr(MAX(COALESCE(complete_time, sale_time)), 1, 10) hi
        FROM sales WHERE completed = 1 AND voided = 0
    """)

    eff_from = max(filter(None, [from_date.isoformat() if from_date else None, budget_first, data_range["lo"]]))
    eff_to = min(filter(None, [to_date.isoformat() if to_date else None, budget_last.isoformat(), data_range["hi"]]))

    def prorate(row_from, row_to, req_from, req_to):
        lo = max(row_from, req_from)
        hi = min(row_to, req_to)
        if hi < lo:
            return 0.0
        days_total = (row_to - row_from).days + 1
        days_overlap = (hi - lo).days + 1
        return min(1.0, days_overlap / days_total)

    prorated = {}
    for _, r in budget_all.iterrows():
        y, m = (int(x) for x in r["month"].split("-"))
        m_start = dt.date(y, m, 1)
        m_end = (dt.date(y, m + 1, 1) - dt.timedelta(days=1)) if m < 12 else dt.date(y, 12, 31)
        share = prorate(m_start, m_end, dt.date.fromisoformat(eff_from), dt.date.fromisoformat(eff_to))
        prorated[r["category"]] = prorated.get(r["category"], 0) + r["amount"] * share

    actual_by_cat = q(f"""
        WITH {VALID_SALE_CTE}
        SELECT COALESCE(c.top_level_name, 'Uncategorised') AS category,
               SUM(l.revenue) AS revenue
        FROM lines l
        LEFT JOIN items i ON i.item_id = l.item_id
        LEFT JOIN categories c ON c.category_id = i.category_id
        GROUP BY category
    """, {"from_date": eff_from, "to_date": eff_to})
    actual_map = dict(zip(actual_by_cat["category"], actual_by_cat["revenue"]))

    adj_win_rows, _, _, _ = get_adjustments(dt.date.fromisoformat(eff_from), dt.date.fromisoformat(eff_to))
    if not adj_win_rows.empty:
        item_cat = q("""
            SELECT UPPER(TRIM(i.description)) d, COALESCE(c.top_level_name,'Uncategorised') cat
            FROM items i LEFT JOIN categories c ON c.category_id = i.category_id
        """).drop_duplicates("d").set_index("d")["cat"].to_dict()
        for _, r in adj_win_rows.iterrows():
            cat = item_cat.get(str(r["description"]).upper().strip())
            if cat:
                actual_map[cat] = actual_map.get(cat, 0) + r["revenue"]

    comparison = []
    for cat in categories:
        b = prorated.get(cat, 0.0)
        a = actual_map.get(cat, 0.0)
        comparison.append({"category": cat, "budget": b, "actual": a, "variance": a - b,
                            "variance_pct": ((a - b) / b * 100) if b else None})
    comp_sorted = sorted(comparison, key=lambda r: r["budget"], reverse=True)
    unbudgeted = sum(v for k, v in actual_map.items() if k not in categories)

    tb = sum(r["budget"] for r in comp_sorted)
    ta = sum(r["actual"] for r in comp_sorted)
    tv = ta - tb
    tv_pct = (tv / tb * 100) if tb else None

    budget_meta = f"{eff_from} to {eff_to}"
    stats = stats_html([
        {"label": "Budget for the period", "value": zar(tb)},
        {"label": "Actual", "value": zar(ta), "foot": "Includes off-Lightspeed adjustments"},
        {"label": "Variance", "value": f'{"+" if tv >= 0 else chr(0x2212)}{zar(abs(tv))}',
         "color": "var(--acc-800)" if tv >= 0 else "var(--clay)",
         "foot": f'{pct(tv_pct)} vs budget' if tv_pct is not None else None},
    ])
    note_bits = [f"Only the {len(categories)} budgeted categories are compared; partial months prorated by day."]
    if unbudgeted:
        note_bits.append(f"A further {zar(unbudgeted)} sits in unbudgeted categories — context, not variance.")
    budget_body = stats + budget_table_html(comp_sorted) + f'<div class="note" style="margin-top:6px">{" ".join(note_bits)}</div>'

col1, col2 = st.columns(2, gap="small")
col1.markdown(frame("Sales by category", "Revenue · margin", cat_body), unsafe_allow_html=True)
col2.markdown(frame("Budget vs actual", budget_meta, budget_body), unsafe_allow_html=True)

# ---------------------------------------------------------------- bike model drill-down + attach rate

if "drill_model" not in st.session_state:
    st.session_state.drill_model = None
    st.session_state.drill_trim = None

model_col, attach_col = st.columns(2, gap="small")

with model_col:
    with st.container(border=True):
        level = "model"
        drill_params = dict(params)
        where_extra = ""
        if st.session_state.drill_model:
            where_extra += " AND im.model = :drill_model"
            drill_params["drill_model"] = st.session_state.drill_model
            if st.session_state.drill_trim:
                level = "size"
                group_expr = "COALESCE(im.size, '(no size recorded)')"
                if st.session_state.drill_trim == "(no trim recorded)":
                    where_extra += " AND im.trim IS NULL"
                else:
                    where_extra += " AND im.trim = :drill_trim"
                    drill_params["drill_trim"] = st.session_state.drill_trim
            else:
                level = "trim"
                group_expr = "COALESCE(im.trim, '(no trim recorded)')"
        else:
            group_expr = "im.model"

        st.markdown(f'<div class="ph"><h3>Bike sales by model</h3><div class="meta">Revenue &middot; margin</div></div>',
                    unsafe_allow_html=True)
        st.markdown('<div class="pb">', unsafe_allow_html=True)

        crumb_row = st.columns([1, 1, 2], gap="small")
        if crumb_row[0].button("All models", disabled=not st.session_state.drill_model, key="crumb_all"):
            st.session_state.drill_model = None
            st.session_state.drill_trim = None
            st.rerun()
        if st.session_state.drill_model:
            if crumb_row[1].button(st.session_state.drill_model, disabled=not st.session_state.drill_trim, key="crumb_model"):
                st.session_state.drill_trim = None
                st.rerun()
        if st.session_state.drill_trim:
            crumb_row[2].markdown(f'<div class="crumbs" style="padding-top:6px">{esc(st.session_state.drill_trim)}</div>', unsafe_allow_html=True)

        st.markdown('<span class="marker-kind" style="display:none"></span>', unsafe_allow_html=True)
        kind_filter = st.radio("Form", ["Complete bikes", "All forms"], horizontal=True, label_visibility="collapsed", key="kind_filter")
        kind_clause = "" if kind_filter == "All forms" else "AND im.kind = 'complete'"

        model_df = q(f"""
            WITH {VALID_SALE_CTE}
            SELECT {group_expr} AS label,
                   SUM(l.revenue) AS revenue,
                   SUM(l.revenue) - SUM(l.cogs) AS gross_profit,
                   SUM(l.quantity) AS units
            FROM lines l
            JOIN item_models im ON im.item_id = l.item_id
            WHERE im.model IS NOT NULL {kind_clause} {where_extra}
            GROUP BY label
            HAVING SUM(l.revenue) <> 0
            ORDER BY revenue DESC
        """, drill_params)

        coverage = q1(f"""
            WITH {VALID_SALE_CTE}
            SELECT
              (SELECT COALESCE(SUM(l.revenue),0) FROM lines l JOIN item_models im ON im.item_id=l.item_id WHERE im.kind='complete') AS complete_rev,
              (SELECT COALESCE(SUM(l.revenue),0) FROM lines l JOIN item_models im ON im.item_id=l.item_id WHERE im.kind<>'complete') AS other_rev
        """, params)

        if level == "model" and kind_filter == "Complete bikes" and coverage["other_rev"]:
            st.markdown(
                f'<div class="note" style="margin-bottom:4px">Complete bikes only: {zar(coverage["complete_rev"])}. '
                f'A further {zar(coverage["other_rev"])} of bike-category revenue is framesets, bare frames, '
                f'build kits and rentals — switch to "All forms" to include it.</div>',
                unsafe_allow_html=True,
            )

        model_rows = [{"label": r["label"], "revenue": r["revenue"], "gross_profit": r["gross_profit"]}
                      for _, r in model_df.iterrows()]

        if model_rows and level != "size":
            shown_m, tot_rev_m, tot_gp_m = prep_bars(model_rows, limit=10)
            max_rev = max([r["revenue"] for r in shown_m] + [1])
            for i, r in enumerate(shown_m):
                is_other = r["label"].startswith("OTHER (")
                row_cols = st.columns([1, 2], gap="small")
                if is_other:
                    row_cols[0].markdown(f'<div class="nm" style="padding:3px 0">{esc(r["label"])}</div>', unsafe_allow_html=True)
                else:
                    if row_cols[0].button(r["label"], key=f"drill_{level}_{i}_{r['label']}"):
                        if level == "model":
                            st.session_state.drill_model = r["label"]
                        else:
                            st.session_state.drill_trim = r["label"]
                        st.rerun()
                width = max(0.0, r["revenue"] / max_rev * 100) if max_rev else 0.0
                color = "var(--acc-800)" if i == 0 else ("var(--acc-600)" if i < 3 else "var(--acc-400)")
                row_cols[1].markdown(
                    f'<div style="display:flex;align-items:center;gap:8px;padding:3px 0">'
                    f'<div class="track" style="flex:1 1 auto"><div class="fill" style="width:{width:.2f}%;background:{color}"></div></div>'
                    f'<div class="rv" style="flex:0 0 auto;white-space:nowrap">{money(r["revenue"])}</div>'
                    f'<div class="mg" style="flex:0 0 auto;white-space:nowrap;width:40px">{pct(margin_of(r["gross_profit"], r["revenue"]))}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            st.markdown(
                f'<div class="brow total"><div class="nm">Total</div><div></div>'
                f'<div class="rv">{money(tot_rev_m)}</div><div class="mg">{pct(margin_of(tot_gp_m, tot_rev_m))}</div></div>',
                unsafe_allow_html=True,
            )
        else:
            shown_m, tot_rev_m, tot_gp_m = prep_bars(model_rows, limit=10)
            st.markdown(bars_html(shown_m, tot_rev_m, tot_gp_m, empty_text="No bike sales in this period."),
                        unsafe_allow_html=True)

        st.markdown('</div>', unsafe_allow_html=True)

with attach_col:
    attach_summary = q1(f"""
        WITH {VALID_SALE_CTE},
        bike_sales AS (
            SELECT DISTINCT l.sale_id FROM lines l
            JOIN item_models im ON im.item_id = l.item_id WHERE im.kind = 'complete'
        ),
        attached AS (
            SELECT l.sale_id, l.revenue, l.quantity
            FROM bike_sales bs JOIN lines l ON l.sale_id = bs.sale_id
            LEFT JOIN items i ON i.item_id = l.item_id
            LEFT JOIN categories c ON c.category_id = i.category_id
            WHERE c.top_level_name IS NULL OR c.top_level_name NOT IN ('BIKE','TURBO')
        )
        SELECT (SELECT COUNT(*) FROM bike_sales) AS bike_transactions,
               (SELECT COUNT(DISTINCT sale_id) FROM attached WHERE revenue > 0) AS transactions_with_attachment,
               (SELECT COALESCE(SUM(revenue), 0) FROM attached) AS attached_revenue,
               (SELECT COALESCE(SUM(quantity), 0) FROM attached) AS attached_units
    """, params)

    attach_by_cat = q(f"""
        WITH {VALID_SALE_CTE},
        bike_sales AS (
            SELECT DISTINCT l.sale_id FROM lines l
            JOIN item_models im ON im.item_id = l.item_id WHERE im.kind = 'complete'
        )
        SELECT COALESCE(c.top_level_name, 'Uncategorised') AS category,
               SUM(l.revenue) AS revenue,
               SUM(l.revenue) - SUM(l.cogs) AS gross_profit,
               SUM(l.quantity) AS units
        FROM bike_sales bs JOIN lines l ON l.sale_id = bs.sale_id
        LEFT JOIN items i ON i.item_id = l.item_id
        LEFT JOIN categories c ON c.category_id = i.category_id
        WHERE c.top_level_name IS NULL OR c.top_level_name NOT IN ('BIKE','TURBO')
        GROUP BY category
        HAVING SUM(l.revenue) <> 0
        ORDER BY revenue DESC
    """, params)

    txns = attach_summary["bike_transactions"] or 0
    with_attach = attach_summary["transactions_with_attachment"] or 0
    attach_stats = stats_html([
        {"label": "Bike sales", "value": f"{txns:,.0f}"},
        {"label": "With accessory", "value": pct(with_attach / txns * 100 if txns else 0), "color": "var(--acc-700)",
         "foot": f"{with_attach:,.0f} of {txns:,.0f}"},
        {"label": "Attached revenue", "value": zar(attach_summary["attached_revenue"]),
         "foot": f"{attach_summary['attached_units']:,.0f} units"},
        {"label": "Per bike", "value": zar(attach_summary["attached_revenue"] / txns if txns else 0)},
    ])
    attach_rows = [[r["category"], f'{r["units"]:,.0f}', money(r["revenue"]), money(r["gross_profit"]),
                    pct(margin_of(r["gross_profit"], r["revenue"]))]
                   for _, r in attach_by_cat.head(6).iterrows()]
    attach_table = html_table(["Attached category", "Units", "Revenue", "GP", "Margin"], attach_rows,
                               name_col=0, empty_text="No attached sales in this period.")
    st.markdown(frame("Accessory attach rate on bike sales", "Same transaction", attach_stats + attach_table),
                unsafe_allow_html=True)

# ---------------------------------------------------------------- page 2 divider

st.markdown(
    f'<div class="p2head"><h2>Sales Dashboard — breakdowns</h2>'
    f'<div class="meta">Specialized Paarl &middot; {esc(preset)}</div></div>',
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------- top products & discount leakage

top_products = q(f"""
    WITH {VALID_SALE_CTE}
    SELECT COALESCE(i.description, '(unknown item)') AS product,
           COALESCE(c.top_level_name, 'Uncategorised') AS category,
           SUM(l.quantity) AS units, SUM(l.revenue) AS revenue,
           SUM(l.revenue) - SUM(l.cogs) AS gross_profit
    FROM lines l
    LEFT JOIN items i ON i.item_id = l.item_id
    LEFT JOIN categories c ON c.category_id = i.category_id
    GROUP BY l.item_id
    ORDER BY revenue DESC LIMIT 10
""", params)
top_rows = [[str(i + 1), r["product"], f'{r["units"]:,.0f}', money(r["revenue"]), money(r["gross_profit"]),
             pct(margin_of(r["gross_profit"], r["revenue"]))]
            for i, (_, r) in enumerate(top_products.iterrows())]
top_body = html_table(["#", "Product", "Units", "Revenue", "GP", "Margin"], top_rows,
                       rank_col=0, name_col=1, empty_text="No sales in this period.")

# Shopify sales breakdown. sales.reference_number_source is Lightspeed's own
# field, documented as "name of the external system for referenceNumber" on the
# Sale resource (developers.lightspeedhq.com) — e.g. Shopify or Hubtiger when a
# sale was pushed in via that integration, blank for a native POS sale.
#
# NOTE: an earlier version of this panel queried a sale_lines.source column that
# does not exist on Lightspeed's API (confirmed against their docs after it came
# back empty against real data) — this is a SALE-level field, joined in below via
# sales, not sale_lines. Was added to the sync pipeline (db.js/sync.js) alongside
# this panel; an OLD snapshot.sqlite predating that fix won't have the column,
# and a full `npm run sync` (not incremental) is needed once to backfill history.
sales_cols = set(q("PRAGMA table_info(sales)")["name"])
if "reference_number_source" not in sales_cols:
    shopify_body = (
        '<div class="empty">sales.reference_number_source (Shopify / Hubtiger / native channel) is not '
        'in this snapshot yet. Run a full <code>npm run sync</code> on the source machine — incremental '
        "syncs won't backfill historical rows — then <code>npm run snapshot</code> and push.</div>"
    )
else:
    shopify_summary = q1(f"""
        WITH {VALID_SALE_CTE}
        SELECT
          COALESCE(SUM(CASE WHEN UPPER(TRIM(s.reference_number_source)) = 'SHOPIFY' THEN l.revenue ELSE 0 END), 0) AS shopify_revenue,
          COALESCE(SUM(CASE WHEN UPPER(TRIM(s.reference_number_source)) = 'SHOPIFY' THEN l.cogs ELSE 0 END), 0) AS shopify_cogs,
          COALESCE(SUM(CASE WHEN UPPER(TRIM(s.reference_number_source)) = 'SHOPIFY' THEN l.quantity ELSE 0 END), 0) AS shopify_units,
          COUNT(DISTINCT CASE WHEN UPPER(TRIM(s.reference_number_source)) = 'SHOPIFY' THEN l.sale_id END) AS shopify_transactions,
          COALESCE(SUM(l.revenue), 0) AS total_revenue
        FROM lines l JOIN sales s ON s.sale_id = l.sale_id
    """, params)
    shopify_gp = shopify_summary["shopify_revenue"] - shopify_summary["shopify_cogs"]
    shopify_share = (shopify_summary["shopify_revenue"] / shopify_summary["total_revenue"] * 100
                      if shopify_summary["total_revenue"] else 0)
    shopify_stats = stats_html([
        {"label": "Shopify revenue", "value": zar(shopify_summary["shopify_revenue"])},
        {"label": "Gross profit", "value": zar(shopify_gp)},
        {"label": "Margin", "value": pct(margin_of(shopify_gp, shopify_summary["shopify_revenue"]))},
        {"label": "Share of total revenue", "value": pct(shopify_share),
         "foot": f"{shopify_summary['shopify_transactions']:,.0f} transactions, {shopify_summary['shopify_units']:,.0f} units"},
    ])
    shopify_by_cat = q(f"""
        WITH {VALID_SALE_CTE}
        SELECT COALESCE(c.top_level_name, 'Uncategorised') AS category,
               SUM(l.revenue) AS revenue, SUM(l.revenue) - SUM(l.cogs) AS gross_profit, SUM(l.quantity) AS units
        FROM lines l
        JOIN sales s ON s.sale_id = l.sale_id
        LEFT JOIN items i ON i.item_id = l.item_id
        LEFT JOIN categories c ON c.category_id = i.category_id
        WHERE UPPER(TRIM(s.reference_number_source)) = 'SHOPIFY'
        GROUP BY category
        HAVING SUM(l.revenue) <> 0
        ORDER BY revenue DESC
    """, params)
    shopify_rows = [[r["category"], f'{r["units"]:,.0f}', money(r["revenue"]), money(r["gross_profit"]),
                      pct(margin_of(r["gross_profit"], r["revenue"]))]
                     for _, r in shopify_by_cat.head(8).iterrows()]
    shopify_table = html_table(["Category", "Units", "Revenue", "GP", "Margin"], shopify_rows, name_col=0,
                                empty_text="No Shopify-sourced sales in this period.")

    other_channels = q(f"""
        WITH {VALID_SALE_CTE}
        SELECT COALESCE(NULLIF(TRIM(s.reference_number_source), ''), 'Lightspeed POS') AS channel,
               SUM(l.revenue) AS revenue
        FROM lines l JOIN sales s ON s.sale_id = l.sale_id
        WHERE UPPER(TRIM(COALESCE(s.reference_number_source, ''))) <> 'SHOPIFY'
        GROUP BY channel
        HAVING SUM(l.revenue) <> 0
        ORDER BY revenue DESC
    """, params)
    other_bits = "; ".join(f"{esc(r['channel'])} {zar(r['revenue'])}" for _, r in other_channels.iterrows())
    other_note = f'<div class="note" style="margin-top:6px">Other channels for context: {other_bits}.</div>' if other_bits else ""
    shopify_body = shopify_stats + shopify_table + other_note

col3, col4 = st.columns(2, gap="small")
col3.markdown(frame("Top 10 products", "By revenue", top_body), unsafe_allow_html=True)
col4.markdown(frame("Shopify sales breakdown", "Online channel", shopify_body), unsafe_allow_html=True)

# ---------------------------------------------------------------- stock (never period-filtered)

anchor_row = q1("""
    SELECT substr(MAX(COALESCE(complete_time, sale_time)), 1, 10) AS d
    FROM sales WHERE completed = 1 AND voided = 0
""")
anchor = anchor_row["d"] if anchor_row else None

if anchor:
    anchor_date = dt.date.fromisoformat(anchor)
    trail_from = (anchor_date - dt.timedelta(days=364)).isoformat()
    trail_params = {"trail_from": trail_from, "anchor": anchor}

    TRAILING = """
    trailing AS (
        SELECT sl.item_id,
               SUM(sl.quantity) AS net_units,
               SUM(CASE WHEN sl.quantity > 0 THEN sl.quantity ELSE 0 END) AS gross_units,
               SUM(sl.quantity * CASE WHEN sl.fifo_cost > 0 THEN sl.fifo_cost ELSE sl.avg_cost END) AS cogs
        FROM sale_lines sl
        JOIN sales s ON s.sale_id = sl.sale_id
        WHERE s.completed = 1 AND s.voided = 0
          AND substr(COALESCE(s.complete_time, s.sale_time), 1, 10) BETWEEN :trail_from AND :anchor
        GROUP BY sl.item_id
    )
    """

    stock_summary = q1("""
        SELECT (SELECT COALESCE(SUM(value_avg_cost),0) FROM item_shops WHERE qoh > 0) AS stock_value,
               (SELECT COALESCE(SUM(qoh),0) FROM item_shops WHERE qoh > 0) AS stock_units,
               (SELECT COUNT(*) FROM item_shops WHERE qoh > 0) AS stocked_skus
    """)
    trailing_cogs = q1("""
        SELECT COALESCE(SUM(sl.quantity * CASE WHEN sl.fifo_cost>0 THEN sl.fifo_cost ELSE sl.avg_cost END),0) AS cogs
        FROM sale_lines sl JOIN sales s ON s.sale_id = sl.sale_id
        WHERE s.completed=1 AND s.voided=0
          AND substr(COALESCE(s.complete_time, s.sale_time),1,10) BETWEEN :lo AND :hi
    """, {"lo": trail_from, "hi": anchor})["cogs"]
    turn = trailing_cogs / stock_summary["stock_value"] if stock_summary["stock_value"] else None

    dead_total = q1(f"""
        WITH {TRAILING}
        SELECT COALESCE(SUM(s.value_avg_cost), 0) AS value, COUNT(*) AS skus
        FROM item_shops s
        LEFT JOIN items i ON i.item_id = s.item_id
        LEFT JOIN trailing t ON t.item_id = s.item_id
        WHERE s.qoh > 0 AND s.value_avg_cost > 0
          AND COALESCE(t.gross_units, 0) <= 0
          AND (i.created_at IS NULL OR julianday(:anchor) - julianday(substr(i.created_at, 1, 10)) > 90)
    """, trail_params)

    stock_by_cat = q(f"""
        WITH {TRAILING},
        cat_cogs AS (
            SELECT COALESCE(c.top_level_name, 'Uncategorised') AS category,
                   SUM(t.cogs) AS trailing_cogs
            FROM trailing t
            LEFT JOIN items i ON i.item_id = t.item_id
            LEFT JOIN categories c ON c.category_id = i.category_id
            GROUP BY category
        ),
        cat_stock AS (
            SELECT COALESCE(c.top_level_name, 'Uncategorised') AS category,
                   SUM(s.qoh) AS qoh,
                   SUM(s.value_avg_cost) AS stock_value
            FROM item_shops s
            LEFT JOIN items i ON i.item_id = s.item_id
            LEFT JOIN categories c ON c.category_id = i.category_id
            WHERE s.qoh > 0
            GROUP BY category
        )
        SELECT st.category, st.qoh, st.stock_value,
               COALESCE(cc.trailing_cogs, 0) AS trailing_cogs
        FROM cat_stock st
        LEFT JOIN cat_cogs cc ON cc.category = st.category
        WHERE st.stock_value > 0
        ORDER BY st.stock_value DESC
    """, trail_params)

    bike_cover = q(f"""
        WITH {TRAILING}
        SELECT im.model || CASE WHEN im.size IS NOT NULL THEN ' ' || im.size ELSE '' END AS label,
               SUM(s.qoh) AS qoh,
               SUM(s.value_avg_cost) AS stock_value,
               COALESCE(SUM(t.gross_units), 0) AS trailing_gross_units
        FROM item_shops s
        JOIN item_models im ON im.item_id = s.item_id
        LEFT JOIN trailing t ON t.item_id = s.item_id
        WHERE s.qoh > 0 AND im.kind = 'complete'
        GROUP BY label
        HAVING SUM(s.value_avg_cost) > 0
        ORDER BY stock_value DESC
        LIMIT 30
    """, trail_params)

    stock_stats = stats_html([
        {"label": "Stock on hand (at cost)", "value": zar(stock_summary["stock_value"]),
         "foot": f'{stock_summary["stock_units"]:,.0f} units, {stock_summary["stocked_skus"]:,} SKUs'},
        {"label": "Stock turn (proxy)", "value": f"{turn:.2f}×" if turn else "—",
         "foot": f"ZAR {compact(trailing_cogs)} COGS / 12m"},
        {"label": "Weeks of cover", "value": f"{52 / turn:.1f}" if turn else "—", "foot": "At the trailing sales rate"},
        {"label": "Aged stock (no sales in 12m)", "value": zar(dead_total["value"]), "color": "var(--clay)",
         "foot": f'{dead_total["skus"]:,} SKUs of capital tied up'},
    ])

    stock_cat_rows = []
    for _, r in stock_by_cat.head(7).iterrows():
        t = r["trailing_cogs"] / r["stock_value"] if r["stock_value"] else None
        stock_cat_rows.append([
            r["category"], f'{r["qoh"]:,.0f}', money(r["stock_value"]), money(r["trailing_cogs"]),
            f"{t:.1f}×" if t else "—", f"{52 / t:.1f}" if t else "—",
        ])
    stock_cat_table = html_table(["Category", "Units held", "At cost", "12m COGS", "Turn", "Weeks"],
                                  stock_cat_rows, name_col=0)

    cover_rows = []
    for _, r in bike_cover.head(8).iterrows():
        sold = r["trailing_gross_units"] or 0
        weeks = (r["qoh"] * 52 / sold) if sold > 0 else None
        weeks_cell = "no sales in 12m" if weeks is None else f"{weeks:.1f}"
        if weeks is None or weeks > 40:
            weeks_cell = Raw(f'<span style="color:var(--clay)">{esc(weeks_cell)}</span>')
        cover_rows.append([r["label"], f'{r["qoh"]:,.0f}', money(r["stock_value"]), f"{sold:,.0f}", weeks_cell])
    cover_table = html_table(["Model / size", "Units held", "At cost", "12m sold", "Weeks cover"],
                              cover_rows, name_col=0)

    stock_body = (
        stock_stats
        + '<div class="two">'
        + f'<div><div class="meta" style="margin-bottom:3px">By category</div>{stock_cat_table}</div>'
        + f'<div><div class="meta" style="margin-bottom:3px">Bike cover by model and size</div>{cover_table}</div>'
        + '</div>'
    )
    st.markdown(
        frame("Stock on hand & stock turn",
              f"Held now · trade over the 12 months to {anchor} · not period-filtered",
              stock_body),
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------- footer notes

note_bits = ["Revenue is ex-VAT and net of discounts, counting only completed, non-voided sales — the same basis as the "
             "Lightspeed sales report."]
if adj_rev:
    note_bits.append(f"Includes {zar(adj_rev)} from Adjustments.xlsx (sales made outside Lightspeed); "
                      f"Lightspeed alone accounts for {zar(kpis['lightspeed_revenue'])}.")
if kpis["zero_cost_revenue"]:
    note_bits.append(
        f"{zar(kpis['zero_cost_revenue'])} of revenue ({pct(kpis['zero_cost_revenue'] / kpis['revenue'] * 100 if kpis['revenue'] else 0)}) "
        "has no unit cost on record — labour and service lines legitimately carry no COGS, which is why service "
        "categories show margins near 100%. Judge product margin on the goods categories."
    )

st.markdown(
    f'''<footer class="notes">
      <div class="note">{" ".join(note_bits)}</div>
      <div class="note">Stock turn is a proxy (COGS &divide; closing stock at cost) — Lightspeed exposes only a
        current snapshot, so the denominator is the closing position rather than a period average. Negative-stock
        SKUs are excluded from the values above and need correcting in Lightspeed.</div>
    </footer>''',
    unsafe_allow_html=True,
)
