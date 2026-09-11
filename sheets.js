/*
 * Reads the two operator-maintained spreadsheets that sit alongside the app:
 *
 *   Adjustments.xlsx            — sales the store made OUTSIDE Lightspeed.
 *   Specialized Budget Data.xlsx — monthly revenue budget per category.
 *
 * Both are read LIVE (re-read whenever the file's mtime changes) so the shop can
 * keep editing them in Excel and see the dashboard update on refresh. The files
 * are never written to: dates arrive as Excel serial numbers because that is how
 * Excel natively stores a date, so they are converted here rather than by
 * altering the workbook, which keeps the sheets fully usable in Excel.
 *
 * Both readers are tolerant: a missing file is not an error (the dashboard simply
 * reports nothing from it), and dates may be Excel serials, real dates, or text.
 */
import ExcelJS from 'exceljs';
import { statSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
export const ADJUSTMENTS_PATH = join(here, 'Adjustments.xlsx');
export const BUDGET_PATH = join(here, 'Specialized Budget Data.xlsx');

// Excel's day 0 is 1899-12-30 (its deliberate 1900 leap-year bug included).
const EXCEL_EPOCH_UTC = Date.UTC(1899, 11, 30);

function excelSerialToISO(n) {
  const ms = EXCEL_EPOCH_UTC + Math.round(Number(n)) * 86400000;
  return new Date(ms).toISOString().slice(0, 10);
}

/** Accepts an Excel serial, a JS Date, or text in ISO / DD-MM-YYYY / DD/MM/YYYY. */
export function normaliseDate(v) {
  if (v == null || v === '') return null;
  if (v instanceof Date) return v.toISOString().slice(0, 10);
  if (typeof v === 'number') return Number.isFinite(v) ? excelSerialToISO(v) : null;
  const s = String(v).trim();
  if (/^\d+(\.\d+)?$/.test(s)) return excelSerialToISO(Number(s));
  let m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
  if (m) return `${m[1]}-${m[2]}-${m[3]}`;
  m = /^(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{4})$/.exec(s);   // DD/MM/YYYY (UK order)
  if (m) return `${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`;
  const d = new Date(s);
  return isNaN(d) ? null : d.toISOString().slice(0, 10);
}

const num = v => {
  if (v == null || v === '') return 0;
  if (typeof v === 'object' && v.result != null) return Number(v.result) || 0;  // formula cell
  const n = Number(String(v).replace(/[^0-9.\-]/g, ''));
  return Number.isFinite(n) ? n : 0;
};
const text = v => (v == null ? '' : (typeof v === 'object' && v.richText
  ? v.richText.map(t => t.text).join('') : String(v))).trim();

// Cache keyed on mtime so an edit in Excel is picked up without a restart.
const cache = new Map();
async function loadSheet(path, parse) {
  if (!existsSync(path)) return { missing: true, rows: [], mtime: null };
  const mtime = statSync(path).mtimeMs;
  const hit = cache.get(path);
  if (hit && hit.mtime === mtime) return hit;
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.readFile(path);
  const ws = wb.worksheets[0];
  const out = { missing: false, rows: parse(ws), mtime, readAt: new Date().toISOString() };
  cache.set(path, out);
  return out;
}

/** Header-name lookup, so column ORDER in the sheet is free to change. */
function headerIndex(ws) {
  const idx = {};
  ws.getRow(1).eachCell((cell, col) => { idx[text(cell.value).toUpperCase()] = col; });
  return idx;
}

/*
 * Adjustments. Subtotal is ex-VAT (Subtotal x 1.15 = Total), which is already the
 * dashboard's revenue basis, so revenue = Subtotal - Discounts and COGS = Cost.
 * Negative rows are the sheet's own corrections/refunds and are kept as-is so
 * they net out exactly as they do in Lightspeed.
 */
export async function readAdjustments() {
  const res = await loadSheet(ADJUSTMENTS_PATH, ws => {
    const h = headerIndex(ws);
    const col = name => h[name] ?? null;
    const rows = [];
    ws.eachRow((row, n) => {
      if (n === 1) return;
      const date = normaliseDate(row.getCell(col('DATE')).value);
      const description = text(row.getCell(col('DESCRIPTION')).value);
      if (!date && !description) return;                       // blank spacer row
      const subtotal = num(row.getCell(col('SUBTOTAL')).value);
      const discounts = col('DISCOUNTS') ? num(row.getCell(col('DISCOUNTS')).value) : 0;
      rows.push({
        id: col('ID') ? text(row.getCell(col('ID')).value) : '',
        date, description,
        qty: num(row.getCell(col('QTY')).value),
        revenue: subtotal - discounts,
        discounts,
        total_incl_vat: col('TOTAL') ? num(row.getCell(col('TOTAL')).value) : 0,
        cost: num(row.getCell(col('COST')).value),
        row_number: n
      });
    });
    return rows;
  });
  return res;
}

/*
 * Budget: one row per month, one column per budgeted category. Only a subset of
 * the Lightspeed categories is budgeted, so the category list is taken from this
 * sheet's own headers — the comparison must never invent a budget of zero for an
 * unbudgeted category, which would make actuals look like pure overspend.
 */
export async function readBudget() {
  const res = await loadSheet(BUDGET_PATH, ws => {
    const headerRow = ws.getRow(1);
    const cols = [];
    headerRow.eachCell((cell, col) => {
      const name = text(cell.value).toUpperCase();
      if (col === 1 || name === 'DATE' || !name) return;
      cols.push({ col, category: name });
    });
    const rows = [];
    ws.eachRow((row, n) => {
      if (n === 1) return;
      const date = normaliseDate(row.getCell(1).value);
      if (!date) return;
      for (const c of cols) {
        const amount = num(row.getCell(c.col).value);
        rows.push({ month: date.slice(0, 7), date, category: c.category, amount });
      }
    });
    return rows;
  });
  return res;
}

export async function budgetCategories() {
  const { rows } = await readBudget();
  return [...new Set(rows.map(r => r.category))].sort();
}
