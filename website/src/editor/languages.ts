import { StreamLanguage, type Language } from '@codemirror/language'
import { python } from '@codemirror/legacy-modes/mode/python'
import { javascript, json, typescript } from '@codemirror/legacy-modes/mode/javascript'
import { c, cpp, csharp, dart, java, kotlin } from '@codemirror/legacy-modes/mode/clike'
import { css, less, sCSS } from '@codemirror/legacy-modes/mode/css'
import { html, xml } from '@codemirror/legacy-modes/mode/xml'
import { shell } from '@codemirror/legacy-modes/mode/shell'
import { yaml } from '@codemirror/legacy-modes/mode/yaml'
import { toml } from '@codemirror/legacy-modes/mode/toml'
import { go } from '@codemirror/legacy-modes/mode/go'
import { rust } from '@codemirror/legacy-modes/mode/rust'
import { standardSQL } from '@codemirror/legacy-modes/mode/sql'
import { properties } from '@codemirror/legacy-modes/mode/properties'
import { dockerFile } from '@codemirror/legacy-modes/mode/dockerfile'
import { diff } from '@codemirror/legacy-modes/mode/diff'
import { clojure } from '@codemirror/legacy-modes/mode/clojure'
import { ruby } from '@codemirror/legacy-modes/mode/ruby'
import { basename } from '../utils'

/**
 * File path → CodeMirror language.  Grammars come from
 * `@codemirror/legacy-modes`, the same highlight.js family the console
 * already uses via `markdown.ts`, so both panes agree on what a token
 * means.  Extensions with no legacy-mode grammar (markdown, makefile)
 * return null → plain text, no highlighting.
 */

const L = {
  python: StreamLanguage.define(python),
  javascript: StreamLanguage.define(javascript),
  typescript: StreamLanguage.define(typescript),
  json: StreamLanguage.define(json),
  c: StreamLanguage.define(c),
  cpp: StreamLanguage.define(cpp),
  java: StreamLanguage.define(java),
  csharp: StreamLanguage.define(csharp),
  kotlin: StreamLanguage.define(kotlin),
  dart: StreamLanguage.define(dart),
  css: StreamLanguage.define(css),
  scss: StreamLanguage.define(sCSS),
  less: StreamLanguage.define(less),
  html: StreamLanguage.define(html),
  xml: StreamLanguage.define(xml),
  shell: StreamLanguage.define(shell),
  yaml: StreamLanguage.define(yaml),
  toml: StreamLanguage.define(toml),
  go: StreamLanguage.define(go),
  rust: StreamLanguage.define(rust),
  sql: StreamLanguage.define(standardSQL),
  properties: StreamLanguage.define(properties),
  dockerfile: StreamLanguage.define(dockerFile),
  diff: StreamLanguage.define(diff),
  clojure: StreamLanguage.define(clojure),
  ruby: StreamLanguage.define(ruby),
}

// Case-insensitive extension → language.  JSX/TSX reuse the plain
// JavaScript/TypeScript grammars: legacy-modes has no JSX mode, so tags
// degrade to identifier tokens — acceptable, no error.
const byExt: Record<string, Language> = {
  py: L.python, pyi: L.python,
  js: L.javascript, mjs: L.javascript, cjs: L.javascript, jsx: L.javascript,
  ts: L.typescript, mts: L.typescript, cts: L.typescript, tsx: L.typescript,
  json: L.json,
  yaml: L.yaml, yml: L.yaml,
  toml: L.toml,
  sh: L.shell, bash: L.shell, zsh: L.shell,
  css: L.css, scss: L.scss, less: L.less,
  html: L.html, htm: L.html,
  vue: L.html, // no vue grammar — template highlights, script/style stay plain
  xml: L.xml, svg: L.xml,
  go: L.go,
  rs: L.rust,
  java: L.java,
  c: L.c, h: L.c,
  cpp: L.cpp, cc: L.cpp, cxx: L.cpp, hpp: L.cpp, hh: L.cpp,
  cs: L.csharp,
  kt: L.kotlin, kts: L.kotlin,
  dart: L.dart,
  sql: L.sql,
  ini: L.properties, conf: L.properties,
  diff: L.diff, patch: L.diff,
  clj: L.clojure, cljs: L.clojure,
  rb: L.ruby,
}

// Filename-based lookup for files without an extension.
const byName: Record<string, Language> = {
  dockerfile: L.dockerfile,
}

/** Language for a file path, or null for plain text (no highlighting). */
export function languageForPath(path: string | null): Language | null {
  if (!path) return null
  const base = basename(path)
  const byFilename = byName[base.toLowerCase()]
  if (byFilename) return byFilename
  const dot = base.lastIndexOf('.')
  if (dot <= 0) return null
  return byExt[base.slice(dot + 1).toLowerCase()] ?? null
}
