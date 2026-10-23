<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import type { HistoryItem } from '../types'
import { tr, trf } from '../i18n'
import Icon from './Icon.vue'

const props = defineProps<{ item: HistoryItem; anchor: HTMLElement }>()
const emit = defineEmits<{ close: [restoreFocus?: boolean]; rename: []; pin: []; delete: [files: boolean] }>()
const menu = ref<HTMLElement>()
const position = ref({ left: '8px', top: '8px' })

function outside(event: PointerEvent) {
  if (!menu.value?.contains(event.target as Node) && !props.anchor.contains(event.target as Node)) emit('close')
}
function viewportChanged(event: Event) {
  if (!(event.target instanceof Node) || !menu.value?.contains(event.target)) emit('close')
}
function keydown(event: KeyboardEvent) {
  const items = [...(menu.value?.querySelectorAll<HTMLButtonElement>('[role=menuitem]') || [])]
  const current = items.indexOf(document.activeElement as HTMLButtonElement)
  let next = -1
  if (event.key === 'ArrowDown') next = (current + 1) % items.length
  if (event.key === 'ArrowUp') next = (current - 1 + items.length) % items.length
  if (event.key === 'Home') next = 0
  if (event.key === 'End') next = items.length - 1
  if (next >= 0) { event.preventDefault(); items[next]?.focus() }
  if (event.key === 'Escape' || event.key === 'Tab') {
    event.preventDefault(); event.stopPropagation(); emit('close', true)
  }
}
onMounted(async () => {
  await nextTick()
  const anchor = props.anchor.getBoundingClientRect()
  const rect = menu.value!.getBoundingClientRect()
  position.value = {
    left: `${Math.max(8, Math.min(anchor.left - 8, window.innerWidth - rect.width - 8))}px`,
    top: `${Math.max(8, anchor.bottom + 8 + rect.height <= window.innerHeight - 8 ? anchor.bottom + 8 : anchor.top - rect.height - 8)}px`,
  }
  menu.value?.querySelector<HTMLButtonElement>('[role=menuitem]')?.focus()
  document.addEventListener('pointerdown', outside, true)
  document.addEventListener('scroll', viewportChanged, true)
  window.addEventListener('resize', viewportChanged)
})
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', outside, true)
  document.removeEventListener('scroll', viewportChanged, true)
  window.removeEventListener('resize', viewportChanged)
})
</script>

<template>
  <Teleport to="body">
    <div id="session-actions-menu" ref="menu" class="session-menu" role="menu" :aria-label="trf('{title}的会话操作', { title: item.title_is_default ? tr('新的文档') : item.title })" :style="position" @keydown="keydown">
      <button type="button" role="menuitem" @click="emit('rename')"><Icon name="edit" :size="17" /><span>{{ tr('重命名') }}</span></button>
      <button type="button" role="menuitem" @click="emit('pin')"><Icon :name="item.pinned ? 'unpin' : 'pin'" :size="17" /><span>{{ item.pinned ? tr('取消置顶') : tr('置顶') }}</span></button>
      <div class="session-menu-separator" role="separator" />
      <button type="button" role="menuitem" @click="emit('delete', false)"><Icon name="trash" :size="17" /><span>{{ tr('仅删除对话') }}</span></button>
      <button type="button" role="menuitem" class="is-destructive" @click="emit('delete', true)"><Icon name="trash-files" :size="17" /><span>{{ tr('删除对话和文件') }}</span></button>
    </div>
  </Teleport>
</template>
