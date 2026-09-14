/**
 * Code → highlighted HTML, in the console's own grammar.
 *
 * Shared by the markdown renderer (fenced and inline code in an answer) and
 * the reasoning renderer (the code a thought emits), so code highlights the
 * same wherever it appears.
 */

import { highlightCode } from '@lezer/highlight'
import { StyleModule } from 'style-mod'
import { languageForName } from './editor/languages'
import { highlightStyle } from './editor/theme'

// The console highlights code with the same CodeMirror grammar and highlight
// style as the editor pane, so both agree on every token — including plain
// identifiers, which highlight.js left unstyled.  HighlightStyle.define
// generates the token class names once, and the generated CSS must be in the
// document before the first console-rendered span appears — which can happen
// before any editor view has mounted the module — hence the mount at import.
// StyleSet dedupes by module instance, so the editor mounting it again is a
// no-op.
if (highlightStyle.module) StyleModule.mount(document, [highlightStyle.module])

export const escapeHtml = (s: string) =>
  s.replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] ?? c)

/**
 * Inner HTML for a code block — every token span-wrapped, the raw source
 * escaped here because `highlightCode` hands it back verbatim — or null when
 * the language is unknown or its grammar throws.  Callers fall back to
 * escaped plain text.
 */
export function highlightToHtml(code: string, lang: string): string | null {
  const language = languageForName(lang)
  if (!language) return null
  try {
    // Inner HTML only — markdown-it wraps it in <pre><code> itself.
    const tree = language.parser.parse(code)
    let out = ''
    highlightCode(
      code,
      tree,
      highlightStyle,
      (text, classes) => {
        out += classes ? `<span class="${classes}">${escapeHtml(text)}</span>` : escapeHtml(text)
      },
      () => {
        out += '\n'
      },
    )
    return out
  } catch {
    // Fall through to the caller's escaped default.
    return null
  }
}
