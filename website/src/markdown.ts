import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js/lib/common'

const md = new MarkdownIt({
  html: false, // escape raw HTML — LLM output is untrusted
  linkify: true,
  // Keep the console's newline-faithful feel (every newline visible, like the
  // old pre-wrap). Deliberate divergence from the CLI, where Rich collapses
  // softbreaks to a single space.
  breaks: true,
  highlight(str: string, lang: string) {
    if (lang && hljs.getLanguage(lang)) {
      try {
        // Inner HTML only — markdown-it wraps it in <pre><code> itself.
        return hljs.highlight(str, { language: lang, ignoreIllegals: true }).value
      } catch {
        // Fall through to markdown-it's escaped default.
      }
    }
    return ''
  },
})

// Open external links in a new tab. URL schemes were already validated during
// parse (markdown-it rejects javascript:/vbscript:/file:/data:).
const linkOpen = md.renderer.rules.link_open ?? ((tokens, idx, options, _env, self) =>
  self.renderToken(tokens, idx, options))
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return linkOpen(tokens, idx, options, env, self)
}

// No <img> elements in a text console: render images as their alt text only
// (escaped — no tracking pixels, no layout abuse).
md.renderer.rules.image = (tokens, idx) =>
  tokens[idx].content.replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] ?? c)

export function renderMarkdown(text: string): string {
  return md.render(text)
}
