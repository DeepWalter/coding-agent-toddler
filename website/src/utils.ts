/**
 * Tiny path helpers shared across panes (editor tabs, explorer, language
 * lookup) — one copy of the basename logic instead of three.
 */

/** Last path segment; '' for '' (`pop` never returns null for any string). */
export function basename(path: string): string {
  return path.split('/').pop() ?? path
}
