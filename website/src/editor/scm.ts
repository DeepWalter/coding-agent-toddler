import { Chunk } from '@codemirror/merge'
import {
  RangeSet,
  StateEffect,
  StateField,
  Text,
  type Extension,
  type Range,
} from '@codemirror/state'
import {
  EditorView,
  GutterMarker,
  ViewPlugin,
  gutter,
  type ViewUpdate,
} from '@codemirror/view'
import {
  chunksToLineChanges,
  splitLines,
  type ScmLineChange,
} from './lineDiff'

/**
 * SCM decorations for the editor: a left gutter with a green bar per
 * added line, a blue bar per modified line, and a red triangle where a
 * deletion begins; plus matching ticks in an overview ruler beside the
 * right scrollbar.  Everything is driven by one state field, so an
 * effects-only transaction updates both views without touching the
 * document, undo history, or dirty state.
 *
 * The diff is `Chunk.build` from @codemirror/merge — a line-boundary
 * character diff — against the HEAD copy fetched by FileEditor.  It is
 * rerun on a short debounce while typing, so the marks track the buffer
 * rather than only the saved file.
 */

class ScmMarker extends GutterMarker {
  elementClass: string

  constructor(readonly kind: ScmLineChange['kind']) {
    super()
    this.elementClass = `cm-scm-${kind}`
  }

  eq(other: GutterMarker): boolean {
    return other instanceof ScmMarker && other.kind === this.kind
  }
}

const MARKERS = {
  add: new ScmMarker('add'),
  mod: new ScmMarker('mod'),
  del: new ScmMarker('del'),
}

interface ScmEntry {
  kind: ScmLineChange['kind']
  /** Document offsets: start of the first changed line and end of the
   *  last changed line.  Deletion entries are a point. */
  from: number
  to: number
}

interface ScmValue {
  markers: RangeSet<GutterMarker>
  entries: ScmEntry[]
}

const EMPTY: ScmValue = { markers: RangeSet.empty, entries: [] }

/** Replace the decorations for the current document.  FileEditor
 *  dispatches this after each debounced diff. */
export const setScmChanges = StateEffect.define<ScmLineChange[]>()

function buildValue(doc: Text, changes: readonly ScmLineChange[]): ScmValue {
  if (!changes.length) return EMPTY
  const ranges: Range<GutterMarker>[] = []
  const entries: ScmEntry[] = []
  for (const change of changes) {
    const first = Math.max(1, Math.min(change.start, doc.lines))
    const last = Math.max(first, Math.min(change.end, doc.lines))
    if (change.kind === 'del') {
      const line = doc.line(first)
      ranges.push(MARKERS.del.range(line.from))
      entries.push({ kind: 'del', from: line.from, to: line.from })
      continue
    }
    for (let n = first; n <= last; n++) {
      ranges.push(MARKERS[change.kind].range(doc.line(n).from))
    }
    entries.push({
      kind: change.kind,
      from: doc.line(first).from,
      to: doc.line(last).to,
    })
  }
  return { markers: RangeSet.of(ranges, true), entries }
}

/** The decorations live in the tab's EditorState, so they survive a tab
 *  switch through FileEditor's state flush.  Failed state swaps rebuild
 *  the state (and refetch), which is exactly when the diff must rerun. */
export const scmField = StateField.define<ScmValue>({
  create: () => EMPTY,
  update(value, tr) {
    let next = value
    if (tr.docChanged && value.entries.length) {
      // Keep the marks tracking their lines between debounced diffs.
      next = {
        markers: value.markers.map(tr.changes),
        entries: value.entries.map((entry) => {
          const from = tr.changes.mapPos(entry.from, -1)
          return {
            kind: entry.kind,
            from,
            to: entry.kind === 'del' ? from : tr.changes.mapPos(entry.to, 1),
          }
        }),
      }
    }
    for (const effect of tr.effects) {
      if (effect.is(setScmChanges)) next = buildValue(tr.state.doc, effect.value)
    }
    return next
  },
})

const scmGutter = gutter({
  class: 'cm-scmGutter',
  markers: (view) => view.state.field(scmField).markers,
})

interface RulerTick {
  kind: ScmLineChange['kind']
  top: number
  height: number
  /** Content-space y of the tick's center — where a click centers it. */
  anchor: number
}

const TICK_LABEL: Record<ScmLineChange['kind'], string> = {
  add: 'Added',
  mod: 'Modified',
  del: 'Deleted',
}

interface RulerMeasure {
  height: number
  ticks: RulerTick[]
}

const scmRuler = ViewPlugin.fromClass(
  class {
    readonly dom = document.createElement('div')
    private destroyed = false
    private retries = 0

    constructor(private readonly view: EditorView) {
      this.dom.className = 'cm-scmRuler'
      this.dom.setAttribute('aria-hidden', 'true')
      this.dom.addEventListener('click', (event) => this.reveal(event))
      view.dom.appendChild(this.dom)
      this.schedule()
    }

    /** Center a clicked tick's change in the visible editor.  The scroll
     *  position is chosen so the thumb's center lands on the tick; no
     *  selection or focus change is made — the gutter already marks the
     *  change. */
    private reveal(event: MouseEvent) {
      const target =
        event.target instanceof Element
          ? event.target.closest<HTMLElement>('.cm-scmTick')
          : null
      if (!target) return
      const anchor = Number(target.dataset.anchor)
      if (!Number.isFinite(anchor)) return
      const scroller = this.view.scrollDOM
      // The ruler's transformed height is the visible editor height; the
      // target scroll puts the anchor at its center.
      const visible = this.dom.getBoundingClientRect().height
      const maxScroll = Math.max(0, scroller.scrollHeight - scroller.clientHeight)
      scroller.scrollTop = Math.min(
        maxScroll,
        Math.max(0, anchor - visible / 2),
      )
    }

    update(update: ViewUpdate) {
      if (
        update.docChanged ||
        update.heightChanged ||
        update.geometryChanged ||
        update.startState.field(scmField) !== update.state.field(scmField)
      ) {
        this.schedule()
      }
    }

    /** Re-measure on demand — the terminal panel's height is a CSS input
     *  the view's own update triggers cannot see. */
    refresh() {
      this.schedule()
    }

    private schedule() {
      if (this.destroyed) return
      this.view.requestMeasure({
        key: this,
        read: (view) => this.read(view),
        write: (measure) => this.write(measure),
      })
    }

    private read(view: EditorView): RulerMeasure | null {
      const height = view.scrollDOM.clientHeight
      // The scrollable range the thumb represents: the content height minus
      // the strip hidden under the terminal panel.  A tick's fraction
      // against this total is exactly where the thumb's center sits when the
      // change is centerd in the visible editor, so a tick inside the thumb
      // always names a change inside the visible range.
      const overlap =
        Number.parseFloat(
          getComputedStyle(view.dom).getPropertyValue('--terminal-overlap'),
        ) || 0
      const total = Math.max(1, view.scrollDOM.scrollHeight - overlap)
      if (!height) return null

      const entries = view.state.field(scmField).entries
      const padding = view.documentPadding.top
      const ticks: RulerTick[] = []
      for (const entry of entries) {
        const start = view.lineBlockAt(
          Math.min(entry.from, view.state.doc.length),
        )
        if (entry.kind === 'del') {
          const anchor = padding + start.top + start.height / 2
          const center = anchor / total
          ticks.push({
            kind: 'del',
            top: center * height - 1,
            height: 2,
            anchor,
          })
          continue
        }
        const end = view.lineBlockAt(
          Math.min(Math.max(entry.to, entry.from), view.state.doc.length),
        )
        const top = padding + start.top
        const bottom = padding + end.top + end.height
        ticks.push({
          kind: entry.kind,
          top: (top / total) * height,
          height: Math.max(2, ((bottom - top) / total) * height),
          anchor: (top + bottom) / 2,
        })
      }
      // The ruler hugs the editor's right edge in both states: the 8px
      // column the native scrollbar occupies while the terminal panel is
      // closed, and the overlay bar's column while it is open.  (The theme
      // pins `right: 0`; no inline offset is written.)
      return { height, ticks }
    }

    private write(measure: RulerMeasure | null) {
      if (this.destroyed) return
      if (!measure) {
        // A hidden editor reports zero height; retry briefly so the
        // first show after a diff/document tab paints the ticks.
        if (this.retries++ < 10) {
          requestAnimationFrame(() => this.schedule())
        }
        return
      }
      this.retries = 0
      this.dom.style.height = `${measure.height}px`
      const children: HTMLElement[] = []
      for (const tick of measure.ticks) {
        const el = document.createElement('div')
        el.className = `cm-scmTick ${tick.kind}`
        el.style.top = `${tick.top}px`
        el.style.height = `${tick.height}px`
        el.dataset.anchor = String(tick.anchor)
        el.title = `${TICK_LABEL[tick.kind]} change — click to center`
        children.push(el)
      }
      this.dom.replaceChildren(...children)
    }

    destroy() {
      this.destroyed = true
      this.dom.remove()
    }
  },
)

/** Compute the line changes between the HEAD text and the current
 *  buffer.  `Chunk.build` has no MergeView-style default scan limit, so
 *  pass one explicitly; the timeout keeps a pathological rewrite bounded
 *  (an imprecise result still yields usable chunks). */
export function computeScmChanges(
  baseline: string,
  current: string,
): ScmLineChange[] {
  if (baseline === current) return []
  const head = Text.of(splitLines(baseline))
  const buffer = Text.of(splitLines(current))
  const chunks = Chunk.build(head, buffer, {
    scanLimit: 100_000,
    timeout: 50,
  })
  return chunksToLineChanges(chunks, buffer)
}

/** Re-measure the overview ruler (the terminal panel's height changed). */
export function refreshScmRuler(view: EditorView): void {
  view.plugin(scmRuler)?.refresh()
}

/** The one extension bundle every SCM-enabled editor state installs. */
export const scmExtensions: Extension = [scmField, scmGutter, scmRuler]
