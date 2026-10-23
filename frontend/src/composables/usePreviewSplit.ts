import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const HANDLE_HEIGHT = 12
const MINIMUM_PANE_HEIGHT = 112
const DEFAULT_RATIO = .44
const STORAGE_KEY = 'folio.previewSplit'

export function usePreviewSplit() {
  const panes = ref<HTMLElement | null>(null)
  const availableHeight = ref(0)
  const preferredRatio = ref(DEFAULT_RATIO)
  const dragging = ref(false)
  const minimumHeight = computed(() => Math.min(MINIMUM_PANE_HEIGHT, availableHeight.value / 2))
  const maximumHeight = computed(() => availableHeight.value - minimumHeight.value)
  const upperHeight = computed(() => Math.max(minimumHeight.value, Math.min(maximumHeight.value, availableHeight.value * preferredRatio.value)))
  const ratio = computed(() => availableHeight.value ? upperHeight.value / availableHeight.value : DEFAULT_RATIO)
  const splitStyle = computed(() => ({ gridTemplateRows: `minmax(0, ${ratio.value}fr) ${HANDLE_HEIGHT}px minmax(0, ${1 - ratio.value}fr)` }))
  const percentage = computed(() => Math.round(ratio.value * 100))
  const minimumPercentage = computed(() => availableHeight.value ? Math.round(minimumHeight.value / availableHeight.value * 100) : 0)
  const maximumPercentage = computed(() => 100 - minimumPercentage.value)
  let observer: ResizeObserver | undefined
  let handle: HTMLElement | null = null
  let pointerId: number | null = null
  let startY = 0
  let startHeight = 0
  let originalRatio = DEFAULT_RATIO

  function save() {
    try { localStorage.setItem(STORAGE_KEY, String(preferredRatio.value)) } catch { /* Optional preference. */ }
  }

  function releasePointer() {
    dragging.value = false
    document.body.classList.remove('is-preview-split-resizing')
    if (handle && pointerId !== null && handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId)
    handle = null
    pointerId = null
  }

  function cancelResize() {
    if (!dragging.value) return
    preferredRatio.value = originalRatio
    releasePointer()
  }

  function measure() {
    const height = Math.max(0, (panes.value?.clientHeight ?? 0) - HANDLE_HEIGHT)
    if (height !== availableHeight.value) {
      cancelResize()
      availableHeight.value = height
    }
  }

  function setHeight(height: number) {
    if (availableHeight.value <= 0) return
    preferredRatio.value = Math.max(minimumHeight.value, Math.min(maximumHeight.value, height)) / availableHeight.value
  }

  function startResize(event: PointerEvent) {
    if (event.button !== 0 || dragging.value) return
    measure()
    if (!availableHeight.value) return
    handle = event.currentTarget as HTMLElement
    pointerId = event.pointerId
    startY = event.clientY
    startHeight = upperHeight.value
    originalRatio = preferredRatio.value
    dragging.value = true
    handle.focus({ preventScroll: true })
    handle.setPointerCapture(event.pointerId)
    document.body.classList.add('is-preview-split-resizing')
    event.preventDefault()
  }

  function moveResize(event: PointerEvent) {
    if (dragging.value && event.pointerId === pointerId) setHeight(startHeight + event.clientY - startY)
  }

  function finishResize(event: PointerEvent) {
    if (!dragging.value || event.pointerId !== pointerId) return
    releasePointer()
    save()
  }

  function keyResize(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      if (dragging.value) { event.preventDefault(); event.stopPropagation(); cancelResize() }
      return
    }
    const step = event.shiftKey ? 48 : 16
    const heights: Record<string, number> = {
      ArrowUp: upperHeight.value - step,
      ArrowDown: upperHeight.value + step,
      Home: minimumHeight.value,
      End: maximumHeight.value,
    }
    if (event.key in heights) {
      event.preventDefault()
      cancelResize()
      setHeight(heights[event.key]!)
      save()
    }
  }

  function cancelOnEscape(event: KeyboardEvent) {
    if (event.key === 'Escape' && dragging.value) { event.preventDefault(); event.stopImmediatePropagation(); cancelResize() }
  }

  function resetSplit() {
    cancelResize()
    preferredRatio.value = DEFAULT_RATIO
    save()
  }

  function resize() { cancelResize(); measure() }

  onMounted(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY)
      const value = stored === null ? NaN : Number(stored)
      if (Number.isFinite(value) && value > 0 && value < 1) preferredRatio.value = value
    } catch { /* Optional preference. */ }
    measure()
    observer = new ResizeObserver(measure)
    if (panes.value) observer.observe(panes.value)
    window.addEventListener('resize', resize)
    window.addEventListener('blur', cancelResize)
    window.addEventListener('keydown', cancelOnEscape, true)
  })

  onBeforeUnmount(() => {
    cancelResize()
    observer?.disconnect()
    window.removeEventListener('resize', resize)
    window.removeEventListener('blur', cancelResize)
    window.removeEventListener('keydown', cancelOnEscape, true)
  })

  return { panes, dragging, splitStyle, percentage, minimumPercentage, maximumPercentage, startResize, moveResize, finishResize, cancelResize, keyResize, resetSplit }
}
