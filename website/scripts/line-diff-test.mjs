// Unit tests for the editor's line-change conversion: CodeMirror's merge
// chunks in, the 1-based add/mod/del ranges the SCM gutter and overview
// ruler consume out.  Run with `node --test website/scripts/line-diff-test.mjs`
// (Node 24 strips the imported .ts types).
import test from 'node:test'
import assert from 'node:assert/strict'
import { Chunk } from '@codemirror/merge'
import { Text } from '@codemirror/state'
import { chunksToLineChanges, splitLines } from '../src/editor/lineDiff.ts'

function text(value) {
  return Text.of(splitLines(value))
}

function diff(a, b) {
  const head = text(a)
  const current = text(b)
  return chunksToLineChanges(
    Chunk.build(head, current, { scanLimit: 100_000, timeout: 50 }),
    current,
  )
}

test('splitLines keeps the trailing empty line and drops CRLF', () => {
  assert.deepEqual(splitLines('a\r\nb\r\n'), ['a', 'b', ''])
  assert.deepEqual(splitLines('a\nb'), ['a', 'b'])
  assert.deepEqual(splitLines(''), [''])
})

test('equal content has no changes', () => {
  assert.deepEqual(diff('a\nb\n', 'a\nb\n'), [])
})

test('an inserted line is an addition', () => {
  assert.deepEqual(diff('a\nc\n', 'a\nb\nc\n'), [
    { kind: 'add', start: 2, end: 2 },
  ])
  assert.deepEqual(diff('b\nc\n', 'a\nb\nc\n'), [
    { kind: 'add', start: 1, end: 1 },
  ])
  assert.deepEqual(diff('a\nb\n', 'a\nb\nc\n'), [
    { kind: 'add', start: 3, end: 3 },
  ])
})

test('a deleted line anchors on the line that moved up', () => {
  assert.deepEqual(diff('a\nb\nc\n', 'a\nc\n'), [
    { kind: 'del', start: 2, end: 2 },
  ])
  assert.deepEqual(diff('a\nb\nc\n', 'b\nc\n'), [
    { kind: 'del', start: 1, end: 1 },
  ])
  assert.deepEqual(diff('a\nb\nc\n', 'a\nb\n'), [
    { kind: 'del', start: 3, end: 3 },
  ])
})

test('a replacement is modified, including unequal line counts', () => {
  assert.deepEqual(diff('a\nb\nc\n', 'a\nX\nc\n'), [
    { kind: 'mod', start: 2, end: 2 },
  ])
  assert.deepEqual(diff('a\nb\nc\n', 'a\nX\nY\nc\n'), [
    { kind: 'mod', start: 2, end: 3 },
  ])
  assert.deepEqual(diff('a\nb\nc\nd\n', 'a\nX\nd\n'), [
    { kind: 'mod', start: 2, end: 2 },
  ])
})

test('separate changes stay separate chunks', () => {
  assert.deepEqual(diff('a\nb\nc\nd\ne\n', 'a\nB\nc\nd\nE\n'), [
    { kind: 'mod', start: 2, end: 2 },
    { kind: 'mod', start: 5, end: 5 },
  ])
})

test('empty and whole-file differences map to one range', () => {
  assert.deepEqual(diff('', 'a\nb\n'), [
    { kind: 'add', start: 1, end: 2 },
  ])
  assert.deepEqual(diff('a\nb\n', ''), [
    { kind: 'del', start: 1, end: 1 },
  ])
})

test('CRLF-only differences are invisible', () => {
  assert.deepEqual(diff('a\r\nb\r\n', 'a\nb\n'), [])
})

test('final-newline changes surface as modifications', () => {
  assert.deepEqual(diff('a', 'a\n'), [
    { kind: 'mod', start: 1, end: 2 },
  ])
  assert.deepEqual(diff('a\n', 'a'), [
    { kind: 'mod', start: 1, end: 1 },
  ])
})

test('plain chunk objects are accepted, with ends clamped to the document', () => {
  const current = Text.of(['a', 'b', ''])
  // A deletion at the final empty line (fromB == toB == doc.length).
  assert.deepEqual(
    chunksToLineChanges([{ fromA: 2, toA: 4, fromB: 4, toB: 4 }], current),
    [{ kind: 'del', start: 3, end: 3 }],
  )
  // Chunk ends may point past the document; the last line still wins.
  assert.deepEqual(
    chunksToLineChanges([{ fromA: 0, toA: 2, fromB: 0, toB: 5 }], current),
    [{ kind: 'mod', start: 1, end: 3 }],
  )
})
