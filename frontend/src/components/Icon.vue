<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 20 })
const paths: Record<string, string> = {
  plus: 'M12 5v14M5 12h14',
  chat: 'M21 11.5a8.5 8.5 0 0 1-8.5 8.5H5l-3 2v-10a9.5 9.5 0 0 1 19 0ZM7 9h8M7 13h5',
  file: 'M14 3H5v18h14V8l-5-5ZM14 3v5h5M8 12h8M8 16h5',
  upload: 'M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6',
  arrow: 'M12 20V4m-6 6 6-6 6 6',
  right: 'M4 12h16m-6-6 6 6-6 6',
  check: 'm5 12 4 4L19 6',
  close: 'm6 6 12 12M18 6 6 18',
  trash: 'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
  'trash-files': 'M5 6H3v15h12v-2M9 3h9l3 3v11H9V3ZM17 10l-4 4m0-4 4 4',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
  pin: 'm16 3 5 5-3 1-4 4v4l-7-7h4l4-4 1-3ZM7 17l-4 4',
  unpin: 'm16 3 5 5-3 1-2 2M11 6l-2 4H7l7 7v-4M7 17l-4 4M3 3l18 18',
  down: 'm8 10 4 4 4-4',
  download: 'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',
  book: 'M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1Zm0 0v15',
  menu: 'M4 6h16M4 12h16M4 18h16',
  stop: 'M6 6h12v12H6Z',
  play: 'm8 5 11 7-11 7Z',
  settings: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2',
  sparkle: 'm12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6L12 3ZM20 2v4m-2-2h4',
  grid: 'M3 3h7v7H3ZM14 3h7v7h-7ZM3 14h7v7H3ZM14 14h7v7h-7Z',
  layers: 'm12 3 10 5-10 5L2 8l10-5ZM2 12l10 5 10-5M2 16l10 5 10-5',
  search: 'M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15ZM16 16l5 5',
  clock: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM12 7v5l3 2',
  shield: 'm12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6l8-4Zm-4 10 3 3 5-6',
  warning: 'm12 3 10 18H2L12 3ZM12 9v5m0 3v.1',
  panel: 'M3 4h18v16H3ZM9 4v16',
  edit: 'm16 3 5 5-12 12-6 1 1-6L16 3ZM13 6l5 5',
}
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="paths[name] || paths.file" /></svg>
</template>
