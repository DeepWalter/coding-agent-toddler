import MarkdownIt from 'markdown-it'
import { escapeHtml, highlightToHtml } from './highlight'
import { withRenderCache } from './renderCache'

const md = new MarkdownIt({
  html: false, // escape raw HTML — LLM output is untrusted
  linkify: true,
  // Keep the console's newline-faithful feel (every newline visible, like the
  // old pre-wrap). Deliberate divergence from the CLI, where Rich collapses
  // softbreaks to a single space.
  breaks: true,
  highlight(str, lang) {
    // '' is markdown-it's "no highlight" — it then emits escaped <pre><code>.
    return highlightToHtml(str, lang) ?? ''
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

export const renderMarkdown = withRenderCache((text: string) => md.render(text))
