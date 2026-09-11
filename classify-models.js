/*
 * Rebuild item_models from item descriptions + model-rules.json.
 *
 * Pure local computation — no Lightspeed calls — so it is safe to run any time,
 * including while a sync is in flight, and cheap to iterate on when the rules
 * change. Run with: npm run classify
 */
import { db } from './db.js';
import { buildModelMatcher, classifyItem, deriveSize } from './models.js';

const treeModels = db.prepare(`
  SELECT DISTINCT model_name FROM categories
  WHERE model_name IS NOT NULL AND top_level_name IN ('BIKE','TURBO')
`).all().map(r => r.model_name);

const matcher = buildModelMatcher(treeModels);
console.log(`Vocabulary: ${matcher.models.size} models (${treeModels.length} from the category tree + extras from model-rules.json)`);

/*
 * Which items count as bikes — the division of labour that survived auditing:
 *
 *   The category tree's TOP LEVEL is reliable (a seatpost is filed under SERVICE
 *   PARTS, a jersey under APPAREL). Its MODEL/TRIM depth is not (79% of BIKE
 *   items have no trim node, and it contains the typo "EPXERT").
 *
 * So the top level gates WHAT an item is, and the description supplies WHICH
 * model. An earlier version classified anything whose description mentioned a
 * model, which wrongly booked "SW ROUBAIX 32.6 RD COLLAR" (a seatpost),
 * "DEMO PRO JERSEY" (apparel) and "BLT MY16 LEVO BOLT KIT" (a bolt kit) as bike
 * sales — a part FOR a model is not a sale OF that model.
 *
 * Uncategorised items are the one place the tree cannot help, so they are
 * admitted only when the model name STARTS the description ("TARMAC SL7 EXPERT
 * …"), which distinguishes the bike itself from a part that merely names it.
 */
const items = db.prepare(`
  SELECT i.item_id, i.description, i.category_id,
         c.category_id AS cat_found, c.top_level_name,
         c.model_name AS tree_model, c.variant_name AS tree_trim,
         CASE WHEN c.node_depth >= 4
              THEN TRIM(substr(c.full_path_name,
                   length(c.top_level_name || '/' || c.discipline_name || '/' || c.model_name || '/' || c.variant_name || '/') + 1))
         END AS tree_size
  FROM items i
  LEFT JOIN categories c ON c.category_id = i.category_id
`).all();

const upsert = db.prepare(`
  INSERT INTO item_models (item_id, model, trim, size, size_system, kind, source, matched_text, tree_model, agrees_with_tree)
  VALUES (@item_id, @model, @trim, @size, @size_system, @kind, @source, @matched_text, @tree_model, @agrees)
  ON CONFLICT(item_id) DO UPDATE SET
    model = excluded.model, trim = excluded.trim, size = excluded.size,
    size_system = excluded.size_system, kind = excluded.kind,
    source = excluded.source, matched_text = excluded.matched_text,
    tree_model = excluded.tree_model, agrees_with_tree = excluded.agrees_with_tree
`);

let classified = 0, fromDescription = 0, fromCategory = 0, disagreements = 0;
let skippedNonBikeCategory = 0, admittedUncategorised = 0;
const kindCounts = {}, sizeCounts = {};
let missingSize = 0;

const tx = db.transaction(rows => {
  db.prepare('DELETE FROM item_models').run();
  for (const r of rows) {
    const isBikeCategory = r.top_level_name === 'BIKE' || r.top_level_name === 'TURBO';
    const isUncategorised = !r.cat_found;

    // Filed under a non-bike top level (SERVICE PARTS, APPAREL, EQUIPMENT…): the
    // tree is trustworthy at this level, so never book it as a bike sale.
    if (!isBikeCategory && !isUncategorised) { skippedNonBikeCategory++; continue; }

    const res = classifyItem(r.description, r.tree_model, r.tree_trim, matcher);
    if (!res.model) continue;

    // Uncategorised: require the model to START the description.
    if (isUncategorised) {
      if (res.source !== 'description' || res.matchIndex !== 0) continue;
      admittedUncategorised++;
    }

    const sz = deriveSize(r.description, matcher, r.tree_size);
    if (sz.size) sizeCounts[sz.system] = (sizeCounts[sz.system] || 0) + 1; else missingSize++;
    upsert.run({
      item_id: r.item_id, model: res.model, trim: res.trim,
      size: sz.size, size_system: sz.system, kind: res.kind,
      source: res.source, matched_text: res.matchedText, tree_model: res.treeModel,
      agrees: res.agreesWithTree === null ? null : (res.agreesWithTree ? 1 : 0)
    });
    classified++;
    if (res.source === 'description') fromDescription++; else fromCategory++;
    if (res.agreesWithTree === false) disagreements++;
    kindCounts[res.kind] = (kindCounts[res.kind] || 0) + 1;
  }
});
tx(items);

console.log(`Classified ${classified} bike items of ${items.length} total.`);
console.log(`  model from description: ${fromDescription}`);
console.log(`  model from category (description had none): ${fromCategory}`);
console.log(`  admitted from uncategorised (model leads description): ${admittedUncategorised}`);
console.log(`  skipped — filed under a non-bike top level: ${skippedNonBikeCategory}`);
console.log(`  description disagrees with tree's model: ${disagreements}`);
console.log('  by kind:', kindCounts);
console.log('  size system:', sizeCounts, '| no size derived:', missingSize);
