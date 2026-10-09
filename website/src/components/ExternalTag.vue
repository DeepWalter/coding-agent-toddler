<script setup lang="ts">
/**
 * The marker an external file wears — the tab strip's name and a tool
 * card's title both append it to the basename: a file outside the served
 * root is identified by its absolute path (see `isExternal`), so the
 * basename alone can be ambiguous, and the file opens read-only.
 */
</script>

<template>
  <span
    class="external-tag"
    title="outside the repository root — read-only"
  >
    <svg
      viewBox="0 0 24 24"
      width="10"
      height="10"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M15 3h6v6" />
      <path d="M10 14 21 3" />
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
    </svg>
    external
  </span>
</template>
