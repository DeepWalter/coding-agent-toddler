import MarkdownIt from 'markdown-it'
import { checkIcon, copyIcon } from './copyIcon'
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

// The copy action a code fence carries in the console (the `codeCopy` env
// flag below).  It wears the shared CopyButton's classes but cannot be that
// component: the block is one cached HTML string rendered through `v-html`,
// so the button's feedback is a class toggled by the delegated click handler
// (codeCopy.ts) and both glyphs ship in the markup (copyIcon.ts).
const CODE_COPY_BUTTON = `<button
  type="button"
  class="copy-button code-copy"
  data-code-copy
  aria-label="Copy code"
  title="Copy code"
>${copyIcon('code-copy-glyph')}${checkIcon('code-copy-check')}</button>`

/** Wrap a default code-block render in the positioning shell the button
 *  needs.  The wrapper, not the <pre>, is the positioning context: a wide
 *  block scrolls sideways, and the button must stay in the corner. */
const withCodeCopy = (html: string, env: unknown): string =>
  (env as { codeCopy?: boolean } | undefined)?.codeCopy
    ? `<div class="code-block">${CODE_COPY_BUTTON}${html}</div>`
    : html

const defaultFence = md.renderer.rules.fence
md.renderer.rules.fence = (tokens, idx, options, env, self) =>
  withCodeCopy(defaultFence(tokens, idx, options, env, self), env)

const defaultCodeBlock = md.renderer.rules.code_block
md.renderer.rules.code_block = (tokens, idx, options, env, self) =>
  withCodeCopy(defaultCodeBlock(tokens, idx, options, env, self), env)

export const renderMarkdown = withRenderCache((text: string) => md.render(text))

/** Console markdown — a reply or a thought — with a copy action in every
 *  code block.  Its own cache: the same text rendered in document mode
 *  (FileEditorDocView) must keep its blocks button-free. */
export const renderMarkdownWithCodeCopy = withRenderCache((text: string) =>
  md.render(text, { codeCopy: true }))
