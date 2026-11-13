/**
 * The copy and check glyphs, shared by CopyButton.vue and the copy buttons
 * the markdown renderer bakes into its HTML.
 *
 * The markdown side can only consume a string (`v-html` renders a cached
 * string, not a Vue tree), so the single source of truth is markup.  The
 * component injects the same strings instead of repeating the paths.
 */

const SVG_ATTRS = [
  'viewBox="0 0 24 24"',
  'width="13"',
  'height="13"',
  'fill="none"',
  'stroke="currentColor"',
  'stroke-width="2"',
  'stroke-linecap="round"',
  'stroke-linejoin="round"',
  'aria-hidden="true"',
].join(' ')

const COPY_BODY =
  '<rect x="9" y="9" width="13" height="13" rx="2" ry="2" />' +
  '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />'

const CHECK_BODY = '<polyline points="20 6 9 17 4 12" />'

/** `className` gives a caller a CSS hook — the code block's button uses it
 *  to show one glyph at a time. */
const icon = (body: string, className?: string): string =>
  `<svg${className ? ` class="${className}"` : ''} ${SVG_ATTRS}>${body}</svg>`

export const copyIcon = (className?: string): string => icon(COPY_BODY, className)

export const checkIcon = (className?: string): string => icon(CHECK_BODY, className)
