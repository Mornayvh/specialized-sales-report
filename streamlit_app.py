"""
Sales Dashboard — Streamlit read-only mirror of the Node app.

Reads snapshot.sqlite ONLY. That file is a deliberately-sanitised export (see
export-snapshot.js) — tokens, employee names, and sync operational state are
excluded before it is ever committed. This app never talks to Lightspeed,
never holds a credential, and has no access to the machine that produces it.

The snapshot is refreshed manually: run `npm run refresh` on the source
machine, then commit and push snapshot.sqlite. The "Data as of" line in the
sidebar states exactly how fresh the figures are — this is a periodic
snapshot, not a live feed.

Revenue basis mirrors server.js exactly (same formulas, ported 1:1):
  - ex-VAT, net of discounts; only completed, non-voided sales
  - line revenue = calc_subtotal - calc_line_discount - calc_transaction_discount
  - COGS = quantity * (fifo_cost if > 0 else avg_cost)
  - period basis = COALESCE(complete_time, sale_time)
Off-Lightspeed Adjustments are included in headline figures and always
disclosed separately, since the Lightspeed-only figure is what ties to the
shop's own report.

Scope note: this first version covers KPIs, trend, category breakdown, bike
model/trim/size drill-down, budget vs actual, top products, discount leakage,
and stock. Accessory attach-rate is NOT yet ported — flagged explicitly in the
footer rather than silently omitted.
"""
import hmac
import sqlite3
import datetime as dt
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).parent / "snapshot.sqlite"

st.set_page_config(page_title="Sales Dashboard", layout="wide")


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

    st.title("Sales Dashboard")
    st.text_input("Access code", type="password", key="password_input", on_change=on_submit)
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

# st.metric truncates long ZAR figures with an ellipsis at narrower column
# widths (e.g. "ZAR 82,409,..."). The value is intact in the DOM either way —
# confirmed via automated testing — but the ellipsis is misleading for a
# financial figure, so it is disabled here.
st.markdown(
    "<style>[data-testid='stMetricValue'] { overflow: visible; white-space: normal; font-size: 1.65rem; }</style>",
    unsafe_allow_html=True,
)

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
    return f"{round(v or 0):,.0f}"


def pct(v):
    return "—" if v is None or pd.isna(v) else f"{v:.1f}%"


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


NEGATIVE_STYLE = "color:#8c1f34"  # matches the design system's negative/warning token


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

st.sidebar.title("Filters")
today = dt.date.today()

if "preset" not in st.session_state:
    st.session_state.preset = "All time"

preset = st.sidebar.radio("Period preset", list(PRESETS.keys()),
                           index=list(PRESETS.keys()).index(st.session_state.preset))
st.session_state.preset = preset
default_from, default_to = PRESETS[preset](today)

st.sidebar.markdown("**Custom range**")
c1, c2 = st.sidebar.columns(2)
from_date = c1.date_input("From", value=default_from, format="YYYY-MM-DD") if default_from else c1.date_input("From", value=None, format="YYYY-MM-DD")
to_date = c2.date_input("To", value=default_to, format="YYYY-MM-DD") if default_to else c2.date_input("To", value=None, format="YYYY-MM-DD")

params = period_params(from_date, to_date)
showing = f"{from_date} to {to_date}" if (from_date or to_date) else "All time"
st.sidebar.markdown(f"**Showing:** {showing}")

meta = q1("SELECT value FROM snapshot_meta WHERE key = 'exported_at'")
if meta:
    st.sidebar.caption(f"Data as of {meta['value'][:16].replace('T', ' ')} UTC — refreshed manually, not live.")
st.sidebar.caption("Stock is never period-filtered — it is a point-in-time position (see Stock section).")

# ---------------------------------------------------------------- header

st.title("Sales Dashboard")
st.caption(f"Specialized Paarl · Lightspeed Retail · {showing} · ZAR, ex-VAT and net of discounts")

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


def delta_pct(curr, prior):
    if prior is None or prior == 0:
        return None
    return (curr - prior) / abs(prior) * 100


def sply_caption(curr, prior, fmt, suffix="%", is_point=False):
    if sply is None:
        return None
    if prior is None:
        return "No data in the same period last year"
    d = (curr - prior) if is_point else delta_pct(curr, prior)
    sign = "+" if (d or 0) >= 0 else "−"
    dtxt = f" ({sign}{abs(d):.1f}{suffix})" if d is not None else ""
    return f"{fmt(prior)} same period last year{dtxt}"


c1, c2, c3, c4 = st.columns(4)
c1.metric("Revenue (ex-VAT, net of discount)", f"ZAR {money(kpis['revenue'])}",
          help=f"{kpis['transactions']:,} transactions")
if sply:
    c1.caption(sply_caption(kpis["revenue"], sply["revenue"], lambda v: f"ZAR {money(v)}"))
else:
    c1.caption(f"{kpis['transactions']:,} transactions")

c2.metric("Gross profit", f"ZAR {money(gp)}")
c2.caption(sply_caption(gp, sply["gp"] if sply else None, lambda v: f"ZAR {money(v)}") or "Revenue less cost of goods sold")

c3.metric("Gross margin", pct(margin))
c3.caption(sply_caption(margin, sply["margin"] if sply else None, pct, suffix="pp", is_point=True) or f"ZAR {compact(kpis['cogs'])} COGS")

c4.metric("Average sale value", f"ZAR {money(avg_sale)}")
c4.caption(sply_caption(avg_sale, sply["avg"] if sply else None, lambda v: f"ZAR {money(v)}") or f"{kpis['units']:,.0f} units sold")

# ---------------------------------------------------------------- budget vs actual

st.subheader("Budget vs actual")
budget_all = q("SELECT * FROM budget")
if budget_all.empty:
    st.info("No budget data in the snapshot.")
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
        comparison.append({"Category": cat, "Budget": b, "Actual": a, "Variance": a - b,
                            "Variance %": ((a - b) / b * 100) if b else None})
    comp_df = pd.DataFrame(comparison).sort_values("Budget", ascending=False)
    unbudgeted = sum(v for k, v in actual_map.items() if k not in categories)

    tb, ta = comp_df["Budget"].sum(), comp_df["Actual"].sum()
    tv = ta - tb
    st.caption(f"Compared over {eff_from} to {eff_to}. Only the {len(categories)} budgeted categories are compared. "
               f"A further ZAR {money(unbudgeted)} of revenue sits in unbudgeted categories — context, not variance.")
    b1, b2, b3 = st.columns(3)
    b1.metric("Budget for the period", f"ZAR {money(tb)}")
    b2.metric("Actual", f"ZAR {money(ta)}", help="Includes off-Lightspeed adjustments")
    b3.metric("Variance", f"{'+' if tv >= 0 else '−'}ZAR {money(abs(tv))}",
              f"{tv / tb * 100:+.1f}% vs budget" if tb else None)

    show = comp_df.copy()
    for c in ["Budget", "Actual", "Variance"]:
        show[c] = show[c].map(money)
    show["Variance %"] = comp_df["Variance %"].map(pct)
    st.dataframe(show, hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- category & model (shared helper)

def render_breakdown(title, rows_df, label_col, label_header, total_label="Total", help_text=None):
    st.subheader(title)
    if help_text:
        st.caption(help_text)
    if rows_df.empty:
        st.caption("No sales in this period.")
        return
    max_rev = rows_df["revenue"].abs().max() or 1
    show = rows_df.copy()
    show["Revenue (ZAR)"] = show["revenue"].map(money)
    show["Margin"] = (show["gross_profit"] / show["revenue"] * 100).map(pct)
    st.dataframe(
        show[[label_col, "units", "Revenue (ZAR)", "Margin"]].rename(columns={label_col: label_header, "units": "Units"}),
        hide_index=True, use_container_width=True,
    )
    tot_rev = rows_df["revenue"].sum()
    tot_gp = rows_df["gross_profit"].sum()
    st.caption(f"**{total_label}: ZAR {money(tot_rev)} · {pct(margin_of(tot_gp, tot_rev))} margin** "
               "— should tie to the KPI card above (a built-in sanity check).")


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
# Fold in adjustments by category (same matching as the budget section).
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

render_breakdown("Sales by category", by_category, "category", "Category",
                  help_text="Top-level Lightspeed category · revenue in ZAR, ex-VAT net of discounts")

# ---- bike model / trim / size drill-down ----
st.subheader("Bike sales by model")
mk1, mk2, mk3 = st.columns([1, 1, 2])
kind_filter = mk1.radio("Form", ["Complete bikes", "All forms"], horizontal=True, label_visibility="collapsed")
kind_clause = "" if kind_filter == "All forms" else "AND im.kind = 'complete'"

if "drill_model" not in st.session_state:
    st.session_state.drill_model = None
    st.session_state.drill_trim = None

level = "model"
group_expr = "im.model"
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

crumbs = ["All models"]
if st.session_state.drill_model:
    crumbs.append(st.session_state.drill_model)
if st.session_state.drill_trim:
    crumbs.append(st.session_state.drill_trim)
cc = st.columns(len(crumbs) + 1)
for i, label in enumerate(crumbs):
    if cc[i].button(label, key=f"crumb{i}", disabled=(i == len(crumbs) - 1)):
        if i == 0:
            st.session_state.drill_model = None
            st.session_state.drill_trim = None
        elif i == 1:
            st.session_state.drill_trim = None
        st.rerun()

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
    st.caption(f"Complete bikes only: ZAR {money(coverage['complete_rev'])}. A further ZAR {money(coverage['other_rev'])} "
               "of bike-category revenue is framesets, bare frames, build kits and rentals — switch to “All forms” to include it.")

if not model_df.empty and level != "size":
    col_key = "label"
    for _, r in model_df.head(30).iterrows():
        row_cols = st.columns([2, 5, 1, 1])
        label_btn = row_cols[0].button(r["label"] + " ›", key=f"drill_{level}_{r['label']}")
        if label_btn:
            if level == "model":
                st.session_state.drill_model = r["label"]
            else:
                st.session_state.drill_trim = r["label"]
            st.rerun()
        row_cols[1].progress(min(1.0, abs(r["revenue"]) / (model_df["revenue"].abs().max() or 1)))
        row_cols[2].write(money(r["revenue"]))
        row_cols[3].write(pct(margin_of(r["gross_profit"], r["revenue"])))
    tot_rev, tot_gp = model_df["revenue"].sum(), model_df["gross_profit"].sum()
    st.caption(f"**Total: ZAR {money(tot_rev)} · {pct(margin_of(tot_gp, tot_rev))} margin**")
else:
    render_breakdown("Size mix", model_df, "label", "Size")

# ---------------------------------------------------------------- top products

st.subheader("Top 10 products")
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
if not top_products.empty:
    tp = top_products.copy()
    tp["Revenue (ZAR)"] = tp["revenue"].map(money)
    tp["Gross profit (ZAR)"] = tp["gross_profit"].map(money)
    tp["Margin"] = (tp["gross_profit"] / tp["revenue"] * 100).map(pct)
    st.dataframe(tp[["product", "category", "units", "Revenue (ZAR)", "Gross profit (ZAR)", "Margin"]]
                 .rename(columns={"product": "Product", "category": "Category", "units": "Units"}),
                 hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- discount leakage

st.subheader("Discount leakage")
disc_summary = q1(f"""
    WITH {VALID_SALE_CTE}
    SELECT COALESCE(SUM(sl.calc_line_discount + sl.calc_transaction_discount), 0) AS total_discount,
           COALESCE(SUM(l.revenue), 0) AS net_revenue,
           SUM(CASE WHEN sl.calc_line_discount + sl.calc_transaction_discount > 0 THEN 1 ELSE 0 END) AS discounted_lines,
           COUNT(*) AS total_lines
    FROM lines l JOIN sale_lines sl ON sl.sale_line_id = l.sale_line_id
""", params)
gross = disc_summary["net_revenue"] + disc_summary["total_discount"]
d1, d2, d3 = st.columns(3)
d1.metric("Discount given", f"ZAR {money(disc_summary['total_discount'])}")
d2.metric("Share of gross", pct(disc_summary["total_discount"] / gross * 100 if gross else 0))
d3.metric("Lines discounted",
          pct(disc_summary["discounted_lines"] / disc_summary["total_lines"] * 100 if disc_summary["total_lines"] else 0),
          f"{disc_summary['discounted_lines']:,} of {disc_summary['total_lines']:,}")

disc_by_cat = q(f"""
    WITH {VALID_SALE_CTE}
    SELECT COALESCE(c.top_level_name, 'Uncategorised') AS category,
           SUM(sl.calc_line_discount + sl.calc_transaction_discount) AS discount,
           SUM(l.revenue) AS net_revenue
    FROM lines l JOIN sale_lines sl ON sl.sale_line_id = l.sale_line_id
    LEFT JOIN items i ON i.item_id = l.item_id
    LEFT JOIN categories c ON c.category_id = i.category_id
    GROUP BY category
    HAVING SUM(sl.calc_line_discount + sl.calc_transaction_discount) > 0
    ORDER BY discount DESC
""", params)
if not disc_by_cat.empty:
    dd = disc_by_cat.copy()
    dd["Discount (ZAR)"] = dd["discount"].map(money)
    dd["Share of gross"] = ((dd["discount"] / (dd["net_revenue"] + dd["discount"])) * 100).map(pct)
    dd["Net revenue (ZAR)"] = dd["net_revenue"].map(money)
    st.dataframe(dd[["category", "Discount (ZAR)", "Share of gross", "Net revenue (ZAR)"]]
                 .rename(columns={"category": "Category"}), hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- stock (never period-filtered)

st.subheader("Stock on hand & stock turn")
st.caption("Held now · not affected by the period filter above — stock is a point-in-time position. "
           "Turn is COGS ÷ closing stock (a proxy for COGS ÷ average stock, since only a current snapshot is available).")

anchor_row = q1("""
    SELECT substr(MAX(COALESCE(complete_time, sale_time)), 1, 10) AS d
    FROM sales WHERE completed = 1 AND voided = 0
""")
anchor = anchor_row["d"] if anchor_row else None
if anchor:
    anchor_date = dt.date.fromisoformat(anchor)
    trail_from = (anchor_date - dt.timedelta(days=364)).isoformat()
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

    s1, s2, s3 = st.columns(3)
    s1.metric("Stock on hand (at cost)", f"ZAR {money(stock_summary['stock_value'])}",
              f"{stock_summary['stock_units']:,.0f} units, {stock_summary['stocked_skus']:,} SKUs")
    s2.metric("Stock turn (proxy)", f"{turn:.2f}×" if turn else "—",
              f"ZAR {compact(trailing_cogs)} COGS / 12m")
    s3.metric("Weeks of cover", f"{52/turn:.1f}" if turn else "—")

st.divider()
st.caption(
    "Revenue is ex-VAT and net of discounts, counting only completed, non-voided sales — the same basis as the "
    "Lightspeed sales report. Includes off-Lightspeed sales from Adjustments.xlsx where matched to a catalogue "
    "category. This is a periodic snapshot refreshed manually from the shop's Node/Lightspeed sync — not a live feed. "
    "Not yet ported to this view: accessory attach-rate (see the Node app for that breakdown)."
)
