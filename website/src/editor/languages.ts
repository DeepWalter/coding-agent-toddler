import { StreamLanguage, type Language } from '@codemirror/language'
// Legacy stream grammars — the fallback for languages with no tree-based
// package.  They only emit plain variableName, so function names and
// variables share the --variable color there.
import { shell } from '@codemirror/legacy-modes/mode/shell'
import { yaml } from '@codemirror/legacy-modes/mode/yaml'
import { toml } from '@codemirror/legacy-modes/mode/toml'
import { csharp, dart, kotlin } from '@codemirror/legacy-modes/mode/clike'
import { less } from '@codemirror/legacy-modes/mode/css'
import { properties } from '@codemirror/legacy-modes/mode/properties'
import { dockerFile } from '@codemirror/legacy-modes/mode/dockerfile'
import { diff } from '@codemirror/legacy-modes/mode/diff'
import { clojure } from '@codemirror/legacy-modes/mode/clojure'
import { ruby } from '@codemirror/legacy-modes/mode/ruby'
import { lua } from '@codemirror/legacy-modes/mode/lua'
import { perl } from '@codemirror/legacy-modes/mode/perl'
import { r } from '@codemirror/legacy-modes/mode/r'
import { swift } from '@codemirror/legacy-modes/mode/swift'
import { vb } from '@codemirror/legacy-modes/mode/vb'
import { vbScript } from '@codemirror/legacy-modes/mode/vbscript'
import { wast } from '@codemirror/legacy-modes/mode/wast'
// Tree-based grammars — tag function calls/defs as function(variableName),
// so the theme's --function/--variable split renders as intended.
import { pythonLanguage } from '@codemirror/lang-python'
import { javascriptLanguage, typescriptLanguage, jsxLanguage, tsxLanguage } from '@codemirror/lang-javascript'
import { jsonLanguage } from '@codemirror/lang-json'
import { cssLanguage } from '@codemirror/lang-css'
import { sassLanguage } from '@codemirror/lang-sass'
import { htmlLanguage } from '@codemirror/lang-html'
import { xmlLanguage } from '@codemirror/lang-xml'
import { goLanguage } from '@codemirror/lang-go'
import { rustLanguage } from '@codemirror/lang-rust'
import { javaLanguage } from '@codemirror/lang-java'
import { cppLanguage } from '@codemirror/lang-cpp'
import { StandardSQL } from '@codemirror/lang-sql'
import { basename } from '../utils'

/**
 * File path or code-fence name → CodeMirror language.  Common languages use
 * the tree-based @codemirror/lang-* grammars, which tag function calls and
 * definitions as function(variableName) — that's what the theme's --function
 * rule matches, so calls render blue while variables stay yellow.  The rest
 * fall back to @codemirror/legacy-modes stream grammars, which only emit
 * plain variableName: there, functions and variables share the --variable
 * color.  Both kinds are shared by the editor pane and the console's
 * markdown highlighter (markdown.ts), so both panes agree on what a token
 * means.  Extensions with no grammar at all (markdown, makefile) return
 * null → plain text, no highlighting.
 */

// Tree-based languages (LezerLanguage instances — the lang-* factories wrap
// these in LanguageSupport, which we don't need).
const T = {
  python: pythonLanguage,
  javascript: javascriptLanguage,
  typescript: typescriptLanguage,
  jsx: jsxLanguage,
  tsx: tsxLanguage,
  json: jsonLanguage,
  css: cssLanguage,
  scss: sassLanguage, // lang-sass's grammar accepts both .scss and .sass
  html: htmlLanguage,
  xml: xmlLanguage,
  go: goLanguage,
  rust: rustLanguage,
  java: javaLanguage,
  // lezer-cpp's grammar is a C++ superset, so it parses C too.
  c: cppLanguage,
  cpp: cppLanguage,
  sql: StandardSQL.language,
}

// Legacy stream grammars (StreamLanguage instances).
const L = {
  shell: StreamLanguage.define(shell),
  yaml: StreamLanguage.define(yaml),
  toml: StreamLanguage.define(toml),
  less: StreamLanguage.define(less),
  csharp: StreamLanguage.define(csharp),
  dart: StreamLanguage.define(dart),
  kotlin: StreamLanguage.define(kotlin),
  properties: StreamLanguage.define(properties),
  dockerfile: StreamLanguage.define(dockerFile),
  diff: StreamLanguage.define(diff),
  clojure: StreamLanguage.define(clojure),
  ruby: StreamLanguage.define(ruby),
  lua: StreamLanguage.define(lua),
  perl: StreamLanguage.define(perl),
  r: StreamLanguage.define(r),
  swift: StreamLanguage.define(swift),
  vb: StreamLanguage.define(vb),
  vbscript: StreamLanguage.define(vbScript),
  wast: StreamLanguage.define(wast),
}

// Case-insensitive extension → language.  JSX/TSX get their own tree
// grammars (jsxLanguage/tsxLanguage); vue reuses html, whose parser nests
// <script>/<style> content as JS/CSS, so templates and scripts highlight.
const byExt: Record<string, Language> = {
  py: T.python, pyi: T.python,
  js: T.javascript, mjs: T.javascript, cjs: T.javascript, jsx: T.jsx,
  ts: T.typescript, mts: T.typescript, cts: T.typescript, tsx: T.tsx,
  json: T.json,
  yaml: L.yaml, yml: L.yaml,
  toml: L.toml,
  sh: L.shell, bash: L.shell, zsh: L.shell,
  css: T.css, scss: T.scss, less: L.less,
  html: T.html, htm: T.html, vue: T.html,
  xml: T.xml, svg: T.xml,
  go: T.go,
  rs: T.rust,
  java: T.java,
  c: T.c, h: T.c,
  cpp: T.cpp, cc: T.cpp, cxx: T.cpp, hpp: T.cpp, hh: T.cpp,
  cs: L.csharp,
  kt: L.kotlin, kts: L.kotlin,
  dart: L.dart,
  sql: T.sql,
  ini: L.properties, conf: L.properties,
  diff: L.diff, patch: L.diff,
  clj: L.clojure, cljs: L.clojure,
  rb: L.ruby,
  lua: L.lua, pl: L.perl, r: L.r, swift: L.swift,
  vb: L.vb, vbs: L.vbscript,
  wast: L.wast, // WebAssembly text format (.wasm files are binary — no highlight)
}

// Filename-based lookup for files without an extension.
const byName: Record<string, Language> = {
  dockerfile: L.dockerfile,
}

// Code-fence names → language (console markdown).  Every byExt extension
// also works as a fence name; this adds the aliases highlight.js used to
// accept that aren't file extensions (python, node, golang, …), plus the
// fence-only grammars that have no natural extension.  hljs also accepted
// php/graphql/makefile/markdown/objective-c — those have no grammar, so
// such fences fall back to plain text.
const fenceAliases: Record<string, Language> = {
  python: T.python, python3: T.python, 'python-repl': T.python,
  javascript: T.javascript, node: T.javascript,
  typescript: T.typescript,
  bash: L.shell, zsh: L.shell, shell: L.shell,
  golang: T.go,
  rust: T.rust,
  csharp: L.csharp, 'c#': L.csharp,
  kotlin: L.kotlin,
  'c++': T.cpp,
  docker: L.dockerfile, dockerfile: L.dockerfile,
  properties: L.properties,
  ruby: L.ruby, clojure: L.clojure,
  // hljs's "wasm" is the WebAssembly text format
  wasm: L.wast,
  vbnet: L.vb, vbscript: L.vbscript,
}
const byFence: Record<string, Language> = { ...byExt, ...fenceAliases }

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

/** Language for a code-fence name (console markdown), or null for plain text. */
export function languageForName(name: string | null): Language | null {
  if (!name) return null
  // No trim: markdown-it passes the first whitespace-separated word already.
  return byFence[name.toLowerCase()] ?? null
}
