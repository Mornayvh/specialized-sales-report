/*
 * Lightspeed categories are a tree, and fullPathName denormalises the whole
 * ancestry into one string:
 *
 *   BIKE / MTB / EPIC 8 / COMP / L / 29
 *    [0]   [1]     [2]     [3]  [4]  [5]
 *
 * The sales team reports bikes by MODEL (segment 2 — EPIC 8, LEVO SL, CHISEL)
 * and sometimes by model + TRIM (segment 3 — S-WORKS, EXPERT, COMP ALLOY).
 * Deeper segments are size and wheel diameter, which are SKU-level detail.
 *
 * Deriving these from the path means the groupings stay in sync with whatever
 * the shop maintains in Lightspeed — no hardcoded model list to fall behind the
 * range, which matters because bike model years turn over constantly.
 */
export function categorySegments(fullPathName, name) {
  const segs = (fullPathName || name || '').split('/').map(s => s.trim()).filter(Boolean);
  return {
    topLevelName: segs[0] ?? null,
    disciplineName: segs[1] ?? null,
    modelName: segs[2] ?? null,
    variantName: segs[3] ?? null
  };
}

// Human label for a model row: "LEVO SL" or "LEVO SL EXPERT".
export function modelLabel(modelName, variantName, includeVariant) {
  if (!modelName) return null;
  return includeVariant && variantName ? `${modelName} ${variantName}` : modelName;
}
