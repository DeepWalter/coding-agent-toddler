/**
 * The thinking-effort scale: the eight tiers the server stores, all of them
 * offered as stops.
 *
 * The scale is shown whole because these names are the vocabulary every
 * other surface speaks — `--reasoning-effort`, `TODDLER_EFFORT_LEVEL`,
 * `/effort` — so a control that offered only some of them could not show a
 * tier a conversation was actually set to, and could not set the tier it
 * showed.
 *
 * Three pairs of them differ only in name: the provider collapses the finer
 * tiers onto DeepSeek's coarse low/high/max scale
 * (`_DEEPSEEK_REASON_EFFORT_MAP` in `toddler/llm/provider.py` — `minimal`
 * and `low` are both `low`, `medium`/`high`/`xhigh` are all `high`, `max`
 * and `ultra` are both `max`).  So `medium` → `high` is a real move of the
 * knob and no change at all to the request.  That is the endpoint's
 * granularity, not this control's.
 */

/** The stops, weakest to strongest — the server's own list and order
 *  (`toddler/config/defaults.py::REASONING_EFFORT_TIERS`). */
export const EFFORT_STOPS = [
  'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra',
] as const
export type EffortStop = (typeof EFFORT_STOPS)[number]

/** The same names as a plain list, for membership tests. */
const KNOWN: readonly string[] = EFFORT_STOPS

/**
 * Where the knob sits for a stored tier: on that tier.
 *
 * An unrecognized tier parks at `max`, mirroring the provider's
 * `.get(tier, "max")` coercion and `completion_budget_for`'s top-budget
 * fallback (`toddler/config/models.py`) — the two places that decide what
 * such a tier costs, so the knob should not claim anything cheaper.
 *
 * Unset parks at `high`: a conversation that names no tier sends no
 * `reasoning_effort` at all, so the endpoint's own default applies, and
 * DeepSeek's default is `high` (the provider says so, and the 32K budget
 * band agrees).
 */
export function stopForTier(tier: string | null | undefined): EffortStop {
  if (tier === null || tier === undefined || tier.trim() === '') return 'high'
  const name = tier.trim().toLowerCase()
  return KNOWN.includes(name) ? (name as EffortStop) : 'max'
}

/** The tier a picked stop stores.  The identity, since a stop *is* a tier —
 *  named for symmetry with :func:`stopForTier` so a call site reads as the
 *  direction it is going. */
export function tierForStop(stop: EffortStop): string {
  return stop
}

/** The stop's index, for the knob's fraction and the slider's ARIA value. */
export function stopIndexOf(stop: EffortStop): number {
  return EFFORT_STOPS.indexOf(stop)
}

/**
 * What the pill and the menu row show: the stored tier verbatim, so an
 * unknown one is shown exactly as the server spelled it.
 *
 * Unset reads `default` — the endpoint's own default is what is actually
 * happening, and naming the stop instead would claim a tier the
 * conversation does not hold.
 */
export function effortLabel(tier: string | null | undefined): string {
  return tier === null || tier === undefined || tier.trim() === ''
    ? 'default'
    : tier
}
