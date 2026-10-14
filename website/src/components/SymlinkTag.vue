<script setup lang="ts">
/**
 * The marker a path reached through a link wears — a tool card's title
 * appends it beside the basename, where the card names one path and a
 * click opens another.  They are one file: the name the call used
 * traverses a symbolic link, and on macOS that is ordinary rather than
 * exotic (`/tmp` is `/private/tmp`, `/home` is `/System/Volumes/...`).
 * Without the marker the two spellings read as a mistake.
 *
 * Its hover names where the file really is — the one thing the marker
 * knows that the card beside it does not say, since the title's own
 * hover already carries the other spelling.
 */
defineProps<{
  /** Where the file is: the resolved path. */
  resolved: string
}>()
</script>

<template>
  <span
    class="symlink-tag"
    :title="`named through a symbolic link — the file is at ${resolved}`"
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
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
    </svg>
    symlink
  </span>
</template>
