/*
 * Infer bike MODEL and TRIM from an item's description.
 *
 * Why not the category tree alone: it is hand-maintained, so it has gaps (79% of
 * BIKE items carry no trim node, 22% no model node) and outright errors (it
 * contains the typo "EPXERT"). Descriptions are entered per product and proved
 * both more complete and more consistent. So:
 *
 *   - VOCABULARY comes from the category tree (self-updating as the range turns
 *     over) plus extraModels in model-rules.json for ranges the tree has dropped.
 *   - ASSIGNMENT comes from the description, so a mis-categorised item still lands
 *     under the right model.
 *   - The tree value is retained as a cross-check; disagreements are reported by
 *     /api/model-audit rather than silently resolved.
 *
 * KIND matters as much as model: a frameset, bare frame, build kit or day rental
 * is not a complete bike. Counting them as model sales would overstate a model's
 * revenue badly, so kind is derived and reported separately.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
export const rules = JSON.parse(readFileSync(join(here, 'model-rules.json'), 'utf8'));

const norm = s => (s || '').toUpperCase().replace(/[–—]/g, '-').replace(/\s+/g, ' ').trim();

// Word-boundary match so "PRO" does not match inside "PROBAR", and "SW" does not
// match inside "SWEAT". Model/trim tokens can contain spaces, dots and hyphens.
function containsToken(haystack, token) {
  const esc = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`(^|[^A-Z0-9])${esc}([^A-Z0-9]|$)`).test(haystack);
}

function tokenIndex(haystack, token) {
  const esc = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const m = new RegExp(`(^|[^A-Z0-9])(${esc})([^A-Z0-9]|$)`).exec(haystack);
  return m ? m.index : -1;
}

/**
 * Build the matcher once, from the category-tree vocabulary plus the rules file.
 * treeModels: array of model names harvested from category depth 2.
 */
export function buildModelMatcher(treeModels = []) {
  const aliasEntries = Object.entries(rules.modelAliases || {});
  const canonical = new Set([...treeModels, ...(rules.extraModels || [])].map(norm).filter(Boolean));
  for (const [, target] of aliasEntries) canonical.add(norm(target));

  // Longest first so "EPIC EVO" wins over "EPIC", and "LEVO SL" over "LEVO".
  const modelTokens = [
    ...aliasEntries.map(([alias, target]) => ({ token: norm(alias), model: norm(target) })),
    ...[...canonical].map(m => ({ token: m, model: m }))
  ].sort((a, b) => b.token.length - a.token.length);

  /*
   * Trim tokens keep the DECLARATION ORDER of model-rules.json — that order is the
   * priority, and must not be re-sorted by length. Sorting by length made the
   * generic material words win over the actual trim level: "LEVO PRO CARBON"
   * matched CARBON (6 chars) before PRO (3), and "LEVO SW CARBON" matched CARBON
   * before the S-WORKS alias SW (2). Within one trim, longer aliases go first.
   */
  const trimTokens = [];
  for (const [canonicalTrim, aliases] of Object.entries(rules.trims || {})) {
    const ordered = [...aliases].sort((a, b) => b.length - a.length);
    for (const alias of ordered) trimTokens.push({ token: norm(alias), trim: canonicalTrim });
  }

  const kindTokens = [];
  for (const [kind, keywords] of Object.entries(rules.kinds || {})) {
    for (const kw of keywords) kindTokens.push({ token: norm(kw), kind });
  }

  return { modelTokens, trimTokens, kindTokens, models: canonical };
}

const ALPHA_SIZES = ['XXS', 'XS', 'S', 'M', 'L', 'XL', 'XXL', 'XXXL', 'S/M', 'M/L'];

/*
 * Frame size, read from the tail of the description. Returns the size and the
 * SYSTEM it belongs to, because Specialized runs several in parallel: S1–S6
 * (S-Sizing, modern MTB), alpha S/M/L (older MTB and apparel-style), and
 * centimetres 38–64 (road/gravel). "S4" and "56" are not comparable, so any
 * chart mixing them on one axis is meaningless — the system travels with the
 * value so callers can keep them apart.
 *
 * Wheel diameters (26/27.5/29/650B) are explicitly NOT sizes. A description
 * ending "29" names the wheel; reading it as a frame size would fabricate one.
 */
export function deriveSize(description, matcher, treeSize) {
  const d = norm(description);
  if (d) {
    // Look at the last two tokens: the size is usually final, occasionally
    // followed by a stray marker.
    const toks = d.split(' ').filter(Boolean);
    for (const raw of [toks[toks.length - 1], toks[toks.length - 2]].filter(Boolean)) {
      const t = (rules.sizeAliases?.[raw]) || raw;
      if ((rules.wheelTokens || []).includes(t)) continue;      // wheel, not a size
      if (/^S[1-6]$/.test(t)) return { size: t, system: 's-sizing' };
      if (ALPHA_SIZES.includes(t)) return { size: t, system: 'alpha' };
      if (/^\d{2}$/.test(t)) {
        const n = Number(t);
        if (n >= 38 && n <= 64) return { size: t, system: 'cm' };
        if (n >= 10 && n <= 24) return { size: t, system: 'kids' };
      }
      if (/^\d$/.test(t)) return { size: t, system: 'kids' };
    }
  }
  // Fall back to the category tree's size slot (depth 4) when the description
  // carries none — it covers only ~37% of bikes but is authoritative where set.
  if (treeSize) {
    const t = norm(treeSize);
    if (/^S[1-6]$/.test(t)) return { size: t, system: 's-sizing' };
    if (ALPHA_SIZES.includes(t)) return { size: t, system: 'alpha' };
    if (/^\d{2}$/.test(t)) {
      const n = Number(t);
      if (n >= 38 && n <= 64) return { size: t, system: 'cm' };
      if (n >= 10 && n <= 24) return { size: t, system: 'kids' };
    }
  }
  return { size: null, system: null };
}

/** Derive kind from the description. Order in the rules file sets precedence. */
export function deriveKind(description, matcher) {
  const d = norm(description);
  if (!d) return 'unknown';
  for (const { token, kind } of matcher.kindTokens) {
    if (d.includes(token)) return kind;
  }
  return 'complete';
}

/**
 * Classify one item.
 * @param description item description (the primary signal)
 * @param treeModel   model_name from the item's category, used only as fallback/cross-check
 * @param treeTrim    variant_name from the item's category (same)
 */
export function classifyItem(description, treeModel, treeTrim, matcher) {
  const d = norm(description);
  const kind = deriveKind(description, matcher);

  // Model: earliest-then-longest description match. Models lead the description
  // ("EPIC 8 SW CARB..."), so an earlier position is the stronger signal; length
  // breaks ties so "EPIC 8" beats "EPIC" at the same position.
  let model = null, matchedText = null, source = null;
  let bestIdx = Infinity, bestLen = 0;
  for (const { token, model: canonicalModel } of matcher.modelTokens) {
    const idx = tokenIndex(d, token);
    if (idx === -1) continue;
    if (idx < bestIdx || (idx === bestIdx && token.length > bestLen)) {
      bestIdx = idx; bestLen = token.length; model = canonicalModel; matchedText = token;
    }
  }
  if (model) source = 'description';

  // Fall back to the tree only when the description yields nothing.
  if (!model && treeModel) { model = norm(treeModel); source = 'category'; }

  // Trim: search the text AFTER the model token, so a model containing a trim-like
  // word cannot be read as its own trim.
  let trim = null;
  if (model) {
    const tail = bestIdx === Infinity ? d : d.slice(bestIdx + bestLen);
    for (const { token, trim: canonicalTrim } of matcher.trimTokens) {
      if (containsToken(tail, token)) { trim = canonicalTrim; break; }
    }
  }
  if (!trim && treeTrim && /^(S-WORKS|PRO|EXPERT|COMP|ELITE|SPORT|ALLOY|CARBON|BASE)/i.test(treeTrim)) {
    trim = norm(treeTrim) === 'EPXERT' ? 'EXPERT' : norm(treeTrim);
  }

  return {
    model,
    trim,
    kind,
    source,
    matchedText,
    // Where in the description the model name appeared. Position 0 means the item
    // IS that model; a match further in usually means the item is a part FOR it
    // ("BLT MY16 LEVO BOLT KIT"), which is why uncategorised items require 0.
    matchIndex: bestIdx === Infinity ? -1 : bestIdx,
    treeModel: treeModel ? norm(treeModel) : null,
    // Disagreement is surfaced, never silently resolved.
    agreesWithTree: treeModel ? norm(treeModel) === model : null
  };
}
