<script setup lang="ts">
withDefaults(defineProps<{ size?: number }>(), { size: 30 })
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 32 32" fill="currentColor" stroke="none" aria-hidden="true" focusable="false">
    <path d="M4 4h6v24H4zM14 4h14L14 18zM14 22 28 8v20H14z" fill="currentColor" />
  </svg>
</template>
