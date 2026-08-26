import MarkdownIt from 'markdown-it'
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

const escapeHtml = (s: string) =>
  s.replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] ?? c)

const md = new MarkdownIt({
  html: false, // escape raw HTML — LLM output is untrusted
  linkify: true,
  // Keep the console's newline-faithful feel (every newline visible, like the
  // old pre-wrap). Deliberate divergence from the CLI, where Rich collapses
  // softbreaks to a single space.
  breaks: true,
  highlight(str: string, lang: string) {
    const language = languageForName(lang)
    if (!language) return ''
    try {
      // Inner HTML only — markdown-it wraps it in <pre><code> itself.
      // Every token gets a span (identifiers included) except unstyled
      // ones; the raw source is escaped here because highlightCode hands
      // it back verbatim.
      const tree = language.parser.parse(str)
      let out = ''
      highlightCode(
        str,
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
      // Fall through to markdown-it's escaped default.
      return ''
    }
  },
})

// Repo-relative links (no scheme, not protocol- or root-relative, not a
// fragment) point at files in the workspace — the message pane intercepts
// their clicks and opens them in the editor.  Everything else is external:
// open in a new tab.  URL schemes were already validated during parse
// (markdown-it rejects javascript:/vbscript:/file:/data:).
const linkOpen = md.renderer.rules.link_open ?? ((tokens, idx, options, _env, self) =>
  self.renderToken(tokens, idx, options))
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  const href = String(tokens[idx].attrGet('href') ?? '')
  const internal =
    !/^[a-z][a-z0-9+.-]*:/i.test(href) &&
    !href.startsWith('//') &&
    !href.startsWith('/') &&
    !href.startsWith('#')
  if (internal) {
    tokens[idx].attrSet('data-file', href)
  } else {
    tokens[idx].attrSet('target', '_blank')
    tokens[idx].attrSet('rel', 'noopener noreferrer')
  }
  return linkOpen(tokens, idx, options, env, self)
}

// No <img> elements in a text console: render images as their alt text only
// (escaped — no tracking pixels, no layout abuse).
md.renderer.rules.image = (tokens, idx) => escapeHtml(tokens[idx].content)

// Streaming consoles re-render the same message text repeatedly (every
// stream frame, plus unrelated re-renders like a theme toggle), and a render
// is pure — cache by exact input so unchanged text never re-parses or
// re-highlights.  Huge texts are skipped: they're unique anyway (streaming)
// and caching them would just hold a full duplicate in memory.
const renderCache = new Map<string, string>()
const RENDER_CACHE_MAX = 100
const RENDER_CACHE_MAX_CHARS = 50_000

export function renderMarkdown(text: string): string {
  if (text.length > RENDER_CACHE_MAX_CHARS) return md.render(text)
  const cached = renderCache.get(text)
  if (cached !== undefined) return cached
  const html = md.render(text)
  if (renderCache.size >= RENDER_CACHE_MAX) {
    // Map iterates in insertion order — evict the oldest entry.
    const oldest = renderCache.keys().next().value
    if (oldest !== undefined) renderCache.delete(oldest)
  }
  renderCache.set(text, html)
  return html
}
