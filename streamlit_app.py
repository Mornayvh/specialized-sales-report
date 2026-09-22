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

The report is two A4 pages and only two: the month in review, then the
financial year to date. Both carry the same five things — three performance
figures against the same period last year, a budget card, a category
overview, the Shopify channel, and the top ten sellers — over a different
window, so they are built by one function and differ only in the dates handed
to it. Attach rate and stock position are standing operational analyses
rather than a period result, so they sit on appendix sheets that are off the
printed document unless the control bar asks for them.

Scope: every figure on both pages covers the budgeted categories plus
Wheelsets, and nothing else. That is what lets the budget column mean
something — revenue including categories nobody budgeted for cannot be
compared with a budget that excludes them. Wheelsets is reported alongside
the budgeted categories with an empty budget rather than dropped; whatever
falls outside the scope is quantified in each page footer rather than lost.

Category, Shopify and top-ten rows open in place: a category into its
subcategories and then into the items and sizes beneath them, the top ten by
range into model and size. The drill-downs are <details> elements, so they
cost no rerun and no server round trip — and the print stylesheet shuts every
one of them, which is what keeps the printed report exactly two pages
however far it has been explored on screen.

Visual design ported from index.html (the Node app's dashboard front end):
same type system (Barlow / Barlow Condensed), same colour discipline (steel
navy is the only series colour; clay is reserved EXCLUSIVELY for unfavourable
values and is never decorative), same panel and table conventions. The one
addition is a favourable green, used only where a figure is read as a
pass/fail against budget. Native Streamlit chrome that cannot be fully
re-skinned (the period selects, the checkbox) is restyled as closely as
Streamlit's own component internals allow.
"""
import base64
import html as html_lib
import re
import hmac
import sqlite3
import datetime as dt
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).parent / "snapshot.sqlite"
LOGO_PATH = Path(__file__).parent / "logo.png"

st.set_page_config(page_title="Sales Dashboard", layout="wide")


def esc(s):
    return html_lib.escape(str(s), quote=True)


class Raw(str):
    """A table cell value that is already-safe HTML — skip escaping."""


@st.cache_data
def logo_data_uri():
    """The band's Specialized mark, inlined as a base64 data URI.

    Inlined rather than served from disk because Streamlit only serves static
    files when enableStaticServing is turned on AND the file sits under
    ./static — and because a data URI is already decoded by the time
    window.print() fires, where a still-loading <img> would print blank.

    logo.png carries a .png extension but its bytes are actually AVIF (an
    ISO-BMFF 'ftyp' box at offset 4), so the MIME type is sniffed rather than
    taken from the filename: declaring image/png for AVIF bytes makes the
    browser drop the image silently.
    """
    if not LOGO_PATH.exists():
        return ""
    raw = LOGO_PATH.read_bytes()
    mime = "image/avif" if raw[4:8] == b"ftyp" else "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def logo_img():
    """The header band's mark, or nothing at all if logo.png is missing."""
    uri = logo_data_uri()
    return f'<img class="bandlogo" src="{uri}" alt="Specialized">' if uri else ""


# ---------------------------------------------------------------- styling
#
# Ported from index.html's :root palette and component classes 1:1 (same
# variable names, same values) so the two front ends stay in visual lockstep.
STYLE = """
<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600&family=Barlow+Condensed:wght@500;600;700&display=swap" rel="stylesheet">
<style>
/* =====================================================================
   Industry design system — tokens lifted verbatim from the design handoff
   (_ds/industry-f4244d10-4d03-48f0-9b01-e3b721af52d8/styles.css).
   Square corners and no shadows anywhere: no border-radius is used in this
   design at all, and the only shadow on the page is the screen-only one
   separating sheets from the ground behind them.
   ===================================================================== */
:root {
  --color-bg:          #f2f2f3;
  --color-text:        #1d1f20;
  --color-divider:     color-mix(in srgb, #1d1f20 16%, transparent);
  --color-neutral-200: #e7e7ea;
  --color-neutral-300: #d4d4d7;
  --color-neutral-400: #b7b7ba;
  --color-neutral-500: #98989b;
  --color-neutral-600: #7a7a7d;
  --color-neutral-700: #5d5d60;
  --color-accent-300:  #b5d9fd;
  --color-accent-400:  #94bce3;
  --color-accent-600:  #597ea3;
  --color-accent-700:  #416180;
  --color-accent-800:  #2c455d;
  --color-accent-900:  #1d2d3d;
  /* Clay. The design hard-codes this rather than tokenising it, because it
     carries meaning rather than style: unfavourable values ONLY (negative
     variance, discount leakage, aged stock). Never decorative. */
  --clay:              #8c1f34;
  /* Its favourable twin. The original design had no "good" colour because the
     only signed figure on the sheet was a variance, drawn in accent navy when
     it was not clay. The budget card and the two variance columns are read as
     a pass/fail at a glance, so they get a green rather than a navy — the one
     place in the report where hue, not just weight, carries the verdict. */
  --good:              #1f6b45;
  --font-heading:      "Barlow Condensed", system-ui, sans-serif;
  --font-body:         "Barlow", system-ui, sans-serif;
}

/* ---------- Streamlit scaffold, flattened to a plain document ground ----------
   The app is no longer a Streamlit page with a content column; it is a stack
   of fixed A4 sheets centred on a ground. Everything Streamlit puts around
   that is neutralised here. */
html, body, [class*="css"] { font-family: var(--font-body); }
.stApp, [data-testid="stAppViewContainer"] { background: #d9d9dc; }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stSidebar"] { display: none !important; }
[data-testid="stMainBlockContainer"] {
  max-width: none; width: 100%; padding: 16px 0 40px; background: transparent;
}
h1, h2, h3 { font-family: var(--font-heading); }
/* Streamlit pads its headings generously for a scrolling app. Inside a fixed
   page box that padding is dead space the design never budgeted for — it made
   the page-1 band 36px taller than specified on its own. */
.sheet h1, .sheet h2, .sheet h3, .sheet h4 { padding: 0; font-weight: 600; }
.sheet p { margin: 0; }

/* =====================================================================
   The document — a stack of fixed A4 portrait sheets.
   794 x 1123px is 210 x 297mm at 96dpi, the same physical box doc-page.js
   pins in the prototype, expressed in the units the screen lays out in.
   overflow:hidden is load-bearing rather than defensive: a section that
   outgrows its sheet must be visibly clipped during review, not silently
   reflowed onto a sheet that does not exist in print.
   ===================================================================== */
.sheet {
  width: 794px; height: 1123px; overflow: hidden;
  margin: 0 auto 20px;
  background: var(--color-bg); color: var(--color-text);
  font-family: var(--font-body);
  /* Streamlit's global line-height is 1.6. The design is measured against the
     browser default, so inheriting 1.6 inflates every table row and bar row by
     roughly a third and pushes each sheet past its own page box. */
  line-height: normal;
  display: flex; flex-direction: column;
  /* Screen-only separation from the ground. Removed in print, where the
     sheet edge IS the paper edge. */
  box-shadow: 0 1px 10px rgba(29,31,32,0.18);
}
/* Page 1 is full-bleed at the top (the band runs edge to edge), so the sheet
   itself carries no top or side padding — the inner wrapper does. */
.sheet.s1 { padding: 0 0 22px; gap: 16px; }
.sheet.s1 .inner { padding: 0 40px; display: flex; flex-direction: column; gap: 14px; }
.sheet.sn { padding: 30px 40px 20px; gap: 22px; }
/* Page 2 is the same compact sheet as page 1 without the full-bleed band, so
   it wears .s1's densities and only replaces the band with its own header. */
.sheet.s1.s2 { padding-top: 24px; }
/* The inner wrapper has to take the sheet's spare height for the methodology
   footer's margin-top:auto to have anything to push against. */
.sheet.s1 .inner { flex: 1; }

/* ---------- header band (page 1, full bleed) ---------- */
.band {
  background: var(--color-accent-900); color: #fff;
  padding: 20px 40px 22px;
  display: flex; align-items: flex-end; justify-content: space-between; gap: 24px;
}
.band h1 {
  font-family: var(--font-heading); font-size: 38px; line-height: 1;
  letter-spacing: -0.02em; margin: 6px 0 5px; color: #fff !important; font-weight: 600;
}
/* Streamlit wraps markdown headings in an auto-generated inner <span> for its
   anchor-link feature; that span is a type-selector match for any bare
   `span` rule, which would otherwise beat the inherited white. */
.band h1 span { color: #fff !important; }
.band .eyebrow { font-size: 10px; letter-spacing: 0.22em; text-transform: uppercase; color: var(--color-accent-300); }
.band .period  { font-size: 11.5px; color: var(--color-accent-300); }
.bandright { display: flex; align-items: center; gap: 22px; flex: none; }
.bandright .txt { text-align: right; }
.bandright .synced {
  font-family: var(--font-heading); font-size: 15px; margin-top: 4px;
  font-variant-numeric: tabular-nums; color: #fff;
}
.bandright .counts { font-size: 11px; color: var(--color-accent-300); font-variant-numeric: tabular-nums; }
/* The mark is white on transparency, so it sits straight on the band. */
.bandlogo { width: 66px; height: 66px; display: block; flex: none; }

/* ---------- KPI strip (page 1) ---------- */
.kpis {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 0;
  border-top: 1px solid var(--color-neutral-500);
  border-bottom: 1px solid var(--color-neutral-500);
  padding: 12px 0 13px;
}
.kpi { padding: 0 16px; }
.kpi + .kpi { border-left: 1px solid var(--color-divider); }
.kpi .k { font-size: 10px; letter-spacing: 0.13em; text-transform: uppercase; color: var(--color-accent-700); }
.kpi .v {
  font-family: var(--font-heading); font-size: 27px; line-height: 1.05;
  letter-spacing: -0.02em; font-variant-numeric: tabular-nums; margin-top: 4px;
}
.kpi .f { font-size: 11px; color: var(--color-neutral-700); margin-top: 3px; line-height: 1.25; }
.kpi .f + .f { margin-top: 1px; }

/* ---------- section header ---------- */
.sh {
  display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
  border-bottom: 1px solid var(--color-neutral-500);
}
.sh h2, .sh h3 {
  font-family: var(--font-heading); font-size: 17px; margin: 0;
  letter-spacing: 0.01em; font-weight: 600;
}
.sh .eyebrow { font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--color-neutral-600); }
/* Per-section rhythm: the design tunes these individually rather than using
   one value, so the variants are named for the section they belong to. */
.sh.tight  { padding-bottom: 3px; margin-bottom: 5px; }
.sh.normal { padding-bottom: 4px; margin-bottom: 8px; }
.sh.loose  { padding-bottom: 5px; margin-bottom: 10px; }
.sh.sub    { padding-bottom: 5px; margin-bottom: 6px; }

/* ---------- page header (pages 2+) ----------
   A 2px rule, deliberately heavier than the 1px section rules. */
.ph2 {
  display: flex; align-items: baseline; justify-content: space-between;
  border-bottom: 2px solid var(--color-accent-900); padding-bottom: 7px;
}
.ph2 h2 { font-family: var(--font-heading); font-size: 26px; margin: 0; letter-spacing: -0.01em; font-weight: 600; }
.ph2 .eyebrow { font-size: 10px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--color-neutral-600); }

/* ---------- bar rows ----------
   Two densities: page 1 is the compact grid, pages 2+ the roomy one. Bar
   width is value / max(value), proportional to the largest row rather than
   to the total, so the leader always reaches the full track. */
.brow { display: grid; align-items: center; }
.sheet.s1 .brow { grid-template-columns: 130px 1fr 96px 58px; gap: 12px; padding: 3px 0; }
.sheet.sn .brow { grid-template-columns: 150px 1fr 108px 64px; gap: 14px; padding: 6px 0; }
.sheet.s1 .brow .nm, .sheet.s1 .brow .rv, .sheet.s1 .brow .mg { font-size: 12.5px; }
.sheet.sn .brow .nm, .sheet.sn .brow .rv, .sheet.sn .brow .mg { font-size: 14px; }
.brow .nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.brow .rv, .brow .mg { text-align: right; font-variant-numeric: tabular-nums; }
.brow .mg { color: var(--color-neutral-700); }
.track { background: var(--color-neutral-200); }
.sheet.s1 .track, .sheet.s1 .fill { height: 12px; }
.sheet.sn .track, .sheet.sn .fill { height: 14px; }

.brow.total { border-top: 1px solid var(--color-neutral-500); font-weight: 600; }
.sheet.s1 .brow.total { padding: 5px 0 0; margin-top: 3px; }
.sheet.sn .brow.total { padding: 8px 0 0; margin-top: 4px; }
.brow.total .nm { font-family: var(--font-heading); letter-spacing: 0.07em; text-transform: uppercase; }
.sheet.s1 .brow.total .nm { font-size: 13px; }
.sheet.sn .brow.total .nm { font-size: 14px; }
.brow.total .mg { color: var(--color-text); }

/* ---------- tables ---------- */
table.dt { width: 100%; border-collapse: collapse; }
.sheet.s1 table.dt { font-size: 12.5px; }
.sheet.sn table.dt { font-size: 14px; }
/* Streamlit's base stylesheet puts a 1px border on all four sides of every
   table cell. The design rules rows with a bottom border only, so the side
   borders have to be cleared explicitly — left alone they draw column rules
   the design does not have AND add a pixel of height to every row, which is
   enough on its own to push page 1 past its page box. */
table.dt th, table.dt td { border: 0; }
table.dt th {
  font-family: var(--font-heading); font-weight: 600; font-size: 10.5px;
  letter-spacing: 0.1em; text-transform: uppercase; color: var(--color-neutral-600);
  padding: 4px 6px; border-bottom: 1px solid var(--color-neutral-500); text-align: right;
}
/* Alignment follows the column's ROLE, not its position: the Top 10 table
   leads with a right-aligned rank before the left-aligned product name, so a
   :first-child rule aligns the wrong column. Every builder tags its name and
   rank cells, header included. */
table.dt th.name, table.dt td.name { text-align: left; }
table.dt th.rank, table.dt td.rank { text-align: right; }
table.dt td {
  border-bottom: 1px solid var(--color-neutral-200);
  font-variant-numeric: tabular-nums; text-align: right;
}
.sheet.s1 table.dt td { padding: 3.5px 6px; }
.sheet.sn table.dt td { padding: 6px 6px; }
td.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-variant-numeric: normal; }
.sheet.sn td.name { max-width: 300px; }
td.rank { font-family: var(--font-heading); color: var(--color-neutral-500); width: 22px; }
td.muted { color: var(--color-neutral-700); }

/* ---------- budget summary row + diverging variance bar ---------- */
.bsum { display: flex; margin-bottom: 10px; }
.bsum > div:first-child { padding-right: 22px; }
.bsum > div + div { padding: 0 22px; border-left: 1px solid var(--color-divider); }
.bsum .k { font-size: 10px; letter-spacing: 0.13em; text-transform: uppercase; color: var(--color-accent-700); }
.bsum .v {
  font-family: var(--font-heading); font-size: 21px; line-height: 1.1;
  font-variant-numeric: tabular-nums; margin-top: 2px;
}
/* The under/over cell. Fill width saturates at +/-25% variance, so a category
   that is 80% over budget and one that is 30% over both read as "full" —
   the number beside it carries the exact value. */
td.vbar { width: 210px; padding: 5px 6px 5px 14px !important; }
.vwrap { display: grid; grid-template-columns: 1fr 1fr; align-items: center; height: 10px; }
.vneg { display: flex; justify-content: flex-end; border-right: 1px solid var(--color-neutral-400); height: 10px; }
.vpos { display: flex; height: 10px; }
.vneg > div, .vpos > div { height: 10px; }
.vneg > div { background: var(--clay); }
.vpos > div { background: var(--color-accent-600); }
th.vbarh { text-align: center !important; padding-left: 14px !important; }

/* ---------- stat cards (operating health) ---------- */
.cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
.card { border: 1px solid var(--color-divider); padding: 10px 13px 12px; }
.card .t {
  font-family: var(--font-heading); font-size: 14px; letter-spacing: 0.05em;
  text-transform: uppercase; color: var(--color-accent-800); margin-bottom: 7px;
}
.card .figrow { display: flex; align-items: baseline; gap: 8px; }
.card .fig { font-family: var(--font-heading); font-size: 30px; line-height: 1; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.card .cap { font-size: 11px; color: var(--color-neutral-700); }
.card .subs { margin-top: 9px; display: flex; flex-direction: column; gap: 4px; font-size: 11.5px; }
.card .subs > div { display: flex; justify-content: space-between; }
.card .subs span:first-child { color: var(--color-neutral-700); }
.card .subs span:last-child { font-variant-numeric: tabular-nums; }

/* ---------- metric cards (the four figures heading each review page) ----------
   Three performance figures plus the budget card, each carrying one
   sub-heading: the same measure a year earlier, or the variance against
   budget. One row, four equal columns, so the figures share a baseline. */
.mcards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 11px; }
/* The Shopify pair. Two cards, so they get a smaller figure than the four that
   head the page — a channel worth a fraction of the shop should not carry the
   same visual weight as the shop's own revenue. */
.mcards.two { grid-template-columns: repeat(2, 1fr); margin-bottom: 7px; }
.mcards.two .mcard .fig { font-size: 19px; }
.mcard { border: 1px solid var(--color-divider); padding: 7px 11px 9px; }
.mcard .t {
  font-family: var(--font-heading); font-size: 11.5px; letter-spacing: 0.09em;
  text-transform: uppercase; color: var(--color-accent-800);
}
.mcard .fig {
  font-family: var(--font-heading); font-size: 24px; line-height: 1.06;
  letter-spacing: -0.02em; font-variant-numeric: tabular-nums; margin-top: 4px;
}
.mcard .sub { font-size: 10.5px; color: var(--color-neutral-700); margin-top: 4px; line-height: 1.3; }
.mcard .sub .n { font-variant-numeric: tabular-nums; }

/* ---------- drill-down tables ----------
   A CSS grid rather than a <table>, because the rows nest: a category opens
   into its subcategories, a subcategory into its items. <details> cannot be a
   child of <tbody>, but it can be a child of a grid container, and every row
   at every depth shares one column template through the inherited --dtcols
   custom property, so the columns stay aligned down the whole tree.

   Collapsed is the printable state. The sheet is a fixed page box, so an
   opened branch would spill past the paper; on screen the sheet grows to fit
   (see .sheet:has(details[open]) below) and in print every branch is forced
   shut again, which is what keeps the report exactly two pages however far
   it has been explored. */
.dt2 { width: 100%; }
.dt2 .r { display: grid; grid-template-columns: var(--dtcols); align-items: center; }
.dt2 .r > div {
  padding: 2px 6px; text-align: right; font-variant-numeric: tabular-nums;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.dt2 .r > div.nm { text-align: left; font-variant-numeric: normal; }
.sheet.s1 .dt2 { font-size: 11.5px; }
.sheet.sn .dt2 { font-size: 12.5px; }
.dt2 .hd { border-bottom: 1px solid var(--color-neutral-500); }
.dt2 .hd > div {
  font-family: var(--font-heading); font-weight: 600; font-size: 9.5px;
  letter-spacing: 0.08em; text-transform: uppercase; color: var(--color-neutral-600);
  white-space: normal; line-height: 1.12; padding-bottom: 3px;
}
.dt2 .l1 { border-bottom: 1px solid var(--color-neutral-200); }
.dt2 .l2, .dt2 .l3 { border-bottom: 1px solid rgba(29,31,32,0.07); }
.dt2 .l2 > div, .dt2 .l3 > div { padding-top: 1.5px; padding-bottom: 1.5px; font-size: 11px; }
.dt2 .l2 > .nm { padding-left: 22px; }
.dt2 .l3 > .nm { padding-left: 42px; color: var(--color-neutral-700); }
.dt2 .kids { background: rgba(29,31,32,0.022); }
.dt2 .tot {
  border-top: 1px solid var(--color-neutral-500); border-bottom: 0; font-weight: 600;
}
.dt2 .tot > div { padding-top: 5px; }
.dt2 .tot > .nm {
  font-family: var(--font-heading); letter-spacing: 0.07em; text-transform: uppercase;
}
/* The disclosure control. summary defaults to display:list-item and draws its
   own marker; both have to go before it can be a grid row, and the caret is
   redrawn on the name cell so it indents with the level it belongs to. */
.dt2 summary { list-style: none; cursor: pointer; }
.dt2 summary::-webkit-details-marker { display: none; }
.dt2 summary.r > .nm::before {
  content: "▸"; display: inline-block; width: 9px; font-size: 9px;
  color: var(--color-neutral-500); margin-right: 4px;
}
.dt2 details[open] > summary.r > .nm::before { content: "▾"; }
.dt2 summary.r:hover { background: rgba(29,31,32,0.05); }
.dt2 .muted { color: var(--color-neutral-700); }

/* ---------- methodology footer ----------
   margin-top:auto pins it to the bottom of the sheet's flex column. */
footer.notes {
  margin-top: auto; border-top: 1px solid var(--color-divider); padding-top: 7px;
  display: grid; grid-template-columns: 1fr 1fr; gap: 18px;
  font-size: 9.5px; line-height: 1.45; color: var(--color-neutral-700);
}

/* An opened drill-down needs room the fixed page box does not have. Rather
   than clip it (the sheet's overflow:hidden is there so an oversized section
   is visibly caught during review) the sheet becomes auto-height for as long
   as anything inside it is open, and snaps back when it is closed again. */
.sheet:has(details[open]) { height: auto; min-height: 1123px; overflow: visible; }

.empty { color: var(--color-neutral-600); font-size: 12px; padding: 14px 0; text-align: center; }
.stack { display: flex; flex-direction: column; gap: 22px; }
a { color: var(--color-accent-700); }
a:hover { color: var(--color-accent-900); }

/* =====================================================================
   Screen chrome — the control bars above the document. Never printed.

   Both bars are st.container()s, because they hold real Streamlit widgets and
   a widget cannot live inside markup emitted by st.markdown. Each container is
   addressed through an invisible marker span it contains: the :has() selector
   below matches the ONE stVerticalBlock whose own direct element-container
   holds that marker, which is what keeps it from also matching every
   ancestor block up to the page root.
   ===================================================================== */
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .mk-ctrlbar) {
  max-width: 794px; margin: 0 auto 14px; background: #fff;
  border: 1px solid var(--color-divider); padding: 10px 14px;
  font-family: var(--font-body); gap: 8px !important;
}
.lbl { font-size: 10px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--color-neutral-600); margin-bottom: 4px; }
/* The coverage caveat under the appendix tables, at the design's footnote scale. */
.note { font-size: 10.5px; line-height: 1.5; color: var(--color-neutral-700); margin-top: 8px; }

/* Streamlit's own widgets, dragged into the design's visual language. The
   button is scoped by marker adjacency: an invisible <span> marker is emitted
   immediately before it, and the rule targets the element container that
   FOLLOWS the one holding the marker. Streamlit gives its widgets no stable
   class of their own, so this is the only way to style one widget without
   styling every widget on the page. The selects and the checkbox are reached
   through their own stable data-testid instead. */
div[data-testid="stElementContainer"]:has(.mk-print) + div[data-testid="stElementContainer"] button {
  font-family: var(--font-body) !important; font-size: 11px !important; letter-spacing: 0.1em;
  text-transform: uppercase; padding: 8px 12px !important; border-radius: 0 !important;
  min-height: 0 !important;
  background: var(--color-accent-800) !important; border: 1px solid var(--color-accent-800) !important;
}
div[data-testid="stElementContainer"]:has(.mk-print) + div[data-testid="stElementContainer"] button p {
  font-size: 11px !important; color: #fff !important;
}
div[data-testid="stElementContainer"]:has(.mk-print) + div[data-testid="stElementContainer"] button:hover {
  background: var(--color-accent-900) !important; border-color: var(--color-accent-900) !important;
}

div[data-testid="stTextInputRootElement"], div[data-baseweb="input"], div[data-baseweb="select"] > div,
div[data-testid="stSelectbox"] .react-aria-ComboBox [role="group"] {
  border-radius: 0 !important; background: #fff !important; box-shadow: none !important;
  border: 1px solid var(--color-neutral-400) !important;
}
div[data-testid="stSelectbox"] input {
  font-family: var(--font-body) !important; font-size: 13px !important;
  color: var(--color-accent-900) !important;
}
div[data-testid="stCheckbox"] [data-testid="stMarkdownContainer"] p {
  font-family: var(--font-body) !important; font-size: 12px !important;
  color: var(--color-accent-800) !important;
}
div[data-testid="stElementContainer"]:has(div[data-testid="stTextInput"]) {
  max-width: 794px; margin: 0 auto;
}

/* =====================================================================
   Print
   ===================================================================== */
@media print {
  /* The page box IS the design here — 210 x 297mm with no margin at all, so
     the sheet's own 40px padding is the only margin, exactly as on screen.
     This replaces what doc-page.js injects in the prototype. */
  @page { size: A4 portrait; margin: 0; }

  /* Browsers default to NOT printing background colours unless the user ticks
     "Background graphics" — an easy-to-miss, non-default setting. Without it
     the navy band prints blank white and its white text disappears. */
  * { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; color-adjust: exact !important; }

  /* Streamlit's print stylesheet leaves its scaffold absolutely positioned at
     a fixed viewport height, which makes the printed document's body shorter
     than its own content — Chrome then fragments the overflow rather than the
     document, washing a white layer over everything and clipping the top of
     the first sheet. Every element from <html> down to stMain has to become a
     normally-flowing, auto-height box for print fragmentation to behave. */
  html, body, #root, [data-testid="stScreencast"], .stApp,
  [data-testid="stAppViewContainer"], [data-testid="stAppViewContainer"] > div,
  section.stMain {
    position: static !important; inset: auto !important;
    height: auto !important; min-height: 0 !important; max-height: none !important;
    overflow: visible !important; background: #fff !important;
  }
  /* Pin the content column to the sheet width. Left wider, Chrome shrink-fits
     the whole document to the paper and the 1:1 pixel mapping is lost. */
  [data-testid="stMainBlockContainer"] {
    width: 794px !important; max-width: 794px !important;
    padding: 0 !important; margin: 0 !important; background: #fff !important;
  }
  div[data-testid="stVerticalBlock"] { gap: 0 !important; }

  [data-testid="stSidebar"], [data-testid="stHeader"], [data-testid="stToolbar"],
  [data-testid="stMainMenu"], .screen-only, .no-print { display: none !important; }
  /* The control bar is an st.container(), not markup we can hang a
     .screen-only class on, so it is hidden through the same marker-scoped
     selector that styles it. Left visible it occupies real height above the
     first sheet and pushes the document onto a third page. */
  div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .mk-ctrlbar),
  [data-testid="stIFrame"], iframe { display: none !important; }

  /* The printed report is the collapsed report, always. A branch left open on
     screen would push its sheet past the paper and fragment onto a third page,
     so the page box is re-asserted and every drill-down is shut for print —
     the two sheets print identically no matter how far they were explored. */
  .dt2 details > .kids { display: none !important; }
  .dt2 summary.r > .nm::before { display: none !important; }
  .sheet { height: 1123px !important; min-height: 0 !important; overflow: hidden !important; }
  /* The appendix is analysis that sits beside the report rather than in it. It
     is off the printed document unless the control bar asks for it. */
  .sheet.appendix { display: none !important; }

  /* One .sheet, one sheet of paper. */
  .sheet {
    margin: 0 !important; box-shadow: none !important;
    break-after: page; break-inside: avoid;
  }
  .sheet:last-of-type { break-after: auto; }
}
</style>
"""
def css_for_markdown(text):
    """Flatten the stylesheet into something st.markdown will pass through whole.

    st.markdown renders Markdown BEFORE the HTML reaches the page, and two
    separate Markdown rules each truncate a <style> block:

      - Asterisks. The `*` closing a CSS comment sits after whitespace and
        before `/`, so Markdown reads it as an emphasis opener and the `*`
        opening the next comment closes the span. Both are eaten, `*/` becomes
        `/`, and the browser treats everything after as one unterminated
        comment. Measured: 1666 characters survived out of 12KB.
      - Blank lines. A blank line closes a raw-HTML block, so the <style>
        element only ever receives the text above the first one. Measured after
        fixing the asterisks: 495 characters, ending mid-token-list.

    st.html() is not the way out — it sanitises the <style> element away
    entirely and renders an empty div.

    So: drop the comments, then drop the blank lines. Both exist for whoever
    reads this file, and neither changes what the browser computes. The comment
    regex is safe here because the sheet holds no string or url() literal that
    could contain a `/*` sequence.
    """
    without_comments = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(ln for ln in without_comments.split("\n") if ln.strip())


st.markdown(css_for_markdown(STYLE), unsafe_allow_html=True)


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

    # The gate wears the report's own page-1 band. It is wrapped in a .sheet so
    # it inherits the same type scale and heading resets as the real thing —
    # without that wrapper the band picks up Streamlit's heading padding and
    # sits 36px taller than the one behind it.
    st.markdown(
        f'''<div class="sheet s1" style="height:auto;padding-bottom:0;margin-bottom:14px">
          <div class="band">
            <div>
              <div class="eyebrow">Specialized Paarl · Lightspeed Retail</div>
              <h1>Sales Dashboard</h1>
            </div>
            {logo_img()}
          </div>
        </div>''',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="lbl" style="max-width:794px;margin:0 auto 4px">Access code</div>',
                unsafe_allow_html=True)
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


def snapshot_key():
    """A fingerprint of the snapshot file, carried as a cache key.

    Every query below is memoised, and the memo has to fall the moment the
    data underneath it changes. On Streamlit Cloud a push restarts the process
    and empties the cache anyway — but locally `npm run refresh` replaces
    snapshot.sqlite under a running server, and without this the app would go
    on serving the previous snapshot's figures indefinitely.

    NOTE the parameter this is passed as must NOT be named with a leading
    underscore: st.cache_data deliberately excludes underscore-prefixed
    arguments from the hash, which would exclude the very thing being keyed on.
    """
    info = DB_PATH.stat()
    return info.st_mtime_ns, info.st_size


# Streamlit re-runs this whole script on every interaction — every widget
# click, every period change — so an uncached query is paid for again each
# time even when nothing it reads has moved. The snapshot is immutable for the
# life of a process, which makes every query on it a pure function of (sql,
# params): exactly what st.cache_data is for.
@st.cache_data(show_spinner=False, max_entries=256)
def _query(sql, params, snapshot):
    return pd.read_sql_query(sql, get_conn(), params=params)


@st.cache_data(show_spinner=False, max_entries=256)
def _query_one(sql, params, snapshot):
    row = get_conn().execute(sql, params).fetchone()
    return dict(row) if row else None


def q(sql, params=None):
    """Run a query, return a DataFrame."""
    return _query(sql, params or {}, snapshot_key())


def q1(sql, params=None):
    """Run a query, return the single row as a dict (or None)."""
    return _query_one(sql, params or {}, snapshot_key())


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


# ---------------------------------------------------------------- HTML builders
#
# Every builder below emits the markup of the approved A4 design, class-for-
# class. The values themselves (grids, paddings, type sizes) live in STYLE
# above rather than inline, so a section that appears on more than one sheet
# picks up that sheet's density from `.sheet.s1` / `.sheet.sn` instead of
# carrying two hard-coded variants.

def section_header(title, eyebrow="", rhythm="normal", level=2):
    """Heading left, uppercase eyebrow right, 1px rule under both.

    `rhythm` selects the design's per-section padding/margin pair rather than
    flattening them to one value — the design tunes each section separately.
    """
    eb = f'<div class="eyebrow">{esc(eyebrow)}</div>' if eyebrow else ""
    return (f'<div class="sh {rhythm}"><h{level}>{esc(title)}</h{level}>{eb}</div>')


def page_header(title, eyebrow):
    """Pages 2+. Note the 2px rule — deliberately heavier than section rules."""
    return f'<div class="ph2"><h2>{esc(title)}</h2><div class="eyebrow">{esc(eyebrow)}</div></div>'


def html_table(headers, rows, name_col=None, rank_col=None, muted_cols=(),
               empty_text="Nothing in this period."):
    if not rows:
        return f'<div class="empty">{esc(empty_text)}</div>'
    def hcls(i):
        if i == name_col:
            return ' class="name"'
        if i == rank_col:
            return ' class="rank"'
        return ""

    thead = "".join(f"<th{hcls(i)}>{esc(h)}</th>" for i, h in enumerate(headers))
    body = []
    for r in rows:
        tds = []
        for i, c in enumerate(r):
            classes = []
            if i == name_col:
                classes.append("name")
            if i == rank_col:
                classes.append("rank")
            if i in muted_cols:
                classes.append("muted")
            cls_attr = f' class="{" ".join(classes)}"' if classes else ""
            title_attr = f' title="{esc(c)}"' if i == name_col else ""
            val = c if isinstance(c, Raw) else esc(c)
            tds.append(f"<td{cls_attr}{title_attr}>{val}</td>")
        body.append(f"<tr>{''.join(tds)}</tr>")
    return f'<table class="dt"><thead><tr>{thead}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def stats_html(items):
    """A row of small figures, used on the appendix sheets where a full card
    grid would be too heavy. Same type scale as a card's sub-rows."""
    parts = []
    for it in items:
        style = f' style="color:{it["colour"]}"' if it.get("colour") else ""
        foot = f'<div class="cap">{esc(it["foot"])}</div>' if it.get("foot") else ""
        parts.append(f'<div><div class="k" style="font-size:10px;letter-spacing:0.13em;'
                     f'text-transform:uppercase;color:var(--color-accent-700)">{esc(it["label"])}</div>'
                     f'<div class="v" style="font-family:var(--font-heading);font-size:21px;'
                     f'line-height:1.1;font-variant-numeric:tabular-nums;margin-top:2px"{style}>'
                     f'{esc(it["value"])}</div>{foot}</div>')
    return f'<div class="bsum">{"".join(parts)}</div>'


def metric_card(title, figure, subs, figure_colour=None):
    """One of the figures heading a review page.

    Title, the figure itself, and one sub-heading line. That line is the same
    measure a year earlier on the performance cards and the variance against
    budget on the budget card — one comparison per card, never two, so the
    cards stay the same height and their figures share a baseline. `subs` is a
    list of {"text", "colour"} fragments joined by a middle dot.
    """
    fig_style = f' style="color:{figure_colour}"' if figure_colour else ""
    parts = []
    for s in subs:
        style = f' style="color:{s["colour"]}"' if s.get("colour") else ""
        parts.append(f'<span class="n"{style}>{esc(s["text"])}</span>')
    return (
        f'<div class="mcard"><div class="t">{esc(title)}</div>'
        f'<div class="fig"{fig_style}>{esc(figure)}</div>'
        f'<div class="sub">{" · ".join(parts)}</div></div>'
    )


def cell(value, colour=None, muted=False):
    """One value cell of a drill-down row."""
    cls = ' class="muted"' if muted else ""
    style = f' style="color:{colour}"' if colour else ""
    return f"<div{cls}{style}>{esc(value)}</div>"


def _drill_rows(nodes, depth, cells_fn):
    """Render one level of the tree, recursing into any node that has children.

    A node with children becomes a <details>; its own figures live in the
    <summary>, so the row stays readable — and printable — while shut.
    """
    out = []
    for n in nodes:
        label = esc(n["label"])
        row = f'<div class="nm" title="{label}">{label}</div>' + cells_fn(n, depth)
        kids = n.get("children")
        if kids:
            out.append(f'<details><summary class="r l{depth}">{row}</summary>'
                       f'<div class="kids">{_drill_rows(kids, depth + 1, cells_fn)}</div></details>')
        else:
            out.append(f'<div class="r l{depth}">{row}</div>')
    return "".join(out)


def drill_table(columns, nodes, cells_fn, total=None, empty_text="Nothing in this period."):
    """A table whose rows open into their own detail.

    `columns` is [(label, css width), ...] with the name column first; every
    row at every depth is laid out against that one template, which is handed
    down the tree as a custom property rather than repeated per level.
    `cells_fn(node, depth)` renders one row's value cells — depth is passed so
    a column that only means something at the top level (a budget variance
    against a category, say) can go quiet further down rather than repeating a
    number that was never apportioned that far.
    """
    if not nodes:
        return f'<div class="empty">{esc(empty_text)}</div>'
    template = " ".join(w for _, w in columns)
    head = (f'<div class="nm">{esc(columns[0][0])}</div>'
            + "".join(f'<div>{esc(label)}</div>' for label, _ in columns[1:]))
    tot = ""
    if total:
        tot = (f'<div class="r tot"><div class="nm">{esc(total["label"])}</div>'
               + total["cells"] + "</div>")
    return (f'<div class="dt2" style="--dtcols:{template}">'
            f'<div class="r hd">{head}</div>{_drill_rows(nodes, 1, cells_fn)}{tot}</div>')


# ---------------------------------------------------------------- adjustments

def get_adjustments(from_date, to_date):
    rows = q("SELECT * FROM adjustments", {})
    if rows.empty:
        return rows
    if from_date:
        rows = rows[rows["date"] >= from_date.isoformat()]
    if to_date:
        rows = rows[rows["date"] <= to_date.isoformat()]
    return rows


# ---------------------------------------------------------------- product naming
#
# The top-ten table groups what was sold into Range / Model / Size rather than
# listing raw descriptions, so that "EPIC 8 COMP M" and "EPIC 8 COMP L" read as
# two sizes of one model of one range instead of two unrelated products.
#
# Bikes already carry that structure: classify-models.js resolved every bike
# item to a model, trim and size in item_models, and those three fields ARE the
# three levels. Nothing else in the snapshot does — an accessory is a free-text
# description and a category path — so for everything else the size is read off
# the end of the description with the same rules models.js uses for bikes,
# widened to the sizes accessories actually come in (shoe, waist, sock), and
# the range falls back to the item's own category leaf. That is why a shoe
# groups under RECON but a pair of shorts groups under SHORT: the leaf is as
# specific as the category tree gets for that product.
ALPHA_SIZES = {"XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL",
               "XS/S", "S/M", "M/L", "L/XL", "XL/XXL"}
SIZE_ALIASES = {"SM": "S", "MD": "M", "LG": "L", "LNG": "L"}
# Diameters, not frame sizes. A description ending "29" names the wheel; read
# as a size it would invent one that does not exist.
WHEEL_TOKENS = {"26", "27.5", "29", "650B", "700C", "700"}
SIZE_NUMERIC = re.compile(r"^\d{1,2}(\.\d+)?$")
SIZE_PAIRED = re.compile(r"^\d{1,2}(\.\d+)?\s*[/-]\s*\d{1,2}(\.\d+)?$")


def _size_token(tok):
    """Is this trailing token a size? Returns it normalised, or None."""
    t = SIZE_ALIASES.get(tok, tok)
    if t in WHEEL_TOKENS:
        return None
    if re.fullmatch(r"S[1-6]", t) or t in ALPHA_SIZES or SIZE_PAIRED.fullmatch(t):
        return t
    # A bare number is a size only within the range sizes are actually
    # expressed in — 38-64cm frames, 36-50 shoes, 26-44 waists, kids' 10-24.
    # Without the ceiling "GARMIN FORERUNNER 965" acquires a size of 965.
    if SIZE_NUMERIC.fullmatch(t) and 1 <= float(t) <= 70:
        return t
    return None


def split_size(description):
    """Split a description into (description without its size, size or None).

    The size is the last token, or the second-to-last where a stray marker
    follows it — the same two-token window models.py reads bike sizes from.
    """
    toks = (description or "").split()
    for idx in (len(toks) - 1, len(toks) - 2):
        if idx < 0:
            continue
        size = _size_token(toks[idx].upper().strip(",.;"))
        if size:
            return " ".join(toks[:idx] + toks[idx + 1:]).strip() or description, size
    return description, None


def text_or_none(value):
    """A column value as text, or None where there isn't one.

    pandas returns a missing TEXT column as float NaN, and NaN is truthy: a
    bare `if row["bike_model"]` is True for every accessory in the shop, which
    is how every non-bike item once ended up grouped under a range named "nan".
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def product_levels(row):
    """(range, model, size) for one sold item."""
    model = text_or_none(row.get("bike_model"))
    if model:
        trim = text_or_none(row.get("bike_trim"))
        return (model, f"{model} {trim}" if trim else model,
                text_or_none(row.get("bike_size")))
    stripped, size = split_size(str(row.get("description") or ""))
    return (text_or_none(row.get("leaf")) or text_or_none(row.get("category")) or "—",
            stripped, size)


# ---------------------------------------------------------------- report scope
#
# The report is deliberately narrower than the shop's turnover: every figure on
# both review pages covers the budgeted categories and Wheelsets, and nothing
# else. That is what makes the budget column mean something — a revenue total
# that includes categories nobody budgeted for cannot be compared with a budget
# that does not. Wheelsets is the one addition: it is a stock-carrying retail
# category the budget never got a line for, so it is reported alongside the
# budgeted seven with an empty budget rather than dropped.
#
# Everything outside that scope (Paarl Trails, Retül, warranty, retail display,
# vouchers) is quantified in the footer of each page rather than silently lost.
EXTRA_CATEGORIES = ["WHEELSET"]

budgeted_categories = sorted(q("SELECT DISTINCT category FROM budget")["category"].tolist())
report_categories = budgeted_categories + [c for c in EXTRA_CATEGORIES
                                           if c not in budgeted_categories]
CAT_PARAMS = {f"cat{i}": c for i, c in enumerate(report_categories)}
CAT_IN = "(" + ", ".join(f":cat{i}" for i in range(len(report_categories))) + ")"

# Channel. Older snapshots predate the column, so the report degrades to an
# explanatory empty state rather than failing to load.
HAS_SOURCE = bool(q1("SELECT COUNT(*) n FROM pragma_table_info('sales') "
                     "WHERE name='reference_number_source'")["n"])
SHOPIFY_TEST = ("UPPER(TRIM(COALESCE(s.reference_number_source, ''))) = 'SHOPIFY'"
                if HAS_SOURCE else "0")

ITEM_SQL = f"""
WITH {VALID_SALE_CTE}
SELECT c.top_level_name AS category,
       CASE WHEN COALESCE(c.discipline_name, '') = '' THEN '(unclassified)'
            ELSE c.discipline_name END AS subcategory,
       COALESCE(NULLIF(c.name, ''), c.top_level_name) AS leaf,
       COALESCE(i.description, '(unknown item)') AS description,
       im.model AS bike_model, im.trim AS bike_trim, im.size AS bike_size,
       SUM(l.quantity) AS units,
       SUM(l.revenue) AS revenue,
       SUM(l.cogs) AS cogs,
       SUM(CASE WHEN {SHOPIFY_TEST} THEN l.quantity ELSE 0 END) AS sh_units,
       SUM(CASE WHEN {SHOPIFY_TEST} THEN l.revenue ELSE 0 END) AS sh_revenue,
       SUM(CASE WHEN {SHOPIFY_TEST} THEN l.cogs ELSE 0 END) AS sh_cogs
FROM lines l
JOIN sales s ON s.sale_id = l.sale_id
LEFT JOIN items i ON i.item_id = l.item_id
LEFT JOIN categories c ON c.category_id = i.category_id
LEFT JOIN item_models im ON im.item_id = l.item_id
WHERE c.top_level_name IN {CAT_IN}
GROUP BY l.item_id
HAVING SUM(l.revenue) <> 0 OR SUM(l.quantity) <> 0
"""

SCOPE_SQL = f"""
WITH {VALID_SALE_CTE}
SELECT COALESCE(SUM(l.revenue), 0) AS all_revenue,
       COALESCE(SUM(CASE WHEN c.top_level_name IN {CAT_IN} THEN l.revenue ELSE 0 END), 0) AS in_scope,
       COALESCE(SUM(CASE WHEN c.top_level_name IN {CAT_IN}
                          AND (l.no_cost_on_record) THEN l.revenue ELSE 0 END), 0) AS zero_cost
FROM lines l
LEFT JOIN items i ON i.item_id = l.item_id
LEFT JOIN categories c ON c.category_id = i.category_id
"""

# An adjustment names an item by description, so it inherits that item's
# category, subcategory and — where it is a bike — its model, trim and size.
# Anything whose description matches no item, or whose item falls outside the
# report's scope, is left out and disclosed in the page footer.
ADJ_LOOKUP = q("""
    SELECT UPPER(TRIM(i.description)) AS key,
           i.description AS description,
           c.top_level_name AS category,
           CASE WHEN COALESCE(c.discipline_name, '') = '' THEN '(unclassified)'
                ELSE c.discipline_name END AS subcategory,
           COALESCE(NULLIF(c.name, ''), c.top_level_name) AS leaf,
           im.model AS bike_model, im.trim AS bike_trim, im.size AS bike_size
    FROM items i
    LEFT JOIN categories c ON c.category_id = i.category_id
    LEFT JOIN item_models im ON im.item_id = i.item_id
    WHERE UPPER(TRIM(i.description)) IN (SELECT UPPER(TRIM(description)) FROM adjustments)
""").drop_duplicates("key").set_index("key")


# ---------------------------------------------------------------- periods
#
# The report is two reviews of the same shop over two windows: the month, and
# the financial year so far. Each page carries its own period, each is compared
# with the same window a year earlier, and both are picked in the control bar
# above the document — nothing that selects a period is ever printed.
#
# The financial year runs 1 July to 30 June — confirmed by the shop on
# 15 September 2026. The budget table agrees (2023-07 to 2027-06, four complete
# July-to-June years).
FY_START_MONTH = 7


def shift_year(d, delta):
    """Same calendar day, `delta` years away. 29 Feb clamps back to the 28th."""
    if d is None:
        return None
    try:
        return d.replace(year=d.year + delta)
    except ValueError:
        return d.replace(month=2, day=28, year=d.year + delta)


def month_end(year, month):
    return (dt.date(year, month + 1, 1) - dt.timedelta(days=1)) if month < 12 \
        else dt.date(year, 12, 31)


def fy_of(d):
    """The financial year `d` falls in, named for the June it ends in."""
    return d.year + 1 if d.month >= FY_START_MONTH else d.year


def fmt_day(d):
    """`1 Jul 2026` — the design's date format."""
    return f"{d.day} {d:%b %Y}"


bounds = q1("""
    SELECT substr(MIN(COALESCE(complete_time, sale_time)), 1, 10) lo,
           substr(MAX(COALESCE(complete_time, sale_time)), 1, 10) hi
    FROM sales WHERE completed = 1 AND voided = 0
""")
data_lo = dt.date.fromisoformat(bounds["lo"]) if bounds and bounds["lo"] else dt.date(2000, 1, 1)
data_hi = dt.date.fromisoformat(bounds["hi"]) if bounds and bounds["hi"] else dt.date.today()

# Only months and financial years the snapshot actually holds sales for are
# offered. A picker that lists periods with no data invites a reader to select
# an empty report and conclude the shop sold nothing.
month_keys = q("""
    SELECT DISTINCT substr(COALESCE(complete_time, sale_time), 1, 7) AS m
    FROM sales WHERE completed = 1 AND voided = 0 ORDER BY m DESC
""")["m"].tolist()
fy_keys = sorted({fy_of(dt.date.fromisoformat(m + "-01")) for m in month_keys}, reverse=True)


def month_window(key):
    """('2026-09') -> (1 Sep 2026, 14 Sep 2026) — clamped to the last day of data."""
    y, m = (int(x) for x in key.split("-"))
    return dt.date(y, m, 1), min(month_end(y, m), data_hi)


def fy_window(fy):
    """FY2027 -> (1 Jul 2026, 14 Sep 2026) — to date, clamped to the data."""
    start = dt.date(fy - 1, FY_START_MONTH, 1)
    return start, min(dt.date(fy, FY_START_MONTH, 1) - dt.timedelta(days=1), data_hi)


def month_label(key):
    return f"{dt.date.fromisoformat(key + '-01'):%B %Y}"


def fy_label(fy):
    return f"FY{fy} · {fy - 1}/{str(fy)[2:]}"


# ---------------------------------------------------------------- control bar
#
# The two review pages are pure output, so every control that drives them sits
# in this bar above the document and is excluded from print.

ctrl = st.container()
with ctrl:
    st.markdown('<span class="mk-ctrlbar" style="display:none"></span>', unsafe_allow_html=True)
    head = st.columns([3, 3, 3, 2.4], gap="small", vertical_alignment="bottom")

    with head[0]:
        st.markdown('<div class="lbl">Month in review</div>', unsafe_allow_html=True)
        month_key = st.selectbox("Month", month_keys, index=0, format_func=month_label,
                                 label_visibility="collapsed", key="month_pick")
    with head[1]:
        st.markdown('<div class="lbl">Financial year in review</div>', unsafe_allow_html=True)
        fy_key = st.selectbox("Financial year", fy_keys, index=0, format_func=fy_label,
                              label_visibility="collapsed", key="fy_pick")
    with head[2]:
        print_appendix = st.checkbox("Print the appendix sheets too", value=False,
                                     key="print_appendix")
    with head[3]:
        # A raw `onclick="window.print()"` worked locally but did nothing on
        # Streamlit Community Cloud: inline event-handler attributes are the
        # first thing a strict Content-Security-Policy blocks, and Cloud applies
        # one the local dev server doesn't. A real st.button uses Streamlit's own
        # click plumbing, and st.components.v1.html renders a proper iframe
        # document where a <script> tag actually executes — unlike
        # unsafe_allow_html, which never runs embedded scripts regardless of CSP.
        st.markdown('<span class="mk-print" style="display:none"></span>', unsafe_allow_html=True)
        print_clicked = st.button("Print to PDF", key="print_pdf", use_container_width=True)

month_from, month_to = month_window(month_key)
fy_from, fy_to = fy_window(fy_key)

if print_appendix:
    # The stylesheet hides .sheet.appendix from print; this puts it back. It has
    # to be an override rather than a flag on <body>, which Streamlit gives no
    # way to set, and it is a single line because a blank line inside a style
    # block delivered through st.markdown truncates it (see css_for_markdown).
    st.markdown("<style>@media print{.sheet.appendix{display:flex !important}}</style>",
                unsafe_allow_html=True)

if print_clicked:
    st.components.v1.html("<script>window.parent.print();</script>", height=0, width=0)

meta_row = q1("SELECT value FROM snapshot_meta WHERE key = 'exported_at'")
synced_text = "Sync status unavailable"
if meta_row:
    stamp = dt.datetime.fromisoformat(meta_row["value"][:19])
    synced_text = f"Synced {fmt_day(stamp.date())}, {stamp:%H:%M}"


# ---------------------------------------------------------------- budget
#
# Budget is monthly, the review windows are not, so a month that the window
# only partly covers contributes only that fraction of its budget. Prorating by
# day is the assumption: it treats trade as flat within a month, which it is
# not — but the alternative is comparing a half-month of sales with a whole
# month of budget, which is wrong by a much larger margin.

budget_all = q("SELECT * FROM budget")
if budget_all.empty:
    budget_lo = budget_hi = None
else:
    b_months = sorted(budget_all["month"].unique())
    budget_lo = dt.date.fromisoformat(b_months[0] + "-01")
    by, bm = (int(x) for x in b_months[-1].split("-"))
    budget_hi = month_end(by, bm)


def budget_for(from_date, to_date):
    """Prorated budget per category over [from_date, to_date], by day."""
    out = {c: 0.0 for c in budgeted_categories}
    if budget_all.empty:
        return out
    for _, r in budget_all.iterrows():
        y, m = (int(x) for x in r["month"].split("-"))
        m_start, m_end = dt.date(y, m, 1), month_end(y, m)
        lo, hi = max(m_start, from_date), min(m_end, to_date)
        if hi < lo:
            continue
        share = ((hi - lo).days + 1) / ((m_end - m_start).days + 1)
        out[r["category"]] = out.get(r["category"], 0.0) + r["amount"] * min(1.0, share)
    return out


# ---------------------------------------------------------------- tree building

def item_label(r):
    """One sold item, named as the reader would name it: the product, then the
    size it was sold in where the item has one."""
    _, name, size = product_levels(r)
    return f"{name} · {size}" if size else name


def build_tree(records, levels, keys, caps=None, depth=0):
    """Group `records` by `levels[0]`, recursing for each deeper level.

    `keys` names the (units, revenue, cost) fields to total — the same builder
    serves the whole-shop tables and the Shopify ones, which differ only in
    which three columns they add up. `caps` limits the row count at a given
    depth and rolls the remainder into one Other row, so a subcategory with
    four hundred lines cannot quietly become four hundred rows of HTML.
    """
    if not levels:
        return []
    unit_k, rev_k, cost_k = keys
    buckets = {}
    for r in records:
        buckets.setdefault(levels[0](r), []).append(r)

    nodes = []
    for label, rs in buckets.items():
        rev = sum(r[rev_k] or 0 for r in rs)
        nodes.append({
            "label": label,
            "units": sum(r[unit_k] or 0 for r in rs),
            "revenue": rev,
            "gp": rev - sum(r[cost_k] or 0 for r in rs),
            "children": build_tree(rs, levels[1:], keys, caps, depth + 1),
        })
    nodes.sort(key=lambda n: n["revenue"], reverse=True)

    cap = (caps or {}).get(depth)
    if cap and len(nodes) > cap:
        head_nodes, rest = nodes[:cap], nodes[cap:]
        nodes = head_nodes + [{
            "label": f"Other ({len(rest)})",
            "units": sum(n["units"] for n in rest),
            "revenue": sum(n["revenue"] for n in rest),
            "gp": sum(n["gp"] for n in rest),
            "children": [],
        }]
    return nodes


def tree_totals(nodes):
    return (sum(n["units"] for n in nodes),
            sum(n["revenue"] for n in nodes),
            sum(n["gp"] for n in nodes))


# ---------------------------------------------------------------- one review
#
# Both pages are the same report over a different window, so they are built by
# the same function and differ only in the period handed to it.

CAT_COLUMNS = [("Category", "1fr"), ("Revenue", "84px"), ("Gross profit", "84px"),
               ("Gross margin", "58px"), ("Budget vs actual", "88px"),
               ("Budget vs actual %", "74px")]
SHOP_COLUMNS = [("Category", "1fr"), ("Quantity", "62px"), ("Revenue", "92px"),
                ("Gross profit", "92px"), ("Gross margin", "68px")]
TOP_COLUMNS = [("Range · model · size", "1fr"), ("Units", "58px"), ("Revenue", "92px"),
               ("Gross profit", "92px"), ("Gross margin", "68px")]


def signed_money(v):
    return f"{'+' if v >= 0 else '−'}{money(abs(v))}"


def verdict_colour(v):
    """Green at or above budget, red below. The only place in the report where
    hue rather than weight carries the judgement."""
    return "var(--good)" if v >= 0 else "var(--clay)"


def period_records(from_date, to_date):
    """Every item sold in the window, inside the report's category scope.

    Returns the rows plus the two disclosure figures the page footer needs:
    revenue that fell outside the scope, and revenue carrying no unit cost.
    """
    params = {**period_params(from_date, to_date), **CAT_PARAMS}
    records = q(ITEM_SQL, params).to_dict("records")
    scope = q1(SCOPE_SQL, params)

    adj = get_adjustments(from_date, to_date)
    adj_in, adj_out = 0.0, 0.0
    for _, a in adj.iterrows():
        key = str(a["description"]).upper().strip()
        if key not in ADJ_LOOKUP.index:
            adj_out += a["revenue"]
            continue
        m = ADJ_LOOKUP.loc[key]
        if m["category"] not in report_categories:
            adj_out += a["revenue"]
            continue
        adj_in += a["revenue"]
        records.append({
            "category": m["category"], "subcategory": m["subcategory"], "leaf": m["leaf"],
            "description": m["description"], "bike_model": m["bike_model"],
            "bike_trim": m["bike_trim"], "bike_size": m["bike_size"],
            "units": a["qty"], "revenue": a["revenue"], "cogs": a["cost"],
            "sh_units": 0.0, "sh_revenue": 0.0, "sh_cogs": 0.0,
        })

    return records, {
        "out_of_scope": scope["all_revenue"] - scope["in_scope"],
        "zero_cost": scope["zero_cost"],
        "adjustments_in": adj_in,
        "adjustments_out": adj_out,
    }


def build_review(from_date, to_date):
    """Everything one review page shows, over one window."""
    records, notes = period_records(from_date, to_date)
    sply_from, sply_to = shift_year(from_date, -1), shift_year(to_date, -1)
    sply_records, _ = period_records(sply_from, sply_to)

    def totals(rows, rev_k="revenue", cost_k="cogs", unit_k="units"):
        rev = sum(r[rev_k] or 0 for r in rows)
        return (sum(r[unit_k] or 0 for r in rows), rev, rev - sum(r[cost_k] or 0 for r in rows))

    _, revenue, gross_profit = totals(records)
    _, sp_revenue, sp_gross_profit = totals(sply_records)
    gm, sp_gm = margin_of(gross_profit, revenue), margin_of(sp_gross_profit, sp_revenue)

    # Budget covers the budgeted categories only, so the actual it is measured
    # against has to as well: including Wheelsets in the numerator of a
    # comparison whose denominator has no Wheelsets line would show a surplus
    # the shop never budgeted to earn.
    budget = budget_for(from_date, to_date)
    budget_total = sum(budget.values())
    budgeted_actual = sum(r["revenue"] or 0 for r in records
                          if r["category"] in budgeted_categories)
    variance = budgeted_actual - budget_total
    variance_pct = (variance / budget_total * 100) if budget_total else None

    def delta_sub(curr, prior, is_points=False):
        """The SPLY line under a performance card: last year's figure, then the
        movement. A period with no prior-year trade gets the honest blank."""
        value = pct(prior) if is_points else money(prior)
        if prior is None or pd.isna(prior) or (not is_points and not prior):
            return [{"text": f"SPLY {value}"}]
        d = (curr - prior) if is_points else (curr - prior) / abs(prior) * 100
        sign = "+" if d >= 0 else "−"
        unit = "pp" if is_points else "%"
        return [{"text": f"SPLY {value}"},
                {"text": f"{sign}{abs(d):.1f}{unit}", "colour": verdict_colour(d)}]

    cards = (
        '<div class="mcards">'
        + metric_card("Revenue", money(revenue), delta_sub(revenue, sp_revenue))
        + metric_card("Gross profit", money(gross_profit), delta_sub(gross_profit, sp_gross_profit))
        + metric_card("Gross profit margin", pct(gm),
                      delta_sub(gm, sp_gm, is_points=True))
        + metric_card("Actual vs budget", money(budgeted_actual),
                      [{"text": f"Budget {money(budget_total)}"},
                       {"text": "—" if variance_pct is None
                        else f"{'+' if variance >= 0 else '−'}{abs(variance_pct):.1f}%",
                        "colour": verdict_colour(variance)}],
                      figure_colour=verdict_colour(variance))
        + "</div>"
    )

    # ---- category overview: category -> subcategory -> item and its size
    cat_nodes = build_tree(records, [lambda r: r["category"],
                                     lambda r: r["subcategory"],
                                     item_label],
                           ("units", "revenue", "cogs"), caps={2: 20})
    for n in cat_nodes:
        n["budget"] = budget.get(n["label"])

    def cat_cells(n, depth):
        cells = [cell(money(n["revenue"])), cell(money(n["gp"])),
                 cell(pct(margin_of(n["gp"], n["revenue"])), muted=True)]
        b = n.get("budget") if depth == 1 else None
        if b is None:
            # Below the top level, and for Wheelsets at it, there is no budget
            # to vary from. An apportioned share of one would be invented.
            cells += [cell("—", muted=True), cell("—", muted=True)]
        else:
            var = n["revenue"] - b
            vpct = (var / b * 100) if b else None
            colour = verdict_colour(var)
            cells.append(cell(signed_money(var), colour=colour))
            cells.append(cell("—" if vpct is None
                              else f"{'+' if var >= 0 else '−'}{abs(vpct):.1f}%", colour=colour))
        return "".join(cells)

    _, cat_rev, cat_gp = tree_totals(cat_nodes)
    cat_total = {"label": "Total", "cells": "".join([
        cell(money(cat_rev)), cell(money(cat_gp)), cell(pct(margin_of(cat_gp, cat_rev))),
        cell(signed_money(variance), colour=verdict_colour(variance)),
        cell("—" if variance_pct is None
             else f"{'+' if variance >= 0 else '−'}{abs(variance_pct):.1f}%",
             colour=verdict_colour(variance)),
    ])}
    category_body = drill_table(CAT_COLUMNS, cat_nodes, cat_cells, total=cat_total,
                                empty_text="No sales in this period.")

    # ---- Shopify
    def value_cells(n, depth):
        return "".join([cell(f"{n['units']:,.0f}"), cell(money(n["revenue"])),
                        cell(money(n["gp"])),
                        cell(pct(margin_of(n["gp"], n["revenue"])), muted=True)])

    shop_records = [r for r in records if (r["sh_revenue"] or 0) or (r["sh_units"] or 0)]
    shop_nodes = build_tree(shop_records, [lambda r: r["category"],
                                           lambda r: r["subcategory"],
                                           item_label],
                            ("sh_units", "sh_revenue", "sh_cogs"), caps={0: 6, 2: 20})
    shop_units, shop_rev, shop_gp = tree_totals(shop_nodes)
    shop_gm = margin_of(shop_gp, shop_rev)
    sp_shop_rev = sum(r["sh_revenue"] or 0 for r in sply_records)
    sp_shop_gp = sp_shop_rev - sum(r["sh_cogs"] or 0 for r in sply_records)
    sp_shop_gm = margin_of(sp_shop_gp, sp_shop_rev)

    shop_total = {"label": "Total", "cells": "".join([
        cell(f"{shop_units:,.0f}"), cell(money(shop_rev)), cell(money(shop_gp)),
        cell(pct(shop_gm)),
    ])}
    if not HAS_SOURCE:
        shopify_body = ('<div class="empty">Channel data is not in this snapshot — '
                        'reference_number_source is missing from the sales table.</div>')
    else:
        shopify_body = (
            '<div class="mcards two">'
            + metric_card("Shopify revenue", money(shop_rev),
                          [{"text": f"SPLY {money(sp_shop_rev)}"}])
            + metric_card("Shopify gross margin", pct(shop_gm),
                          [{"text": f"GM SPLY {pct(sp_shop_gm)}"}])
            + "</div>"
            + drill_table(SHOP_COLUMNS, shop_nodes, value_cells, total=shop_total,
                          empty_text="No Shopify-sourced sales in this period.")
        )

    # ---- top ten sellers, grouped range -> model -> size
    def range_of(r):
        return product_levels(r)[0]

    def model_of(r):
        return product_levels(r)[1]

    def size_of(r):
        return product_levels(r)[2] or "(no size)"

    top_nodes = build_tree(records, [range_of, model_of, size_of],
                           ("units", "revenue", "cogs"))[:10]
    top_units, top_rev, top_gp = tree_totals(top_nodes)
    top_total = {"label": "Top ten total", "cells": "".join([
        cell(f"{top_units:,.0f}"), cell(money(top_rev)), cell(money(top_gp)),
        cell(pct(margin_of(top_gp, top_rev))),
    ])}
    top_body = drill_table(TOP_COLUMNS, top_nodes, value_cells, total=top_total,
                           empty_text="No sales in this period.")

    return {
        "from": from_date, "to": to_date,
        "sply_from": sply_from, "sply_to": sply_to,
        "revenue": revenue, "gross_profit": gross_profit, "margin": gm,
        "budget_total": budget_total, "budgeted_actual": budgeted_actual,
        "variance": variance, "variance_pct": variance_pct,
        "cards": cards, "category_body": category_body,
        "shopify_body": shopify_body, "top_body": top_body,
        "notes": notes, "records": records,
    }


def review_footer(rv):
    """The two-column methodology strip at the foot of a review page."""
    n = rv["notes"]
    left = ("Revenue is ex-VAT and net of discounts, counting only completed, non-voided "
            "sales — the same basis as the Lightspeed sales report. Every figure on this "
            f"page covers the {len(budgeted_categories)} budgeted categories plus Wheelsets.")
    if n["out_of_scope"]:
        left += (f" A further ZAR {money(n['out_of_scope'])} traded in categories outside that "
                 "scope (Paarl Trails, Retül, warranty, display) and is not counted anywhere "
                 "on this page.")
    if n["adjustments_in"]:
        left += (f" Includes ZAR {money(n['adjustments_in'])} of off-Lightspeed sales from "
                 "Adjustments.xlsx.")

    right = ("Budget is prorated by day, so a part-covered month contributes only that part "
             "of its budget. Wheelsets carries no budget line, so the variance column and "
             f"its total compare the {len(budgeted_categories)} budgeted categories only.")
    if n["zero_cost"]:
        zc_pct = n["zero_cost"] / rv["revenue"] * 100 if rv["revenue"] else 0
        right += (f" ZAR {money(n['zero_cost'])} of revenue ({pct(zc_pct)}) carries no unit cost — "
                  "service and labour lines legitimately have no COGS, which is why Service "
                  "Centre shows a margin near 100%.")
    return f'<footer class="notes"><div>{left}</div><div>{right}</div></footer>'


def review_sections(rv):
    """The three sections shared by both review pages."""
    sply_span = f"vs {fmt_day(rv['sply_from'])} – {fmt_day(rv['sply_to'])}"
    return f'''<section>
      {section_header("Category overview", "Budgeted categories and Wheelsets · open a row for its subcategories and sizes", "tight", level=3)}
      {rv["category_body"]}
    </section>
    <section>
      {section_header("Shopify sales", f"Online channel · {sply_span}", "tight", level=3)}
      {rv["shopify_body"]}
    </section>
    <section>
      {section_header("Top ten sellers", "By revenue · range, opening into model and size", "tight", level=3)}
      {rv["top_body"]}
    </section>
    {review_footer(rv)}'''


month_review = build_review(month_from, month_to)
fy_review = build_review(fy_from, fy_to)

month_span = f"{fmt_day(month_from)} – {fmt_day(month_to)}"
fy_span = f"{fmt_day(fy_from)} – {fmt_day(fy_to)}"
period_counts = q1(f"""
    WITH {VALID_SALE_CTE}
    SELECT (SELECT COUNT(*) FROM valid) AS sales, (SELECT COUNT(*) FROM lines) AS lines
""", period_params(month_from, month_to))

# ================================================================== SHEET 1 — the month

st.markdown(
    f'''<div class="sheet s1">
  <div class="band">
    <div>
      <div class="eyebrow">Specialized Paarl · Lightspeed Retail</div>
      <h1>{month_label(month_key)}</h1>
      <div class="period">{month_span} · ZAR, ex-VAT and net of discounts</div>
    </div>
    <div class="bandright">
      <div class="txt">
        <div class="eyebrow">Page 1 of 2 · Month in review</div>
        <div class="synced">{esc(synced_text)}</div>
        <div class="counts">{period_counts["sales"]:,} sales · {period_counts["lines"]:,} lines</div>
      </div>
      {logo_img()}
    </div>
  </div>
  <div class="inner">
    {month_review["cards"]}
    {review_sections(month_review)}
  </div>
</div>''',
    unsafe_allow_html=True,
)

# ================================================================== SHEET 2 — the financial year

st.markdown(
    f'''<div class="sheet s1 s2">
  <div class="inner">
    {page_header(f"FY{fy_key} to date", f"Specialized Paarl · Page 2 of 2 · {fy_span}")}
    {fy_review["cards"]}
    {review_sections(fy_review)}
  </div>
</div>''',
    unsafe_allow_html=True,
)


# ================================================================== APPENDIX
#
# Attach rate and stock position sit beside the report rather than in it: both
# are standing operational analyses rather than a period result, and the stock
# position is not period-filtered at all. They are on their own sheets, and off
# the printed document unless the control bar asks for them, so the report
# itself is always exactly the two pages above.

def attach_stats(from_date, to_date):
    """Accessories sold in the same transaction as a complete bike.

    Attached lines are scoped the same way the rest of the report is, so the
    attached revenue here and the category overview on page 1 are the same
    money counted two ways rather than two different universes.
    """
    params = {**period_params(from_date, to_date), **CAT_PARAMS}
    summary = q1(f"""
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
            WHERE c.top_level_name IN {CAT_IN}
              AND c.top_level_name NOT IN ('BIKE', 'TURBO')
        )
        SELECT (SELECT COUNT(*) FROM bike_sales) AS bike_transactions,
               (SELECT COUNT(DISTINCT sale_id) FROM attached WHERE revenue > 0) AS with_attachment,
               (SELECT COALESCE(SUM(revenue), 0) FROM attached) AS attached_revenue,
               (SELECT COALESCE(SUM(quantity), 0) FROM attached) AS attached_units
    """, params)
    by_cat = q(f"""
        WITH {VALID_SALE_CTE},
        bike_sales AS (
            SELECT DISTINCT l.sale_id FROM lines l
            JOIN item_models im ON im.item_id = l.item_id WHERE im.kind = 'complete'
        )
        SELECT c.top_level_name AS category,
               SUM(l.quantity) AS units,
               SUM(l.revenue) AS revenue,
               SUM(l.revenue) - SUM(l.cogs) AS gross_profit
        FROM bike_sales bs JOIN lines l ON l.sale_id = bs.sale_id
        LEFT JOIN items i ON i.item_id = l.item_id
        LEFT JOIN categories c ON c.category_id = i.category_id
        WHERE c.top_level_name IN {CAT_IN}
          AND c.top_level_name NOT IN ('BIKE', 'TURBO')
        GROUP BY category
        HAVING SUM(l.revenue) <> 0
        ORDER BY revenue DESC
    """, params)
    txns = summary["bike_transactions"] or 0
    return summary, by_cat, txns, ((summary["with_attachment"] / txns * 100) if txns else None)


def attach_row(label, summary, txns, rate):
    return stats_html([
        {"label": "Bike sales", "value": f"{txns:,}"},
        {"label": "With an accessory", "value": pct(rate),
         "foot": f'{summary["with_attachment"]:,} of {txns:,}'},
        {"label": "Attached revenue", "value": money(summary["attached_revenue"]),
         "foot": f'{summary["attached_units"]:,.0f} units'},
        {"label": "Per bike sold",
         "value": money(summary["attached_revenue"] / txns if txns else 0), "foot": label},
    ])


m_sum, m_cat, m_txns, m_rate = attach_stats(month_from, month_to)
f_sum, f_cat, f_txns, f_rate = attach_stats(fy_from, fy_to)
attach_rows = [[r["category"], f'{r["units"]:,.0f}', money(r["revenue"]),
                money(r["gross_profit"]), pct(margin_of(r["gross_profit"], r["revenue"]))]
               for _, r in f_cat.iterrows()]

st.markdown(
    f'''<div class="sheet sn appendix">
  {page_header("Accessory attach rate", "Specialized Paarl · Appendix A")}
  <section>
    {section_header(f"{month_label(month_key)}", month_span, "sub", level=3)}
    {attach_row("This month", m_sum, m_txns, m_rate)}
  </section>
  <section>
    {section_header(f"FY{fy_key} to date", fy_span, "sub", level=3)}
    {attach_row("Year to date", f_sum, f_txns, f_rate)}
    {html_table(["Attached category", "Units", "Revenue", "Gross profit", "Margin"],
                attach_rows, name_col=0, muted_cols=(4,),
                empty_text="No attached sales in this period.")}
    <div class="note">An attachment is any line on the same transaction as a complete bike
    that is not itself a bike. Scoped, like the report, to the budgeted categories and
    Wheelsets. A bike sold with nothing else counts against the rate, not out of it.</div>
  </section>
</div>''',
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------- stock (never period-filtered)

anchor_row = q1("""
    SELECT substr(MAX(COALESCE(complete_time, sale_time)), 1, 10) AS d
    FROM sales WHERE completed = 1 AND voided = 0
""")
anchor = anchor_row["d"] if anchor_row else None

if anchor:
    trail_from = (dt.date.fromisoformat(anchor) - dt.timedelta(days=364)).isoformat()
    trail_params = {"trail_from": trail_from, "anchor": anchor, **CAT_PARAMS}

    TRAILING = """
    trailing AS (
        SELECT sl.item_id,
               SUM(CASE WHEN sl.quantity > 0 THEN sl.quantity ELSE 0 END) AS gross_units,
               SUM(sl.quantity * CASE WHEN sl.fifo_cost > 0 THEN sl.fifo_cost ELSE sl.avg_cost END) AS cogs
        FROM sale_lines sl
        JOIN sales s ON s.sale_id = sl.sale_id
        WHERE s.completed = 1 AND s.voided = 0
          AND substr(COALESCE(s.complete_time, s.sale_time), 1, 10) BETWEEN :trail_from AND :anchor
        GROUP BY sl.item_id
    )
    """
    stock_summary = q1(f"""
        SELECT COALESCE(SUM(s.value_avg_cost), 0) AS stock_value,
               COALESCE(SUM(s.qoh), 0) AS stock_units,
               COUNT(*) AS stocked_skus
        FROM item_shops s
        JOIN items i ON i.item_id = s.item_id
        JOIN categories c ON c.category_id = i.category_id
        WHERE s.qoh > 0 AND c.top_level_name IN {CAT_IN}
    """, trail_params)
    trailing_cogs = q1(f"""
        WITH {TRAILING}
        SELECT COALESCE(SUM(t.cogs), 0) AS cogs FROM trailing t
        JOIN items i ON i.item_id = t.item_id
        JOIN categories c ON c.category_id = i.category_id
        WHERE c.top_level_name IN {CAT_IN}
    """, trail_params)["cogs"]
    turn = trailing_cogs / stock_summary["stock_value"] if stock_summary["stock_value"] else None

    dead_total = q1(f"""
        WITH {TRAILING}
        SELECT COALESCE(SUM(s.value_avg_cost), 0) AS value, COUNT(*) AS skus
        FROM item_shops s
        JOIN items i ON i.item_id = s.item_id
        JOIN categories c ON c.category_id = i.category_id
        LEFT JOIN trailing t ON t.item_id = s.item_id
        WHERE s.qoh > 0 AND s.value_avg_cost > 0
          AND c.top_level_name IN {CAT_IN}
          AND COALESCE(t.gross_units, 0) <= 0
          AND (i.created_at IS NULL OR julianday(:anchor) - julianday(substr(i.created_at, 1, 10)) > 90)
    """, trail_params)

    stock_by_cat = q(f"""
        WITH {TRAILING},
        cat_cogs AS (
            SELECT c.top_level_name AS category, SUM(t.cogs) AS trailing_cogs
            FROM trailing t
            JOIN items i ON i.item_id = t.item_id
            JOIN categories c ON c.category_id = i.category_id
            WHERE c.top_level_name IN {CAT_IN}
            GROUP BY category
        ),
        cat_stock AS (
            SELECT c.top_level_name AS category,
                   SUM(s.qoh) AS qoh, SUM(s.value_avg_cost) AS stock_value
            FROM item_shops s
            JOIN items i ON i.item_id = s.item_id
            JOIN categories c ON c.category_id = i.category_id
            WHERE s.qoh > 0 AND c.top_level_name IN {CAT_IN}
            GROUP BY category
        )
        SELECT st.category, st.qoh, st.stock_value, COALESCE(cc.trailing_cogs, 0) AS trailing_cogs
        FROM cat_stock st LEFT JOIN cat_cogs cc ON cc.category = st.category
        WHERE st.stock_value > 0
        ORDER BY st.stock_value DESC
    """, trail_params)

    bike_cover = q(f"""
        WITH {TRAILING}
        SELECT im.model || CASE WHEN im.size IS NOT NULL THEN ' ' || im.size ELSE '' END AS label,
               SUM(s.qoh) AS qoh, SUM(s.value_avg_cost) AS stock_value,
               COALESCE(SUM(t.gross_units), 0) AS trailing_gross_units
        FROM item_shops s
        JOIN item_models im ON im.item_id = s.item_id
        LEFT JOIN trailing t ON t.item_id = s.item_id
        WHERE s.qoh > 0 AND im.kind = 'complete'
        GROUP BY label
        HAVING SUM(s.value_avg_cost) > 0
        ORDER BY stock_value DESC LIMIT 12
    """, trail_params)

    stock_cat_rows = []
    for _, r in stock_by_cat.iterrows():
        t = r["trailing_cogs"] / r["stock_value"] if r["stock_value"] else None
        stock_cat_rows.append([r["category"], f'{r["qoh"]:,.0f}', money(r["stock_value"]),
                               money(r["trailing_cogs"]),
                               f"{t:.1f}×" if t else "—", f"{52 / t:.1f}" if t else "—"])

    cover_rows = []
    for _, r in bike_cover.iterrows():
        sold = r["trailing_gross_units"] or 0
        weeks = (r["qoh"] * 52 / sold) if sold > 0 else None
        weeks_cell = "no sales in 12m" if weeks is None else f"{weeks:.1f}"
        if weeks is None or weeks > 40:
            weeks_cell = Raw(f'<span style="color:var(--clay)">{esc(weeks_cell)}</span>')
        cover_rows.append([r["label"], f'{r["qoh"]:,.0f}', money(r["stock_value"]),
                           f"{sold:,.0f}", weeks_cell])

    st.markdown(
        f'''<div class="sheet sn appendix">
  {page_header("Stock position", "Specialized Paarl · Appendix B")}
  <section>
    {section_header("Stock on hand and stock turn",
                    f"Held now · trade over the 12 months to {anchor} · not period-filtered",
                    "sub", level=3)}
    {stats_html([
        {"label": "Stock at cost", "value": money(stock_summary["stock_value"]),
         "foot": f'{stock_summary["stock_units"]:,.0f} units, {stock_summary["stocked_skus"]:,} SKUs'},
        {"label": "Stock turn", "value": f"{turn:.2f}×" if turn else "—",
         "foot": f"{compact(trailing_cogs)} COGS over 12m"},
        {"label": "Weeks of cover", "value": f"{52 / turn:.1f}" if turn else "—",
         "foot": "At the trailing sales rate"},
        {"label": "Aged stock", "value": money(dead_total["value"]), "colour": "var(--clay)",
         "foot": f'{dead_total["skus"]:,} SKUs, no sales in 12m'},
    ])}
  </section>
  <section>
    {section_header("By category", "Units held, cost, turn and weeks of cover", "sub", level=3)}
    {html_table(["Category", "Units held", "At cost", "12m COGS", "Turn", "Weeks"],
                stock_cat_rows, name_col=0)}
  </section>
  <section>
    {section_header("Bike cover by model and size", "Complete bikes held now", "sub", level=3)}
    {html_table(["Model / size", "Units held", "At cost", "12m sold", "Weeks cover"],
                cover_rows, name_col=0)}
    <div class="note">Stock turn is a proxy — trailing COGS divided by closing stock at cost —
    and is a point-in-time position, never period-filtered. Scoped, like the report, to the
    budgeted categories and Wheelsets.</div>
  </section>
</div>''',
        unsafe_allow_html=True,
    )
