import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

export function useSidebarResize() {
  const preferred = ref(232)
  const viewport = ref(window.innerWidth)
  const maximum = computed(() => Math.max(220, Math.min(420, viewport.value - 540)))
  const width = computed(() => Math.max(220, Math.min(maximum.value, preferred.value)))
  const dragging = ref(false)
  let start = 0
  let original = 232
  const save = () => { try { localStorage.setItem('folio.sidebarWidth', String(width.value)) } catch { /* Optional preference. */ } }
  function startResize(event: PointerEvent) {
    if (event.button || viewport.value <= 760) return
    start = event.clientX
    original = width.value
    dragging.value = true
    ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
    event.preventDefault()
  }
  function moveResize(event: PointerEvent) {
    if (dragging.value) preferred.value = Math.max(220, Math.min(maximum.value, original + event.clientX - start))
  }
  function finishResize() { if (dragging.value) { dragging.value = false; save() } }
  function cancelResize() { if (dragging.value) { preferred.value = original; dragging.value = false } }
  function keyResize(event: KeyboardEvent) {
    if (event.key === 'Escape') return cancelResize()
    const step = event.shiftKey ? 32 : 8
    const value: Record<string, number> = { ArrowLeft: width.value - step, ArrowRight: width.value + step, Home: 220, End: maximum.value }
    if (event.key in value) { event.preventDefault(); preferred.value = Math.max(220, Math.min(maximum.value, value[event.key]!)); save() }
  }
  const resize = () => { viewport.value = window.innerWidth; cancelResize() }
  onMounted(() => {
    try { const value = Number(localStorage.getItem('folio.sidebarWidth')); if (value >= 220 && value <= 420) preferred.value = value } catch { /* Optional preference. */ }
    window.addEventListener('resize', resize)
    window.addEventListener('blur', cancelResize)
  })
  onBeforeUnmount(() => { window.removeEventListener('resize', resize); window.removeEventListener('blur', cancelResize) })
  return { width, maximum, viewport, dragging, startResize, moveResize, finishResize, cancelResize, keyResize }
}
