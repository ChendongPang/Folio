import { computed, onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'

const MINIMUM = 280
const DEFAULT = 380
const LIMIT = 820

export function usePreviewResize(sidebarWidth: Readonly<Ref<number>>, open: Readonly<Ref<boolean>>) {
  const preferred = ref(DEFAULT)
  const viewport = ref(window.innerWidth)
  const maximum = computed(() => Math.max(MINIMUM, Math.min(LIMIT, viewport.value - sidebarWidth.value - 420)))
  const width = computed(() => Math.max(MINIMUM, Math.min(
    viewport.value > 1150 ? maximum.value : LIMIT,
    preferred.value,
  )))
  const dragging = ref(false)
  let start = 0
  let original = DEFAULT
  let originalPreference = DEFAULT
  let capture: { element: HTMLElement; pointerId: number } | undefined

  function release() {
    const current = capture
    capture = undefined
    if (current?.element.hasPointerCapture(current.pointerId)) current.element.releasePointerCapture(current.pointerId)
  }
  function save() {
    try { localStorage.setItem('folio.previewWidth', String(preferred.value)) } catch { /* Optional preference. */ }
  }
  function startResize(event: PointerEvent) {
    if (event.button !== 0 || !event.isPrimary || viewport.value <= 1150 || !open.value) return
    original = width.value
    originalPreference = preferred.value
    start = event.clientX
    dragging.value = true
    const element = event.currentTarget as HTMLElement
    capture = { element, pointerId: event.pointerId }
    element.setPointerCapture(event.pointerId)
    element.focus({ preventScroll: true })
    event.preventDefault()
  }
  function moveResize(event: PointerEvent) {
    if (dragging.value && capture?.pointerId === event.pointerId) {
      preferred.value = Math.max(MINIMUM, Math.min(maximum.value, original + start - event.clientX))
    }
  }
  function finishResize() {
    if (!dragging.value) return
    dragging.value = false
    release()
    save()
  }
  function cancelResize() {
    if (!dragging.value) return
    preferred.value = originalPreference
    dragging.value = false
    release()
  }
  function keyResize(event: KeyboardEvent) {
    if (event.key === 'Escape' && dragging.value) {
      event.preventDefault()
      event.stopPropagation()
      cancelResize()
      return
    }
    if (viewport.value <= 1150 || !open.value) return
    const step = event.shiftKey ? 32 : 8
    const values: Record<string, number> = {
      ArrowLeft: width.value + step, ArrowRight: width.value - step, Home: MINIMUM, End: maximum.value,
    }
    if (event.key in values) {
      event.preventDefault()
      preferred.value = Math.max(MINIMUM, Math.min(maximum.value, values[event.key]!))
      save()
    }
  }
  function resize() { cancelResize(); viewport.value = window.innerWidth }
  watch(open, value => { if (!value) cancelResize() })
  onMounted(() => {
    try {
      const value = Number(localStorage.getItem('folio.previewWidth'))
      if (Number.isFinite(value) && value >= MINIMUM && value <= LIMIT) preferred.value = value
    } catch { /* Optional preference. */ }
    window.addEventListener('resize', resize)
    window.addEventListener('blur', cancelResize)
  })
  onBeforeUnmount(() => {
    cancelResize()
    window.removeEventListener('resize', resize)
    window.removeEventListener('blur', cancelResize)
  })
  return { width, minimum: MINIMUM, maximum, viewport, dragging, startResize, moveResize, finishResize, cancelResize, keyResize }
}
