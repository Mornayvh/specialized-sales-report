import 'dotenv/config';
import express from 'express';
import { db, getSyncState } from './db.js';
import { buildAuthorizeUrl, exchangeCodeForToken } from './lightspeed.js';
import { runSync } from './sync.js';
import { readAdjustments, readBudget } from './sheets.js';

const app = express();
app.use(express.static('public'));

app.get('/oauth/authorize', (req, res) => {
  res.redirect(buildAuthorizeUrl());
});

app.get('/oauth/callback', async (req, res) => {
  try {
    await exchangeCodeForToken(req.query.code);
    res.send('Authorized. You can close this tab and run `npm run sync`, then load the dashboard.');
  } catch (err) {
    res.status(500).send(`OAuth error: ${err.message}`);
  }
});

let syncRunning = false;
app.post('/api/sync', async (req, res) => {
  if (syncRunning) return res.status(409).json({ error: 'A sync is already running.' });
  syncRunning = true;
  try {
    await runSync({ incremental: true });
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  } finally {
    syncRunning = false;
  }
});

/*
 * Revenue definitions — these reconcile to the firm's own Lightspeed report.
 *
 *  - Only sales with completed = 1 AND voided = 0 count. Open carts and
 *    unconverted quotes carry non-zero totals in the API but are not revenue.
 *  - Revenue is EX-VAT and NET OF DISCOUNTS.
 *      sale level: calc_subtotal - calc_discount
 *      line level: calc_subtotal - calc_line_discount - calc_transaction_discount
 *    Line level is used throughout so revenue, COGS and margin come off one
 *    grain; /api/reconcile proves the two bases agree.
 *  - COGS uses the per-unit cost captured on the line AT TIME OF SALE
 *    (fifo_cost, falling back to avg_cost). The item's current cost would
 *    misstate historical margin. Lines with no cost (labour, service) are
 *    legitimately zero-COGS; zero_cost_revenue reports how much revenue that is.
 *  - Period basis is COALESCE(complete_time, sale_time) — when the sale closed,
 *    not when the record was last touched.
 */
// SQLite/better-sqlite3 quirk (confirmed on 3.49.2): a HAVING clause that
// references an aggregate by its SELECT alias (e.g. `HAVING revenue <> 0` for
// `SUM(l.revenue) AS revenue`) can silently drop a group when the query also
// has a LEFT JOIN producing heavy NULL fan-out (many physical rows collapsing
// into one group). It reproduced reliably here: an 'Uncategorised' group
// summing to a genuine, nonzero ZAR 869.57 vanished from a result whose HAVING
// read `revenue <> 0`, even though the value plainly satisfies that condition —
// while `HAVING SUM(l.revenue) <> 0` (repeating the aggregate) kept it.
// EVERY HAVING clause in this file repeats its aggregate expression rather than
// referencing the output alias, for exactly this reason.
const VALID_SALE_CTE = `
  valid AS (
    SELECT sale_id, employee_id, shop_id,
           COALESCE(complete_time, sale_time) AS ts,
           (calc_subtotal - calc_discount) AS sale_revenue
    FROM sales
    WHERE completed = 1 AND voided = 0
      AND (@from IS NULL OR substr(COALESCE(complete_time, sale_time), 1, 10) >= @from)
      AND (@to   IS NULL OR substr(COALESCE(complete_time, sale_time), 1, 10) <= @to)
  ),
  lines AS (
    SELECT sl.sale_line_id, sl.sale_id, sl.item_id, sl.quantity,
           v.ts, v.employee_id, v.shop_id,
           (sl.calc_subtotal - sl.calc_line_discount - sl.calc_transaction_discount) AS revenue,
           (sl.quantity * CASE WHEN sl.fifo_cost > 0 THEN sl.fifo_cost ELSE sl.avg_cost END) AS cogs,
           -- "no cost on record" is a property of the item's cost fields, NOT of the
           -- computed COGS: a return has negative COGS but its cost is perfectly known.
           (sl.fifo_cost <= 0 AND sl.avg_cost <= 0) AS no_cost_on_record
    FROM sale_lines sl
    JOIN valid v ON v.sale_id = sl.sale_id
  )
`;

function periodParams(req) {
  return { from: req.query.from || null, to: req.query.to || null };
}

/*
 * Same-period-last-year support. Shifts a "YYYY-MM-DD" bound back one calendar
 * year, clamping the day so a leap-year 29 Feb lands on 28 Feb the year before
 * rather than rolling into March. Both bounds are shifted the same way, so a
 * selected Jun–Aug 2026 compares against Jun–Aug 2025 — the exact months last
 * year, not just "365 days back" (which would drift across month boundaries).
 */
function shiftYear(dateStr, delta) {
  if (!dateStr) return null;
  const [y, m, d] = dateStr.split('-').map(Number);
  const targetYear = y + delta;
  const daysInMonth = new Date(Date.UTC(targetYear, m, 0)).getUTCDate();
  const day = Math.min(d, daysInMonth);
  return `${targetYear}-${String(m).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
}

// No comparable prior period for an unbounded "all time" view — both bounds null.
function priorYearParams(params) {
  if (!params.from && !params.to) return null;
  return { from: shiftYear(params.from, -1), to: shiftYear(params.to, -1) };
}

// Expand a sparse "YYYY-MM" series into every month between first and last.
function fillMonths(rows) {
  if (rows.length < 2) return rows;
  const byMonth = new Map(rows.map(r => [r.month, r]));
  const [startY, startM] = rows[0].month.split('-').map(Number);
  const [endY, endM] = rows[rows.length - 1].month.split('-').map(Number);
  const out = [];
  for (let y = startY, m = startM; y < endY || (y === endY && m <= endM);) {
    const key = `${y}-${String(m).padStart(2, '0')}`;
    out.push(byMonth.get(key) ?? { month: key, revenue: 0, gross_profit: 0 });
    if (++m > 12) { m = 1; y++; }
  }
  return out;
}


/*
 * ---------------------------------------------------------------------------
 * Off-Lightspeed adjustments
 * ---------------------------------------------------------------------------
 * Adjustments.xlsx holds sales the store made outside Lightspeed. They are real
 * revenue, so they are added to the actuals — but ALWAYS disclosed separately,
 * because the Lightspeed-only figure is the one that ties to the shop's own
 * report. /api/reconcile deliberately stays Lightspeed-only for that reason.
 *
 * Category comes from matching the sheet's Description against the item
 * catalogue; unmatched rows are reported rather than silently dropped or lumped
 * into a category they do not belong to.
 */
function descriptionToCategory() {
  const map = new Map();
  const rows = db.prepare(`
    SELECT UPPER(TRIM(i.description)) AS d, COALESCE(c.top_level_name, 'Uncategorised') AS cat
    FROM items i LEFT JOIN categories c ON c.category_id = i.category_id
    WHERE i.description IS NOT NULL
  `).all();
  for (const r of rows) if (!map.has(r.d)) map.set(r.d, r.cat);
  return map;
}

async function getAdjustments(from, to) {
  const { rows, missing } = await readAdjustments();
  const map = descriptionToCategory();
  const inPeriod = rows.filter(r => r.date && (!from || r.date >= from) && (!to || r.date <= to));
  const byCategory = new Map();
  let revenue = 0, cost = 0, units = 0;
  const unmatched = [];
  for (const r of inPeriod) {
    const cat = map.get((r.description || '').toUpperCase().trim()) || null;
    if (!cat) unmatched.push(r.description);
    const key = cat || 'Unmatched (adjustments)';
    const agg = byCategory.get(key) || { revenue: 0, cost: 0, units: 0 };
    agg.revenue += r.revenue; agg.cost += r.cost; agg.units += r.qty;
    byCategory.set(key, agg);
    revenue += r.revenue; cost += r.cost; units += r.qty;
  }
  return {
    missing, rows: inPeriod, byCategory,
    totals: { revenue, cost, units, gross_profit: revenue - cost, lines: inPeriod.length },
    unmatched: [...new Set(unmatched)]
  };
}

/*
 * Budget proration. A budget row is a whole month, so a period that covers only
 * part of a month must take only that share of it — otherwise a month-to-date
 * view compares a few days of actuals against a full month of budget and reads
 * as a catastrophic miss.
 */
function prorateBudget(budgetRows, from, to) {
  const out = new Map();   // category -> amount
  for (const r of budgetRows) {
    const [y, m] = r.month.split('-').map(Number);
    const monthStart = new Date(Date.UTC(y, m - 1, 1));
    const monthEnd = new Date(Date.UTC(y, m, 0));
    const daysInMonth = monthEnd.getUTCDate();
    const lo = from ? new Date(from + 'T00:00:00Z') : monthStart;
    const hi = to ? new Date(to + 'T00:00:00Z') : monthEnd;
    const overlapStart = lo > monthStart ? lo : monthStart;
    const overlapEnd = hi < monthEnd ? hi : monthEnd;
    if (overlapEnd < overlapStart) continue;
    const overlapDays = Math.round((overlapEnd - overlapStart) / 86400000) + 1;
    const share = Math.min(1, overlapDays / daysInMonth);
    out.set(r.category, (out.get(r.category) || 0) + r.amount * share);
  }
  return out;
}

app.get('/api/dashboard', async (req, res) => {
  const params = periodParams(req);

  const kpisStmt = db.prepare(`
    WITH ${VALID_SALE_CTE}
    SELECT
      (SELECT COALESCE(SUM(revenue), 0) FROM lines)                        AS revenue,
      (SELECT COALESCE(SUM(cogs), 0) FROM lines)                           AS cogs,
      (SELECT COALESCE(SUM(quantity), 0) FROM lines)                       AS units,
      (SELECT COUNT(*) FROM valid)                                         AS transactions,
      (SELECT COALESCE(SUM(sale_revenue), 0) FROM valid)                   AS sale_level_revenue,
      (SELECT COALESCE(SUM(revenue), 0) FROM lines WHERE no_cost_on_record) AS zero_cost_revenue,
      (SELECT COALESCE(SUM(revenue), 0) FROM lines WHERE revenue < 0)       AS returns_revenue,
      (SELECT COUNT(*) FROM lines WHERE revenue < 0)                        AS returns_lines
  `);
  const kpis = kpisStmt.get(params);

  /*
   * Same-period-last-year. Reuses the identical kpis query with the shifted
   * date bounds, and folds in the same off-Lightspeed adjustments treatment the
   * headline figures get, so the comparison is apples-to-apples with what the
   * card actually displays (not Lightspeed-only vs. Lightspeed-plus-adjustments).
   * null when the current view has no bounds ("All time") — there is no
   * meaningful "prior year" of an unbounded range.
   */
  const priorParams = priorYearParams(params);
  let sply = null;
  if (priorParams) {
    const priorRaw = kpisStmt.get(priorParams);
    const priorAdj = await getAdjustments(priorParams.from, priorParams.to);
    sply = {
      revenue: priorRaw.revenue + priorAdj.totals.revenue,
      cogs: priorRaw.cogs + priorAdj.totals.cost,
      units: priorRaw.units + priorAdj.totals.units,
      transactions: priorRaw.transactions,
      period: { from: priorParams.from, to: priorParams.to }
    };
  }

  const byCategory = db.prepare(`
    WITH ${VALID_SALE_CTE}
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
  `).all(params);

  const topProducts = db.prepare(`
    WITH ${VALID_SALE_CTE}
    SELECT COALESCE(i.description, '(unknown item ' || l.item_id || ')') AS product,
           i.system_sku AS sku,
           COALESCE(c.top_level_name, 'Uncategorised') AS category,
           SUM(l.quantity) AS units,
           SUM(l.revenue) AS revenue,
           SUM(l.revenue) - SUM(l.cogs) AS gross_profit
    FROM lines l
    LEFT JOIN items i ON i.item_id = l.item_id
    LEFT JOIN categories c ON c.category_id = i.category_id
    GROUP BY l.item_id
    ORDER BY revenue DESC
    LIMIT 10
  `).all(params);

  /*
   * Bike sales by model, drillable. kind defaults to complete bikes only:
   * framesets, bare frames, build kits and rentals are real revenue but not a
   * bike sale, and blending them would inflate a model's figures by tens of
   * thousands of rand per unit.
   *
   * Three drill levels, driven by which parents the caller has selected:
   *   (none)               -> models            EPIC 8, LEVO SL
   *   drillModel           -> trims of it       COMP, EXPERT, PRO, S-WORKS
   *   drillModel + Trim    -> sizes of that     S2, S3, S4 / S, M, L / 52, 54, 56
   *
   * Size is only offered at the deepest level on purpose: within one model+trim
   * the sizing system is consistent, whereas a size axis spanning models would
   * mix S-Sizing, alpha and centimetres into one meaningless scale.
   */
  const kindFilter = req.query.modelKind || 'complete';
  const drillModel = req.query.drillModel || null;
  const drillTrim = req.query.drillTrim || null;
  const level = drillModel ? (drillTrim ? 'size' : 'trim') : 'model';

  const groupExpr = {
    model: 'im.model',
    trim:  `COALESCE(im.trim, '(no trim recorded)')`,
    size:  `COALESCE(im.size, '(no size recorded)')`
  }[level];

  const drillClauses = [];
  if (drillModel) drillClauses.push('AND im.model = @drillModel');
  if (drillTrim) {
    drillClauses.push(drillTrim === '(no trim recorded)'
      ? 'AND im.trim IS NULL'
      : 'AND im.trim = @drillTrim');
  }
  const kindClause = kindFilter === 'all' ? '' : 'AND im.kind = @modelKind';

  // NB: the output alias must NOT be `model`/`size` — item_models has real columns
  // by those names, and GROUP BY would bind to the column rather than to this
  // expression, silently collapsing every child into one row.
  const byModel = db.prepare(`
    WITH ${VALID_SALE_CTE}
    SELECT ${groupExpr} AS drill_label,
           MIN(im.size_system) AS size_system,
           SUM(l.revenue) AS revenue,
           SUM(l.revenue) - SUM(l.cogs) AS gross_profit,
           SUM(l.quantity) AS units
    FROM lines l
    JOIN item_models im ON im.item_id = l.item_id
    WHERE im.model IS NOT NULL ${kindClause} ${drillClauses.join(' ')}
    GROUP BY drill_label
    HAVING SUM(l.revenue) <> 0
    ORDER BY revenue DESC
  `).all({
    ...params,
    ...(kindFilter === 'all' ? {} : { modelKind: kindFilter }),
    ...(drillModel ? { drillModel } : {}),
    ...(drillTrim && drillTrim !== '(no trim recorded)' ? { drillTrim } : {})
  }).map(r => ({
    model: r.drill_label, size_system: r.size_system,
    revenue: r.revenue, gross_profit: r.gross_profit, units: r.units
  }));

  // How much bike revenue the model breakdown does NOT cover, so the split is
  // never mistaken for the whole of bike sales.
  const modelCoverage = db.prepare(`
    WITH ${VALID_SALE_CTE}
    SELECT
      (SELECT COALESCE(SUM(l.revenue), 0) FROM lines l
         LEFT JOIN items i ON i.item_id = l.item_id
         LEFT JOIN categories c ON c.category_id = i.category_id
        WHERE c.top_level_name IN ('BIKE','TURBO'))                       AS bike_category_revenue,
      (SELECT COALESCE(SUM(l.revenue), 0) FROM lines l
         JOIN item_models im ON im.item_id = l.item_id
        WHERE im.kind = 'complete')                                        AS complete_bike_revenue,
      (SELECT COALESCE(SUM(l.revenue), 0) FROM lines l
         JOIN item_models im ON im.item_id = l.item_id
        WHERE im.kind <> 'complete')                                       AS non_bike_form_revenue
  `).get(params);

  /*
   * ATTACH RATE — accessories sold in the same transaction as a complete bike.
   * "Attached" means any line NOT in the BIKE/TURBO categories, so a second bike
   * in the same sale is not counted as its own accessory.
   */
  const attachSummary = db.prepare(`
    WITH ${VALID_SALE_CTE},
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
  `).get(params);

  const attachByCategory = db.prepare(`
    WITH ${VALID_SALE_CTE},
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
  `).all(params);

  /*
   * DISCOUNT LEAKAGE. Discount is measured against GROSS (list) revenue —
   * revenue + discount — because discount as a share of the already-discounted
   * net would understate the giveaway.
   */
  const discountSummary = db.prepare(`
    WITH ${VALID_SALE_CTE}
    SELECT COALESCE(SUM(sl.calc_line_discount + sl.calc_transaction_discount), 0) AS total_discount,
           COALESCE(SUM(l.revenue), 0) AS net_revenue,
           SUM(CASE WHEN sl.calc_line_discount + sl.calc_transaction_discount > 0 THEN 1 ELSE 0 END) AS discounted_lines,
           COUNT(*) AS total_lines
    FROM lines l JOIN sale_lines sl ON sl.sale_line_id = l.sale_line_id
  `).get(params);

  const discountByCategory = db.prepare(`
    WITH ${VALID_SALE_CTE}
    SELECT COALESCE(c.top_level_name, 'Uncategorised') AS category,
           SUM(sl.calc_line_discount + sl.calc_transaction_discount) AS discount,
           SUM(l.revenue) AS net_revenue
    FROM lines l
    JOIN sale_lines sl ON sl.sale_line_id = l.sale_line_id
    LEFT JOIN items i ON i.item_id = l.item_id
    LEFT JOIN categories c ON c.category_id = i.category_id
    GROUP BY category
    HAVING SUM(sl.calc_line_discount + sl.calc_transaction_discount) > 0
    ORDER BY discount DESC
  `).all(params);

  const discountByModel = db.prepare(`
    WITH ${VALID_SALE_CTE}
    SELECT im.model AS model,
           SUM(sl.calc_line_discount + sl.calc_transaction_discount) AS discount,
           SUM(l.revenue) AS net_revenue
    FROM lines l
    JOIN sale_lines sl ON sl.sale_line_id = l.sale_line_id
    JOIN item_models im ON im.item_id = l.item_id
    WHERE im.kind = 'complete'
    GROUP BY im.model
    HAVING SUM(sl.calc_line_discount + sl.calc_transaction_discount) > 0
    ORDER BY discount DESC LIMIT 12
  `).all(params);

  /*
   * Merge off-Lightspeed adjustments into the actuals. Kept as an explicit,
   * separately reported block so the Lightspeed-only figure remains visible.
   */
  const adj = await getAdjustments(params.from, params.to);
  const kpisWithAdj = {
    ...kpis,
    revenue: kpis.revenue + adj.totals.revenue,
    cogs: kpis.cogs + adj.totals.cost,
    units: kpis.units + adj.totals.units,
    lightspeed_revenue: kpis.revenue,
    adjustments_revenue: adj.totals.revenue,
    adjustments_gross_profit: adj.totals.gross_profit,
    adjustments_lines: adj.totals.lines,
    sply
  };
  const catMap = new Map(byCategory.map(r => [r.category, { ...r }]));
  for (const [cat, a] of adj.byCategory) {
    const row = catMap.get(cat) || { category: cat, revenue: 0, gross_profit: 0, units: 0 };
    row.revenue += a.revenue;
    row.gross_profit += a.revenue - a.cost;
    row.units += a.units;
    catMap.set(cat, row);
  }
  const byCategoryWithAdj = [...catMap.values()].filter(r => r.revenue !== 0)
    .sort((a, b) => b.revenue - a.revenue);

  res.json({
    kpis: kpisWithAdj, byCategory: byCategoryWithAdj, topProducts,
    adjustments: { ...adj.totals, unmatched: adj.unmatched, missing: adj.missing },
    attachSummary, attachByCategory,
    discountSummary, discountByCategory, discountByModel,
    byModel, modelCoverage, modelKind: kindFilter,
    modelDrill: { level, model: drillModel, trim: drillTrim }
  });
});

/*
 * Audit trail for the model inference, so the classification is inspectable
 * rather than a black box: what the description said, what the category tree
 * said, and every bike-category item it failed to classify.
 */
app.get('/api/model-audit', (req, res) => {
  const disagreements = db.prepare(`
    SELECT i.description, im.model AS description_model, im.tree_model, im.trim, im.kind
    FROM item_models im JOIN items i ON i.item_id = im.item_id
    WHERE im.agrees_with_tree = 0
    ORDER BY i.description LIMIT 500
  `).all();
  const unclassified = db.prepare(`
    SELECT i.description, c.full_path_name
    FROM items i JOIN categories c ON c.category_id = i.category_id
    LEFT JOIN item_models im ON im.item_id = i.item_id
    WHERE c.top_level_name IN ('BIKE','TURBO') AND im.item_id IS NULL
    LIMIT 500
  `).all();
  const noTrim = db.prepare(`
    SELECT i.description, im.model
    FROM item_models im JOIN items i ON i.item_id = im.item_id
    WHERE im.kind = 'complete' AND im.trim IS NULL
    ORDER BY i.description LIMIT 500
  `).all();
  const summary = db.prepare(`
    SELECT COUNT(*) AS classified,
           SUM(CASE WHEN source = 'description' THEN 1 ELSE 0 END) AS from_description,
           SUM(CASE WHEN source = 'category' THEN 1 ELSE 0 END) AS from_category,
           SUM(CASE WHEN agrees_with_tree = 0 THEN 1 ELSE 0 END) AS disagreements,
           SUM(CASE WHEN kind = 'complete' THEN 1 ELSE 0 END) AS complete_bikes
    FROM item_models
  `).get();
  res.json({ summary, disagreements, unclassified, noTrim });
});

/*
 * BUDGET vs ACTUAL.
 *
 * Only the categories the budget sheet actually budgets are compared — the sheet
 * covers 7 of the 13 Lightspeed categories. Comparing an unbudgeted category
 * against an implied zero would show its entire revenue as overspend, so
 * unbudgeted revenue is reported separately as context, never as variance.
 *
 * Actuals include the off-Lightspeed adjustments, since the budget is a budget
 * for the whole business, not just for what happened to go through the till.
 */
app.get('/api/budget', async (req, res) => {
  const params = periodParams(req);
  const { rows: budgetRows, missing } = await readBudget();
  if (missing || !budgetRows.length) return res.json({ missing: true });

  const categories = [...new Set(budgetRows.map(r => r.category))];

  /*
   * The comparison window is the INTERSECTION of what was asked for, what the
   * budget covers, and what trade data exists. Without this the default view
   * compared 4 years of budget (including 9 future months) against 5.6 years of
   * actuals and reported a meaningless +16% — neither side covering the same time.
   */
  const months = [...new Set(budgetRows.map(r => r.month))].sort();
  const budgetFirst = months[0] + '-01';
  const [by, bm] = months[months.length - 1].split('-').map(Number);
  const budgetLast = new Date(Date.UTC(by, bm, 0)).toISOString().slice(0, 10);
  const dataRange = db.prepare(`
    SELECT substr(MIN(COALESCE(complete_time, sale_time)), 1, 10) AS lo,
           substr(MAX(COALESCE(complete_time, sale_time)), 1, 10) AS hi
    FROM sales WHERE completed = 1 AND voided = 0
  `).get();

  const maxOf = (...xs) => xs.filter(Boolean).sort().pop();
  const minOf = (...xs) => xs.filter(Boolean).sort().shift();
  const effFrom = maxOf(params.from, budgetFirst, dataRange.lo);
  const effTo = minOf(params.to, budgetLast, dataRange.hi);
  const clipped = (effFrom !== (params.from ?? dataRange.lo)) || (effTo !== (params.to ?? dataRange.hi));
  const effParams = { from: effFrom, to: effTo };

  const prorated = prorateBudget(budgetRows, effFrom, effTo);

  const actualRows = db.prepare(`
    WITH ${VALID_SALE_CTE}
    SELECT COALESCE(c.top_level_name, 'Uncategorised') AS category,
           SUM(l.revenue) AS revenue,
           SUM(l.revenue) - SUM(l.cogs) AS gross_profit
    FROM lines l
    LEFT JOIN items i ON i.item_id = l.item_id
    LEFT JOIN categories c ON c.category_id = i.category_id
    GROUP BY category
  `).all(effParams);

  const adj = await getAdjustments(effFrom, effTo);
  const actualByCat = new Map(actualRows.map(r => [r.category, { revenue: r.revenue, gross_profit: r.gross_profit }]));
  for (const [cat, a] of adj.byCategory) {
    const cur = actualByCat.get(cat) || { revenue: 0, gross_profit: 0 };
    cur.revenue += a.revenue;
    cur.gross_profit += a.revenue - a.cost;
    actualByCat.set(cat, cur);
  }

  const comparison = categories.map(cat => {
    const budget = prorated.get(cat) || 0;
    const actual = actualByCat.get(cat)?.revenue || 0;
    return {
      category: cat, budget, actual,
      variance: actual - budget,
      variance_pct: budget ? (actual - budget) / budget * 100 : null,
      gross_profit: actualByCat.get(cat)?.gross_profit || 0
    };
  }).sort((a, b) => b.budget - a.budget);

  const totals = comparison.reduce((t, r) => ({
    budget: t.budget + r.budget, actual: t.actual + r.actual
  }), { budget: 0, actual: 0 });
  totals.variance = totals.actual - totals.budget;
  totals.variance_pct = totals.budget ? totals.variance / totals.budget * 100 : null;

  // Revenue outside the budgeted categories — context, deliberately not variance.
  let unbudgeted = 0;
  for (const [cat, v] of actualByCat) if (!categories.includes(cat)) unbudgeted += v.revenue;

  res.json({
    comparison, totals, unbudgeted, categories,
    budgetRange: { first: months[0], last: months[months.length - 1] },
    requested: { from: params.from, to: params.to },
    // The window actually compared, and whether it had to be narrowed.
    effective: { from: effFrom, to: effTo, clipped }
  });
});

/*
 * MONTHLY TREND — its own endpoint with its own period, deliberately independent
 * of the dashboard's filter so the long-run trend stays readable while the rest
 * of the page is scoped to a short period.
 */
app.get('/api/trend', async (req, res) => {
  const params = periodParams(req);
  const monthlyRaw = db.prepare(`
    WITH ${VALID_SALE_CTE}
    SELECT substr(ts, 1, 7) AS month,
           SUM(revenue) AS revenue,
           SUM(revenue) - SUM(cogs) AS gross_profit
    FROM lines
    GROUP BY month
    ORDER BY month
  `).all(params);

  // Fold off-Lightspeed adjustments into their month so the trend matches the KPIs.
  const adj = await getAdjustments(params.from, params.to);
  const byMonth = new Map(monthlyRaw.map(r => [r.month, { ...r }]));
  for (const r of adj.rows) {
    const m = r.date.slice(0, 7);
    const row = byMonth.get(m) || { month: m, revenue: 0, gross_profit: 0 };
    row.revenue += r.revenue;
    row.gross_profit += r.revenue - r.cost;
    byMonth.set(m, row);
  }
  const merged = [...byMonth.values()].sort((a, b) => a.month.localeCompare(b.month));
  res.json({ monthly: fillMonths(merged) });
});

/*
 * STOCK ON HAND AND STOCK TURN.
 *
 * Deliberately NOT scoped by the dashboard's period filter. Stock is a
 * point-in-time position, so pairing "stock as at today" with, say, 2021 revenue
 * would produce a turn figure that means nothing. This endpoint always reports
 * current stock against a fixed TRAILING 12 MONTHS of trade, and the card says so.
 *
 * Stock turn here is a PROXY: true turn is COGS / AVERAGE inventory, but the API
 * exposes only a current snapshot, so the denominator is the closing position
 * rather than a period average. It reads high when stock has been run down and
 * low when it has been built up. stock_snapshots is accumulating a daily
 * category-level history so later periods can use a real average.
 */
app.get('/api/stock', (req, res) => {
  // Anchor the window on the latest sale in the data, not the wall clock, so the
  // figure does not silently decay if syncing stops.
  const anchor = db.prepare(`
    SELECT substr(MAX(COALESCE(complete_time, sale_time)), 1, 10) AS d
    FROM sales WHERE completed = 1 AND voided = 0
  `).get()?.d;
  if (!anchor) return res.json({ empty: true });
  const from = new Date(Date.parse(anchor) - 364 * 864e5).toISOString().slice(0, 10);

  /*
   * Trailing trade, per item. gross_units counts only POSITIVE lines: it answers
   * "has this ever sold?", which net-of-returns units cannot — an item with
   * returns >= sales nets to zero and would be misread as never having sold.
   * net_units and cogs stay net, since consumption and cost of goods are
   * genuinely reduced by a return.
   */
  const TRAILING = `
    trailing AS (
      SELECT sl.item_id,
             SUM(sl.quantity) AS net_units,
             SUM(CASE WHEN sl.quantity > 0 THEN sl.quantity ELSE 0 END) AS gross_units,
             SUM(sl.quantity * CASE WHEN sl.fifo_cost > 0 THEN sl.fifo_cost ELSE sl.avg_cost END) AS cogs
      FROM sale_lines sl
      JOIN sales s ON s.sale_id = sl.sale_id
      WHERE s.completed = 1 AND s.voided = 0
        AND substr(COALESCE(s.complete_time, s.sale_time), 1, 10) BETWEEN @from AND @anchor
      GROUP BY sl.item_id
    )
  `;
  const p = { from, anchor };

  const summary = db.prepare(`
    WITH ${TRAILING}
    SELECT
      (SELECT COALESCE(SUM(value_avg_cost), 0) FROM item_shops WHERE qoh > 0)      AS stock_value,
      (SELECT COALESCE(SUM(qoh), 0) FROM item_shops WHERE qoh > 0)                 AS stock_units,
      (SELECT COUNT(*) FROM item_shops WHERE qoh > 0)                              AS stocked_skus,
      (SELECT COALESCE(SUM(cogs), 0) FROM trailing)                                AS trailing_cogs,
      (SELECT COALESCE(SUM(value_avg_cost), 0) FROM item_shops WHERE qoh < 0)      AS negative_stock_value,
      (SELECT COUNT(*) FROM item_shops WHERE qoh < 0)                              AS negative_stock_skus
  `).get(p);

  /*
   * Turn per category = the category's TOTAL trailing COGS / its current stock.
   *
   * The numerator must be computed independently of what is currently held. An
   * earlier version joined trailing trade through item_shops, which silently
   * dropped every item that had sold out — leaving only slow movers and their
   * returns, and producing NEGATIVE category COGS. Category COGS summed to
   * ZAR 2.9m against a true ZAR 16.7m.
   */
  const byCategory = db.prepare(`
    WITH ${TRAILING},
    cat_cogs AS (
      SELECT COALESCE(c.top_level_name, 'Uncategorised') AS category,
             SUM(t.cogs) AS trailing_cogs,
             SUM(t.net_units) AS trailing_units
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
           COALESCE(cc.trailing_cogs, 0) AS trailing_cogs,
           COALESCE(cc.trailing_units, 0) AS trailing_units
    FROM cat_stock st
    LEFT JOIN cat_cogs cc ON cc.category = st.category
    WHERE st.stock_value > 0
    ORDER BY st.stock_value DESC
  `).all(p);

  /*
   * Aged stock: held, nothing sold in the trailing year, AND first carried more
   * than 90 days ago. That last condition matters — brand-new season stock has
   * not had a chance to sell, and without it the metric labels fresh arrivals as
   * stale (two of the top "aged" lines were 2026 models that had literally never
   * been offered). created_at is the item record's createTime, a proxy for when
   * the shop first carried the SKU.
   *
   * Caveat for bikes: every colour/size is its own SKU, so SKU-level ageing
   * overstates the problem for a range that sells well in other variants. The
   * model/size cover table below is the fairer lens for bikes.
   */
  const AGED = `
    s.qoh > 0 AND s.value_avg_cost > 0
    AND COALESCE(t.gross_units, 0) <= 0
    AND (i.created_at IS NULL OR julianday(@anchor) - julianday(substr(i.created_at, 1, 10)) > 90)
  `;
  const deadStock = db.prepare(`
    WITH ${TRAILING}
    SELECT i.description AS product,
           COALESCE(c.top_level_name, 'Uncategorised') AS category,
           s.qoh, s.value_avg_cost AS stock_value,
           CAST(julianday(@anchor) - julianday(substr(i.created_at, 1, 10)) AS INT) AS days_carried
    FROM item_shops s
    LEFT JOIN items i ON i.item_id = s.item_id
    LEFT JOIN categories c ON c.category_id = i.category_id
    LEFT JOIN trailing t ON t.item_id = s.item_id
    WHERE ${AGED}
    ORDER BY s.value_avg_cost DESC
    LIMIT 25
  `).all(p);

  const deadTotal = db.prepare(`
    WITH ${TRAILING}
    SELECT COALESCE(SUM(s.value_avg_cost), 0) AS value, COUNT(*) AS skus
    FROM item_shops s
    LEFT JOIN items i ON i.item_id = s.item_id
    LEFT JOIN trailing t ON t.item_id = s.item_id
    WHERE ${AGED}
  `).get(p);

  // Stock too new to judge — reported separately so it is neither hidden nor
  // miscounted as stale.
  const newStock = db.prepare(`
    WITH ${TRAILING}
    SELECT COALESCE(SUM(s.value_avg_cost), 0) AS value, COUNT(*) AS skus
    FROM item_shops s
    LEFT JOIN items i ON i.item_id = s.item_id
    LEFT JOIN trailing t ON t.item_id = s.item_id
    WHERE s.qoh > 0 AND s.value_avg_cost > 0 AND COALESCE(t.gross_units, 0) <= 0
      AND i.created_at IS NOT NULL
      AND julianday(@anchor) - julianday(substr(i.created_at, 1, 10)) <= 90
  `).get(p);

  /* Cover by bike model and size — the pairing that answers "am I carrying stock
   * in sizes that do not sell?". Weeks of cover = units held / weekly sales rate. */
  const bikeCover = db.prepare(`
    WITH ${TRAILING}
    SELECT im.model || CASE WHEN im.size IS NOT NULL THEN ' ' || im.size ELSE '' END AS label,
           im.model AS model, im.size AS size,
           SUM(s.qoh) AS qoh,
           SUM(s.value_avg_cost) AS stock_value,
           COALESCE(SUM(t.net_units), 0) AS trailing_units,
           COALESCE(SUM(t.gross_units), 0) AS trailing_gross_units
    FROM item_shops s
    JOIN item_models im ON im.item_id = s.item_id
    LEFT JOIN trailing t ON t.item_id = s.item_id
    WHERE s.qoh > 0 AND im.kind = 'complete'
    GROUP BY label
    HAVING SUM(s.value_avg_cost) > 0
    ORDER BY stock_value DESC
    LIMIT 30
  `).all(p);

  res.json({ window: { from, to: anchor }, summary, byCategory, deadStock, deadTotal, newStock, bikeCover });
});

app.get('/api/meta', (req, res) => {
  const bounds = db.prepare(`
    SELECT substr(MIN(COALESCE(complete_time, sale_time)), 1, 10) AS min_date,
           substr(MAX(COALESCE(complete_time, sale_time)), 1, 10) AS max_date
    FROM sales WHERE completed = 1 AND voided = 0
  `).get();
  const counts = db.prepare(`
    SELECT (SELECT COUNT(*) FROM sales) AS sales,
           (SELECT COUNT(*) FROM sale_lines) AS sale_lines,
           (SELECT COUNT(*) FROM items) AS items
  `).get();
  const sync = getSyncState();
  res.json({
    ...bounds, counts, currency: 'ZAR',
    lastSyncedAt: sync?.last_synced_at ?? null,
    fullSyncCompletedAt: sync?.full_sync_completed_at ?? null,
    syncRunningSince: sync?.running_since ?? null
  });
});

// Proves the line-level revenue basis agrees with the sale-level basis, so the
// margin/category/product breakdowns can be trusted against the Lightspeed report.
app.get('/api/reconcile', (req, res) => {
  const params = periodParams(req);
  const row = db.prepare(`
    WITH ${VALID_SALE_CTE}
    SELECT (SELECT COALESCE(SUM(sale_revenue), 0) FROM valid) AS sale_level,
           (SELECT COALESCE(SUM(revenue), 0) FROM lines) AS line_level
  `).get(params);
  const diff = row.line_level - row.sale_level;
  res.json({
    ...row,
    difference: diff,
    difference_pct: row.sale_level ? (diff / row.sale_level) * 100 : 0
  });
});

const port = process.env.PORT || 3000;
app.listen(port, () => console.log(`Dashboard running at http://localhost:${port}`));
