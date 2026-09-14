import Database from 'better-sqlite3';

export const db = new Database('data.sqlite');

// The CLI sync and the server are separate processes on one file. WAL lets a
// reader (dashboard) run against a writer (sync); busy_timeout makes a brief
// lock collision wait rather than throw SQLITE_BUSY.
db.pragma('journal_mode = WAL');
db.pragma('busy_timeout = 15000');

// Migration: old sale_lines schema stored VAT-inclusive totals with no completed/voided
// filtering, which overstated revenue. Drop and rebuild with the corrected schema.
// fifo_cost/avg_cost were added later for gross-profit reporting.
const lineCols = db.prepare("PRAGMA table_info(sale_lines)").all().map(c => c.name);
const lineRebuilt = lineCols.length && !(lineCols.includes('calc_subtotal') && lineCols.includes('fifo_cost'));
if (lineRebuilt) {
  db.exec('DROP TABLE sale_lines');
}

// Migration: an earlier version of this file added sale_lines.source, guessing at
// a channel field that doesn't actually exist on the SaleLine API resource (it was
// always NULL). Drop it — the real field is sales.reference_number_source, below.
if (!lineRebuilt && lineCols.includes('source')) {
  db.exec('ALTER TABLE sale_lines DROP COLUMN source');
}

// Migration: items previously held only a description. category_id is needed for the
// category breakdown, so rebuild from the (fast) bulk Item endpoint.
const itemCols = db.prepare("PRAGMA table_info(items)").all().map(c => c.name);
if (itemCols.length && !itemCols.includes('category_id')) {
  db.exec('DROP TABLE items');
} else if (itemCols.length && !itemCols.includes('created_at')) {
  db.exec('ALTER TABLE items ADD COLUMN created_at TEXT');
}

// Migration: complete_time added for period-accurate reporting.
const saleCols = db.prepare("PRAGMA table_info(sales)").all().map(c => c.name);
if (saleCols.length && !saleCols.includes('complete_time')) {
  db.exec('ALTER TABLE sales ADD COLUMN complete_time TEXT');
}

// Migration: reference_number / reference_number_source added for the
// Shopify/Hubtiger/native sales-channel breakdown (see the sales table comment
// above). ADD COLUMN so existing synced history survives — but it's NULL on
// every already-synced row. Only a FULL sync (`npm run sync`, not the
// incremental refresh) re-fetches unchanged Sale rows and backfills it.
if (saleCols.length && !saleCols.includes('reference_number_source')) {
  db.exec('ALTER TABLE sales ADD COLUMN reference_number TEXT');
  db.exec('ALTER TABLE sales ADD COLUMN reference_number_source TEXT');
}

// Migration: frame size added to the derived classification.
const imCols = db.prepare("PRAGMA table_info(item_models)").all().map(c => c.name);
if (imCols.length && !imCols.includes('size')) {
  db.exec('DROP TABLE item_models');   // rebuilt by `npm run classify`, no API cost
}

// Migration: model/trim path segments added for the by-model breakdown.
const catCols = db.prepare("PRAGMA table_info(categories)").all().map(c => c.name);
if (catCols.length) {
  for (const col of ['discipline_name', 'model_name', 'variant_name']) {
    if (!catCols.includes(col)) db.exec(`ALTER TABLE categories ADD COLUMN ${col} TEXT`);
  }
}

// Migration: sync bookkeeping columns (see sync_state comment below).
const syncCols = db.prepare("PRAGMA table_info(sync_state)").all().map(c => c.name);
if (syncCols.length) {
  if (!syncCols.includes('full_sync_completed_at')) db.exec('ALTER TABLE sync_state ADD COLUMN full_sync_completed_at TEXT');
  if (!syncCols.includes('running_since')) db.exec('ALTER TABLE sync_state ADD COLUMN running_since TEXT');
  if (!syncCols.includes('running_pid')) db.exec('ALTER TABLE sync_state ADD COLUMN running_pid INTEGER');
}

db.exec(`
  CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    account_id TEXT,
    access_token TEXT,
    refresh_token TEXT,
    expires_at INTEGER
  );

  CREATE TABLE IF NOT EXISTS employees (
    employee_id TEXT PRIMARY KEY,
    first_name TEXT,
    last_name TEXT
  );

  CREATE TABLE IF NOT EXISTS shops (
    shop_id TEXT PRIMARY KEY,
    name TEXT
  );

  -- created_at is the item record's createTime, used as a proxy for "when did we
  -- first carry this?". Without it, brand-new stock that has simply not had time
  -- to sell is indistinguishable from genuinely stale stock, and gets wrongly
  -- reported as aged.
  CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY,
    description TEXT,
    system_sku TEXT,
    category_id TEXT,
    created_at TEXT
  );

  -- Lightspeed categories are a tree (nodeDepth 0 = top level). full_path_name is
  -- "BIKE/MTB/EPIC", so top_level_name is its first segment — that is the grouping
  -- the sales team reports on (Bike, Turbo, Equipment, Service Parts, ...).
  -- Path segments are denormalised into columns so grouping needs no tree walking:
  --   BIKE / MTB / EPIC 8 / COMP / L / 29
  --    [0]    [1]    [2]     [3]
  -- depth 2 is the MODEL (EPIC 8, LEVO SL, CHISEL) and depth 3 the TRIM
  -- (S-WORKS, EXPERT, COMP ALLOY) — the grain the sales team reports models on.
  CREATE TABLE IF NOT EXISTS categories (
    category_id TEXT PRIMARY KEY,
    name TEXT,
    parent_id TEXT,
    node_depth INTEGER,
    full_path_name TEXT,
    top_level_name TEXT,
    discipline_name TEXT,
    model_name TEXT,
    variant_name TEXT
  );

  -- One row per Sale. Revenue (ex-VAT, net of discount) = calc_subtotal - calc_discount.
  -- Only rows with completed = 1 AND voided = 0 represent finalized transactions.
  -- complete_time is when the sale actually closed and is the correct basis for
  -- period reporting; sale_time (timeStamp) is last-modified and drifts if a sale
  -- is edited later. Filter on COALESCE(complete_time, sale_time).
  -- reference_number_source is Lightspeed's own field for "name of the external
  -- system for referenceNumber" (Sale resource, per developers.lightspeedhq.com) —
  -- e.g. Shopify or Hubtiger when a sale was pushed in via that integration, NULL
  -- for a native POS sale. This is a Sale-level field, NOT a SaleLine one — an
  -- earlier version of this schema wrongly guessed at a SaleLine.source that does
  -- not exist on the API and was always empty.
  CREATE TABLE IF NOT EXISTS sales (
    sale_id TEXT PRIMARY KEY,
    employee_id TEXT,
    shop_id TEXT,
    sale_time TEXT,
    complete_time TEXT,
    completed INTEGER,
    voided INTEGER,
    calc_subtotal REAL,
    calc_discount REAL,
    reference_number TEXT,
    reference_number_source TEXT
  );

  -- One row per SaleLine. Line revenue (ex-VAT, net of discount) =
  -- calc_subtotal - calc_line_discount - calc_transaction_discount.
  -- Always filter via JOIN sales ON sale_id WHERE completed = 1 AND voided = 0.
  -- fifo_cost/avg_cost are PER UNIT and captured at time of sale, so line COGS is
  -- quantity * cost. Using the item's current cost would misstate historical margin.
  CREATE TABLE IF NOT EXISTS sale_lines (
    sale_line_id TEXT PRIMARY KEY,
    sale_id TEXT,
    item_id TEXT,
    employee_id TEXT,
    shop_id TEXT,
    quantity REAL,
    calc_subtotal REAL,
    calc_line_discount REAL,
    calc_transaction_discount REAL,
    fifo_cost REAL,
    avg_cost REAL
  );

  -- last_synced_at is the incremental watermark. It may ONLY advance on a run that
  -- completed, and full_sync_completed_at gates incremental mode: without a
  -- completed full sync there is no trustworthy baseline to increment from, and
  -- advancing the watermark would silently strand unsynced history.
  -- running_since is a cross-process lock (the CLI and the server both sync).
  -- Derived classification, kept separate from synced item data so it can be
  -- rebuilt from model-rules.json at any time without re-fetching from Lightspeed.
  -- kind distinguishes complete bikes from framesets/frames/build kits/rentals.
  CREATE TABLE IF NOT EXISTS item_models (
    item_id TEXT PRIMARY KEY,
    model TEXT,
    trim TEXT,
    size TEXT,
    size_system TEXT,
    kind TEXT,
    source TEXT,
    matched_text TEXT,
    tree_model TEXT,
    agrees_with_tree INTEGER
  );

  /*
   * Current stock position, one row per item.
   *
   * IMPORTANT: Lightspeed's ItemShop returns TWO rows per item — shopID 0 and
   * shopID 1 — carrying the SAME qoh and the same value. Shop 0 is the aggregate
   * record (it is not a real shop; it does not appear in the shops table, and it is the
   * row that carries averageCost). Summing both double-counts stock exactly 2x,
   * so the sync stores only the aggregate row.
   *
   * value_avg_cost / value_fifo are Lightspeed's OWN computed stock values, used
   * in preference to qoh x cost so the figures tie to the shop's own reports.
   */
  CREATE TABLE IF NOT EXISTS item_shops (
    item_id TEXT PRIMARY KEY,
    shop_id TEXT,
    qoh REAL,
    avg_cost REAL,
    value_avg_cost REAL,
    value_fifo REAL,
    reorder_point REAL,
    on_layaway REAL,
    on_workorder REAL,
    on_special_order REAL,
    updated_at TEXT
  );

  /*
   * Daily stock history, aggregated to category level (~13 rows/day rather than
   * ~31k) so that TRUE average inventory becomes computable over time. The API
   * only ever exposes a current snapshot, so stock turn today can only use a
   * point-in-time denominator; this table is what makes a real average possible
   * for future periods.
   */
  CREATE TABLE IF NOT EXISTS stock_snapshots (
    snapshot_date TEXT,
    category TEXT,
    qoh REAL,
    value_avg_cost REAL,
    PRIMARY KEY (snapshot_date, category)
  );

  CREATE TABLE IF NOT EXISTS sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_synced_at TEXT,
    full_sync_completed_at TEXT,
    running_since TEXT,
    running_pid INTEGER
  );

  CREATE INDEX IF NOT EXISTS idx_sale_lines_sale ON sale_lines(sale_id);
  CREATE INDEX IF NOT EXISTS idx_sale_lines_item ON sale_lines(item_id);
  CREATE INDEX IF NOT EXISTS idx_sales_time ON sales(sale_time);
  CREATE INDEX IF NOT EXISTS idx_sales_valid ON sales(completed, voided);
  CREATE INDEX IF NOT EXISTS idx_item_shops_qoh ON item_shops(qoh);
`);

export function getSyncState() {
  return db.prepare('SELECT * FROM sync_state WHERE id = 1').get() ?? null;
}

export function getLastSyncedAt() {
  return getSyncState()?.last_synced_at ?? null;
}

// Marks a run finished: advances the incremental watermark, and for a full run
// records the baseline that makes incremental mode legitimate.
export function completeSync(startedAt, { full }) {
  db.prepare(`
    INSERT INTO sync_state (id, last_synced_at, full_sync_completed_at, running_since)
    VALUES (1, @started, CASE WHEN @full = 1 THEN @started ELSE NULL END, NULL)
    ON CONFLICT(id) DO UPDATE SET
      last_synced_at = @started,
      full_sync_completed_at = CASE WHEN @full = 1 THEN @started ELSE sync_state.full_sync_completed_at END,
      running_since = NULL
  `).run({ started: startedAt, full: full ? 1 : 0 });
}

// Fallback only — used when a lock predates PID tracking (running_pid is NULL).
// Real staleness detection is the PID liveness check below, which is exact and
// doesn't need a guessed timeout: a full first sync legitimately runs for hours,
// but an incremental one that dies leaves a lock that would otherwise silently
// block every "Re-sync" click for up to this long.
const LOCK_STALE_MS = 6 * 60 * 60 * 1000;

// The CLI sync and the server both run on this machine, so a PID from either can
// be probed with kill(pid, 0): throws ESRCH if nothing with that PID exists,
// EPERM if it exists but is owned by someone else (still alive, from our POV).
function isProcessAlive(pid) {
  if (!pid) return false;
  try { process.kill(pid, 0); return true; }
  catch (err) { return err.code === 'EPERM'; }
}

// Cross-process advisory lock. Returns false only when another sync is
// VERIFIABLY still running — a dead holder's lock is reclaimed immediately
// rather than waiting out a timeout, so an interrupted sync can never leave
// "Re-sync" silently doing nothing for hours.
export function acquireSyncLock(nowIso) {
  const row = getSyncState();
  if (row?.running_since) {
    if (row.running_pid) {
      if (isProcessAlive(row.running_pid)) return false;
      // Holder's PID is confirmed dead — fall through and reclaim.
    } else if (Date.now() - Date.parse(row.running_since) < LOCK_STALE_MS) {
      return false;   // legacy lock with no PID recorded; only the timeout applies
    }
  }
  db.prepare(`
    INSERT INTO sync_state (id, running_since, running_pid) VALUES (1, @now, @pid)
    ON CONFLICT(id) DO UPDATE SET running_since = @now, running_pid = @pid
  `).run({ now: nowIso, pid: process.pid });
  return true;
}

export function releaseSyncLock() {
  db.prepare('UPDATE sync_state SET running_since = NULL, running_pid = NULL WHERE id = 1').run();
}

export function getTokens() {
  return db.prepare('SELECT * FROM tokens WHERE id = 1').get();
}

export function saveTokens({ accountId, accessToken, refreshToken, expiresAt }) {
  db.prepare(`
    INSERT INTO tokens (id, account_id, access_token, refresh_token, expires_at)
    VALUES (1, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
      account_id = excluded.account_id,
      access_token = excluded.access_token,
      refresh_token = excluded.refresh_token,
      expires_at = excluded.expires_at
  `).run(accountId, accessToken, refreshToken, expiresAt);
}
