/**
 * The streaming console's render cache — one copy of the rule that unchanged
 * text is never parsed twice.
 */

const MAX_ENTRIES = 100
const MAX_CHARS = 50_000

/**
 * Wrap a pure text→HTML renderer in an exact-input cache.
 *
 * Streaming consoles re-render the same message text repeatedly (every stream
 * frame, plus unrelated re-renders like a theme toggle), and a render is pure
 * — so unchanged text never re-parses or re-highlights.  Huge texts are
 * skipped: they're unique anyway (streaming) and caching them would just hold
 * a full duplicate in memory.
 */
export function withRenderCache(
  render: (text: string) => string,
): (text: string) => string {
  const cache = new Map<string, string>()
  return (text: string) => {
    if (text.length > MAX_CHARS) return render(text)
    const cached = cache.get(text)
    if (cached !== undefined) return cached
    const html = render(text)
    if (cache.size >= MAX_ENTRIES) {
      // Map iterates in insertion order — evict the oldest entry.
      const oldest = cache.keys().next().value
      if (oldest !== undefined) cache.delete(oldest)
    }
    cache.set(text, html)
    return html
  }
}
