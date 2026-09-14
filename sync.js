import 'dotenv/config';
import { db, getSyncState, completeSync, acquireSyncLock, releaseSyncLock } from './db.js';
import { lsFetchAll } from './lightspeed.js';
import { categorySegments } from './categories.js';

export async function runSync({ incremental = false } = {}) {
  const startedAt = new Date().toISOString();
  const state = getSyncState();

  // Incremental mode is only legitimate on top of a completed full sync. Without
  // that baseline, syncing "since the watermark" would leave older history
  // permanently unsynced while the watermark marched forward.
  let since = null;
  if (incremental) {
    if (!state?.full_sync_completed_at) {
      console.warn('No completed full sync on record — running a FULL sync instead of incremental.');
    } else {
      since = state.last_synced_at ?? null;
    }
  }
  const isFull = since === null;

  if (!acquireSyncLock(startedAt)) {
    // acquireSyncLock already confirmed the holder's process is genuinely alive
    // (checked by PID, not by how long ago it started), so this is a real
    // concurrent sync, not a stuck lock — the right move is just to wait.
    throw new Error(`A sync is already running (started ${state?.running_since}). Wait for it to finish, then try again.`);
  }
  try {
    await syncAll({ since, startedAt, isFull });
  } finally {
    releaseSyncLock();
  }
}

// Every resource here carries its own timeStamp, bumped by Lightspeed on any
// change — including ItemShop, whose timeStamp advances on every sale that
// moves qoh. So `since` (the same incremental watermark used for Sale/SaleLine)
// works uniformly everywhere: an unmodified record is simply never re-fetched,
// and its existing row in SQLite is left as-is (upserts only touch fetched
// rows), which is the same safe pattern already proven for Sale/SaleLine.
function withSince(path, since) {
  if (!since) return path;
  const sep = path.includes('?') ? '&' : '?';
  return `${path}${sep}timeStamp=%3E,${encodeURIComponent(since)}`;
}

async function syncAll({ since, startedAt, isFull }) {

  // Lightspeed omits archived records from list endpoints by default. Historical
  // sales reference archived employees (former staff) and archived items
  // (discontinued models), so both passes are required or those sales lose their
  // rep name / product name / category entirely.
  const employees = [
    ...await lsFetchAll(withSince('/Employee.json', since), 'Employee'),
    ...await lsFetchAll(withSince('/Employee.json?archived=true', since), 'Employee')
  ];
  const upsertEmployee = db.prepare(`
    INSERT INTO employees (employee_id, first_name, last_name) VALUES (?, ?, ?)
    ON CONFLICT(employee_id) DO UPDATE SET first_name = excluded.first_name, last_name = excluded.last_name
  `);
  for (const e of employees) upsertEmployee.run(e.employeeID, e.firstName, e.lastName);
  console.log(`Employees ${isFull ? '' : 'changed '}(incl. archived): ${employees.length}`);

  const shops = await lsFetchAll('/Shop.json', 'Shop');
  const upsertShop = db.prepare(`
    INSERT INTO shops (shop_id, name) VALUES (?, ?)
    ON CONFLICT(shop_id) DO UPDATE SET name = excluded.name
  `);
  for (const s of shops) upsertShop.run(s.shopID, s.name);
  console.log(`Shops: ${shops.length}`);

  // Categories are a tree; top_level_name (first segment of fullPathName) is the
  // reporting grouping the sales team uses.
  const categories = await lsFetchAll(withSince('/Category.json', since), 'Category', 100);
  const upsertCategory = db.prepare(`
    INSERT INTO categories (category_id, name, parent_id, node_depth, full_path_name,
                            top_level_name, discipline_name, model_name, variant_name)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(category_id) DO UPDATE SET
      name = excluded.name, parent_id = excluded.parent_id, node_depth = excluded.node_depth,
      full_path_name = excluded.full_path_name, top_level_name = excluded.top_level_name,
      discipline_name = excluded.discipline_name, model_name = excluded.model_name,
      variant_name = excluded.variant_name
  `);
  for (const c of categories) {
    const s = categorySegments(c.fullPathName, c.name);
    upsertCategory.run(
      c.categoryID, c.name, c.parentID, Number(c.nodeDepth), c.fullPathName,
      s.topLevelName, s.disciplineName, s.modelName, s.variantName
    );
  }
  console.log(`Categories ${isFull ? '' : 'changed '}: ${categories.length}`);

  // Bulk Item pull (~100/page) replaces per-item lookups — orders of magnitude faster
  // and carries categoryID, which per-item description-only lookups did not give us.
  // On incremental runs this is filtered to changed items too — previously this
  // re-fetched the ENTIRE catalogue (~47k items, hundreds of requests) on every
  // sync regardless of what changed, which was most of why a "quick" re-sync
  // routinely took several minutes.
  const upsertItem = db.prepare(`
    INSERT INTO items (item_id, description, system_sku, category_id, created_at) VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(item_id) DO UPDATE SET
      description = excluded.description, system_sku = excluded.system_sku,
      category_id = excluded.category_id, created_at = excluded.created_at
  `);
  const onItemPage = (batch, totalSoFar) => {
    for (const it of batch) upsertItem.run(it.itemID, it.description, it.systemSku, it.categoryID, it.createTime ?? null);
    if (totalSoFar % 2000 < 100) console.log(`Items synced so far: ${totalSoFar}`);
  };
  const items = await lsFetchAll(withSince('/Item.json', since), 'Item', 100, onItemPage);
  const archivedItems = await lsFetchAll(withSince('/Item.json?archived=true', since), 'Item', 100, onItemPage);
  console.log(`Items ${isFull ? '' : 'changed '}: ${items.length} active + ${archivedItems.length} archived`);

  // Sale carries completed/voided status — required to know which sales are real, finalized
  // revenue vs. open carts, unconverted quotes, or voided transactions.
  const upsertSale = db.prepare(`
    INSERT INTO sales (sale_id, employee_id, shop_id, sale_time, complete_time, completed, voided, calc_subtotal, calc_discount)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(sale_id) DO UPDATE SET
      employee_id = excluded.employee_id, shop_id = excluded.shop_id, sale_time = excluded.sale_time,
      complete_time = excluded.complete_time, completed = excluded.completed, voided = excluded.voided,
      calc_subtotal = excluded.calc_subtotal, calc_discount = excluded.calc_discount
  `);
  const sales = await lsFetchAll(withSince('/Sale.json', since), 'Sale', 100, (batch, totalSoFar) => {
    for (const s of batch) {
      upsertSale.run(
        s.saleID, s.employeeID ?? null, s.shopID ?? null, s.timeStamp ?? null, s.completeTime ?? null,
        s.completed === 'true' ? 1 : 0, s.voided === 'true' ? 1 : 0,
        Number(s.calcSubtotal), Number(s.calcDiscount)
      );
    }
    if (totalSoFar % 2000 < 100) console.log(`Sales synced so far: ${totalSoFar}`);
  });

  // SaleLine revenue (ex-VAT, net of discount) = calc_subtotal - calc_line_discount - calc_transaction_discount.
  // Validity (completed/voided) is determined via JOIN to sales, not stored redundantly here.
  const upsertLine = db.prepare(`
    INSERT INTO sale_lines (sale_line_id, sale_id, item_id, employee_id, shop_id, quantity, calc_subtotal, calc_line_discount, calc_transaction_discount, fifo_cost, avg_cost, source)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(sale_line_id) DO UPDATE SET
      sale_id = excluded.sale_id, item_id = excluded.item_id, employee_id = excluded.employee_id,
      shop_id = excluded.shop_id, quantity = excluded.quantity, calc_subtotal = excluded.calc_subtotal,
      calc_line_discount = excluded.calc_line_discount, calc_transaction_discount = excluded.calc_transaction_discount,
      fifo_cost = excluded.fifo_cost, avg_cost = excluded.avg_cost, source = excluded.source
  `);
  const saleLines = await lsFetchAll(withSince('/SaleLine.json', since), 'SaleLine', 100, (batch, totalSoFar) => {
    for (const l of batch) {
      upsertLine.run(
        l.saleLineID, l.saleID, l.itemID, l.employeeID ?? null, l.shopID,
        Number(l.unitQuantity), Number(l.calcSubtotal), Number(l.calcLineDiscount), Number(l.calcTransactionDiscount),
        Number(l.fifoCost), Number(l.avgCost), l.source || null
      );
    }
    if (totalSoFar % 2000 < 100) console.log(`Sale lines synced so far: ${totalSoFar}`);
  });

  /*
   * Stock position. Only the aggregate ItemShop row (shopID 0) is stored — see the
   * item_shops comment in db.js: the per-shop row duplicates the same qoh, so
   * keeping both would double stock value.
   */
  const upsertStock = db.prepare(`
    INSERT INTO item_shops (item_id, shop_id, qoh, avg_cost, value_avg_cost, value_fifo,
                            reorder_point, on_layaway, on_workorder, on_special_order, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(item_id) DO UPDATE SET
      shop_id = excluded.shop_id, qoh = excluded.qoh, avg_cost = excluded.avg_cost,
      value_avg_cost = excluded.value_avg_cost, value_fifo = excluded.value_fifo,
      reorder_point = excluded.reorder_point, on_layaway = excluded.on_layaway,
      on_workorder = excluded.on_workorder, on_special_order = excluded.on_special_order,
      updated_at = excluded.updated_at
  `);
  const stock = await lsFetchAll(withSince('/ItemShop.json?shopID=0', since), 'ItemShop', 100, (batch, totalSoFar) => {
    for (const r of batch) {
      upsertStock.run(
        r.itemID, r.shopID, Number(r.qoh), Number(r.averageCost),
        Number(r.totalValueAvgCost), Number(r.totalValueFifo), Number(r.reorderPoint),
        Number(r.onLayaway), Number(r.onWorkorder), Number(r.onSpecialOrder), startedAt
      );
    }
    if (totalSoFar % 5000 < 100) console.log(`Stock rows synced so far: ${totalSoFar}`);
  });
  console.log(`Stock ${isFull ? '' : 'changed '}: ${stock.length} item positions`);

  // Append today's category-level stock snapshot so average inventory (and thus a
  // true stock turn) becomes computable for future periods.
  db.prepare(`
    INSERT INTO stock_snapshots (snapshot_date, category, qoh, value_avg_cost)
    SELECT date('now'), COALESCE(c.top_level_name, 'Uncategorised'),
           SUM(s.qoh), SUM(s.value_avg_cost)
    FROM item_shops s
    LEFT JOIN items i ON i.item_id = s.item_id
    LEFT JOIN categories c ON c.category_id = i.category_id
    -- qoh > 0, matching what the dashboard reports as stock on hand. Including
    -- negative positions here would make this history disagree with the headline
    -- figure and quietly understate the average inventory it will later feed.
    WHERE s.qoh > 0
    GROUP BY 2
    ON CONFLICT(snapshot_date, category) DO UPDATE SET
      qoh = excluded.qoh, value_avg_cost = excluded.value_avg_cost
  `).run();

  completeSync(startedAt, { full: isFull });
  console.log(`Done (${isFull ? 'full' : 'incremental'}). Employees ${employees.length}, shops ${shops.length}, categories ${categories.length}, items ${items.length + archivedItems.length}, sales ${sales.length}, sale lines ${saleLines.length}, stock ${stock.length}.`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const incremental = process.argv.includes('--incremental');
  runSync({ incremental }).catch(err => { console.error(err); process.exit(1); });
}
