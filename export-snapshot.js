/*
 * Builds snapshot.sqlite — the ONLY data that is meant to leave this machine.
 * It gets committed to the (private) GitHub repo and read by the Streamlit
 * Cloud app, which has no access to this Mac, the Lightspeed OAuth session,
 * or the live data.sqlite.
 *
 * What is deliberately EXCLUDED, and why:
 *   - `tokens`      Lightspeed OAuth client/access/refresh credentials. Must
 *                   never leave this machine under any circumstances.
 *   - `employees`   Staff first/last names. The app does not report by rep
 *                   (removed at the client's request), so there is no reason
 *                   for names to exist in a shared artifact at all. employee_id
 *                   remains on sales/sale_lines as a bare, unnamed foreign key —
 *                   harmless, and there is no employees table in the snapshot
 *                   to resolve it against even if someone tried.
 *   - `sync_state`  Purely operational (watermark, PID, lock state) — not
 *                   reporting data, and a PID is meaningless off this machine.
 *
 * Budget and Adjustments data (normally read live from the two .xlsx files by
 * sheets.js) are folded into the snapshot as plain tables, so the Streamlit
 * app needs nothing but sqlite3 — no spreadsheet parsing, no access to files
 * that are themselves gitignored.
 *
 * Run this AFTER `npm run sync:incremental` (or npm run sync), then commit
 * and push snapshot.sqlite for the shared dashboard to pick up the refresh.
 */
import Database from 'better-sqlite3';
import { existsSync, unlinkSync, statSync } from 'node:fs';
import { readAdjustments, readBudget } from './sheets.js';

const SRC = 'data.sqlite';
const OUT = 'snapshot.sqlite';

const SAFE_TABLES = [
  'shops', 'sales', 'items', 'categories', 'sale_lines',
  'item_models', 'item_shops', 'stock_snapshots'
];

for (const f of [OUT, OUT + '-shm', OUT + '-wal']) {
  if (existsSync(f)) unlinkSync(f);
}

// NOT opened readonly: better-sqlite3's readonly flag applies to the whole
// connection, including any database ATTACHed afterward — which would block
// writing to the newly attached snapshot file. main (data.sqlite) is still
// only ever read from here; nothing in this script writes back to it.
const src = new Database(SRC);
src.exec(`ATTACH DATABASE '${OUT}' AS snap`);

for (const table of SAFE_TABLES) {
  src.exec(`CREATE TABLE snap.${table} AS SELECT * FROM main.${table}`);
}

// Same indexes the main app relies on, so the Streamlit queries (ported
// from server.js) perform similarly against the snapshot.
src.exec(`
  CREATE INDEX snap.idx_sale_lines_sale ON sale_lines(sale_id);
  CREATE INDEX snap.idx_sale_lines_item ON sale_lines(item_id);
  CREATE INDEX snap.idx_sales_time ON sales(sale_time);
  CREATE INDEX snap.idx_sales_valid ON sales(completed, voided);
`);

src.exec(`
  CREATE TABLE snap.budget (month TEXT, category TEXT, amount REAL);
  CREATE TABLE snap.adjustments (date TEXT, description TEXT, qty REAL, revenue REAL, cost REAL);
  CREATE TABLE snap.snapshot_meta (key TEXT PRIMARY KEY, value TEXT);
`);

const { rows: budgetRows, missing: budgetMissing } = await readBudget();
const insBudget = src.prepare('INSERT INTO snap.budget (month, category, amount) VALUES (?, ?, ?)');
for (const r of budgetRows) insBudget.run(r.month, r.category, r.amount);

const { rows: adjRows, missing: adjMissing } = await readAdjustments();
const insAdj = src.prepare('INSERT INTO snap.adjustments (date, description, qty, revenue, cost) VALUES (?, ?, ?, ?, ?)');
for (const r of adjRows) insAdj.run(r.date, r.description, r.qty, r.revenue, r.cost);

const insMeta = src.prepare('INSERT INTO snap.snapshot_meta (key, value) VALUES (?, ?)');
insMeta.run('exported_at', new Date().toISOString());
insMeta.run('budget_missing', budgetMissing ? '1' : '0');
insMeta.run('adjustments_missing', adjMissing ? '1' : '0');

src.exec('DETACH DATABASE snap');
src.close();

const size = statSync(OUT).size;
console.log(`Wrote ${OUT} (${(size / 1024 / 1024).toFixed(1)} MB)`);
console.log(`  budget rows: ${budgetRows.length}${budgetMissing ? ' (sheet not found)' : ''}`);
console.log(`  adjustment rows: ${adjRows.length}${adjMissing ? ' (sheet not found)' : ''}`);
console.log('Excluded from snapshot (by design): tokens, employees, sync_state.');
console.log('Next: git add snapshot.sqlite && git commit && git push');
