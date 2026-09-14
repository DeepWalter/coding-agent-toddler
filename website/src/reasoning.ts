/**
 * Reasoning text → HTML for the thinking block.
 *
 * Chain-of-thought is freeform prose, so it never goes through markdown:
 * literal `*`, `1.`, indentation and everything else the model wrote is
 * escaped and emitted exactly as-is.  The one thing lifted out is code —
 * fenced blocks and `inline` spans — where colour is what a thought's code
 * reads worst without.  Fence syntax is consumed, so the block shows the code
 * rather than the markers around it.
 */

import { escapeHtml, highlightToHtml } from './highlight'
import { withRenderCache } from './renderCache'

/** A fence opens on a line of three or more backticks or tildes, optionally
 *  followed by an info string (its first word names the language). */
const FENCE = /^[ \t]*(`{3,}|~{3,})(.*)$/

export const renderReasoning = withRenderCache(renderReasoningUncached)

function renderReasoningUncached(text: string): string {
  const parts: string[] = []
  let prose: string[] = []
  let code: string[] = []
  let info = ''
  let marker: string | null = null

  const flushProse = () => {
    if (prose.length) parts.push(renderProse(prose.join('\n')))
    prose = []
  }
  const flushCode = () => {
    parts.push(renderCode(code.join('\n'), info))
    code = []
  }

  for (const line of text.split('\n')) {
    const match = FENCE.exec(line)
    const ticks = match?.[1] ?? null
    const rest = (match?.[2] ?? '').trim()

    if (marker === null) {
      if (ticks) {
        flushProse()
        marker = ticks
        info = rest.split(/\s+/)[0] ?? ''
        continue
      }
      prose.push(line)
      continue
    }
    // Inside a fence: only a marker of the same character, at least as long,
    // with nothing after it, closes the block.
    if (ticks && ticks[0] === marker[0] && ticks.length >= marker.length && !rest) {
      flushCode()
      marker = null
      continue
    }
    code.push(line)
  }
  // A fence still open when the text runs out is the streaming case — colour
  // what has arrived so far rather than waiting for the closing line.
  if (marker !== null) flushCode()
  flushProse()
  return parts.join('')
}

/** Prose with `inline` spans chipped.  Only a paired backtick becomes code:
 *  a lone one stays literal, so a half-streamed pair does not flicker. */
function renderProse(text: string): string {
  return text
    .split(/(`[^`\n]+`)/g)
    .map((part) =>
      part.length > 2 && part.startsWith('`') && part.endsWith('`')
        ? `<code class="thinking-inline">${escapeHtml(part.slice(1, -1))}</code>`
        : escapeHtml(part),
    )
    .join('')
}

/** A fenced block, highlighted when the language is one the console knows.
 *  The lines are joined without their trailing newline — that is the line
 *  ending before the closing fence, not content, and keeping it would draw a
 *  blank line inside the block. */
function renderCode(code: string, lang: string): string {
  const html = highlightToHtml(code, lang) ?? escapeHtml(code)
  return `<code class="thinking-code">${html}</code>`
}
