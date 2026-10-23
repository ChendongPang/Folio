<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useWorkspace } from './composables/useWorkspace'
import { useSidebarResize } from './composables/useSidebarResize'
import { usePreviewResize } from './composables/usePreviewResize'
import Icon from './components/Icon.vue'
import BrandMark from './components/BrandMark.vue'
import MessageList from './components/MessageList.vue'
import ModelSettings from './components/ModelSettings.vue'
import DocumentPreview from './components/DocumentPreview.vue'
import MaterialsPanel from './components/MaterialsPanel.vue'
import ConfirmationPanel from './components/ConfirmationPanel.vue'
import SessionMenu from './components/SessionMenu.vue'
import type { HistoryItem } from './types'
import { language, localizeRuntimeText, setLanguage, tr } from './i18n'

const w = useWorkspace()
const s = w.state
const rail = useSidebarResize()
const previewRail = usePreviewResize(rail.width, computed(() => s.previewOpen))
const settings = ref<InstanceType<typeof ModelSettings>>()
const model = reactive({ configured: false, model: '' })
const input = ref<HTMLTextAreaElement>()
const scroller = ref<HTMLElement>()
const fileInput = ref<HTMLInputElement>()
const draggingFiles = ref(false)
const deleteDialog = ref<HTMLDialogElement>()
const deleteTarget = ref<{ id: string; title: string; files: boolean } | null>(null)
const deleteError = ref('')
const menuTarget = ref<HistoryItem | null>(null)
const menuAnchor = ref<HTMLElement>()
const renameDialog = ref<HTMLDialogElement>()
const renameInput = ref<HTMLInputElement>()
const renameTarget = ref<HistoryItem | null>(null)
const renameTitle = ref('')
const renameError = ref('')
const sessionBusy = computed(() => s.restoring || !!s.deletingConversation || !!s.updatingConversation)
let dragDepth = 0
const locked = computed(() => s.running || s.restoring || s.restoreFailed)
const step = computed(() => s.activeJob || s.directoryReview?.phase === 'generating' ? 3 : s.directoryReview?.directory ? 2 : s.files.length || s.messages.length ? 1 : 0)
const prompt = computed(() => s.activeJob
  ? s.activeJobStatus === 'paused' ? tr('生成已停止，点击右侧继续') : tr('文档正在生成，可随时切换会话查看其他任务')
  : w.reviewing.value ? tr('继续询问资料或文档内容…') : tr('告诉我你想生成什么文档，以及用途和要求…'))
const suggestions = [
  { title: '标书', zh: '基于已上传的资料，生成一份完整标书，重点写清技术方案、实施计划、服务保障和验收承诺。先给我完整目录确认。', en: 'Using the uploaded sources, create a complete bid proposal covering the technical solution, implementation plan, service commitments and acceptance terms. Show me the complete outline for confirmation first.' },
  { title: '需求说明书', zh: '基于已上传的资料，生成一份面向项目团队的需求说明书，重点写清业务流程与规则。先给我完整目录确认。', en: 'Using the uploaded sources, create a requirements specification for the project team, focusing on business processes and rules. Show me the complete outline for confirmation first.' },
  { title: '研究报告', zh: '基于已上传的资料，生成一份研究报告，梳理背景、关键发现、分析结论与建议。先给我完整目录确认。', en: 'Using the uploaded sources, create a research report covering the background, key findings, analysis and recommendations. Show me the complete outline for confirmation first.' },
]
const conversationTitle = computed(() => s.titleIsLabel || !s.title ? tr('新的文档') : s.title)
function historyTitle(item: HistoryItem) { return item.title_is_default ? tr('新的文档') : item.title }
const uploading = computed(() => s.files.some(file => file.queued || file.uploading))
const uploadedCount = computed(() => s.files.filter(file => file.server_id && !file.failed).length)
const filesReady = computed(() => !s.files.some(file => file.queued || file.uploading || file.failed || file.removing))
const saveFeedback = computed(() => {
  if (s.saveStatus === '已保存') return { tone: 'success', icon: 'check' }
  if (s.saveStatus === '保存失败 · 草稿暂存本机') return { tone: 'error', icon: 'warning' }
  if (s.saveStatus === '草稿待同步') return { tone: 'warning', icon: 'clock' }
  return { tone: 'neutral', icon: 'clock' }
})
function chooseFiles() { fileInput.value?.click() }
function changeLanguage(event: Event) {
  setLanguage((event.target as HTMLSelectElement).value === 'en' ? 'en' : 'zh-CN')
}
function receiveFiles(event: Event) {
  const element = event.target as HTMLInputElement
  const files = [...(element.files || [])]
  element.value = ''
  void w.addFiles(files)
}
function editText() {
  w.scheduleDraft()
  if (input.value) { input.value.style.height = 'auto'; input.value.style.height = `${Math.min(input.value.scrollHeight, 160)}px` }
}
function suggest(text: string) { s.text = text; editText(); input.value?.focus() }
function inputKey(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); void w.send('chat') }
}
function updateModel(value: { configured: boolean; model?: string }) { model.configured = value.configured; model.model = value.model || '' }
function openSessionMenu(item: HistoryItem, event: Event) {
  if (sessionBusy.value) return
  if (menuTarget.value?.id === item.id) { closeSessionMenu(); return }
  menuAnchor.value = event.currentTarget as HTMLElement
  menuTarget.value = item
}
function closeSessionMenu(restoreFocus = false) {
  menuTarget.value = null
  if (restoreFocus) menuAnchor.value?.focus()
}
async function toggleSessionPin(item: HistoryItem) {
  closeSessionMenu(true)
  await w.toggleConversationPin(item)
}
async function requestRenameConversation(item: HistoryItem) {
  if (sessionBusy.value) return
  closeSessionMenu(true)
  renameTarget.value = item
  renameTitle.value = historyTitle(item)
  renameError.value = ''
  renameDialog.value?.showModal()
  await nextTick()
  renameInput.value?.focus()
  renameInput.value?.select()
}
function cancelRenameConversation(event?: Event) {
  if (s.updatingConversation) { event?.preventDefault(); return }
  renameDialog.value?.close()
}
async function confirmRenameConversation() {
  if (!renameTarget.value || sessionBusy.value || !renameTitle.value.trim()) return
  renameError.value = ''
  if (await w.renameConversation(renameTarget.value.id, renameTitle.value.trim())) renameDialog.value?.close()
  else renameError.value = s.toast || '重命名失败，请稍后重试。'
}
function requestDeleteConversation(item: HistoryItem, files = false) {
  if (sessionBusy.value) return
  closeSessionMenu(true)
  deleteTarget.value = { id: item.id, title: historyTitle(item), files }
  deleteError.value = ''
  deleteDialog.value?.showModal()
}
function cancelDeleteConversation(event?: Event) {
  if (s.deletingConversation) { event?.preventDefault(); return }
  deleteDialog.value?.close()
}
async function confirmDeleteConversation() {
  if (!deleteTarget.value || s.deletingConversation) return
  deleteError.value = ''
  if (await w.deleteConversation(deleteTarget.value.id, deleteTarget.value.files)) {
    deleteDialog.value?.close()
    await nextTick()
    if (document.activeElement === document.body) {
      document.querySelector<HTMLButtonElement>('.session-item.is-active, #new-chat')?.focus()
    }
  } else {
    deleteError.value = s.toast || '删除失败，请稍后重试。'
  }
}
function dragEnter(event: DragEvent) { if (event.dataTransfer?.types.includes('Files')) { event.preventDefault(); dragDepth++; draggingFiles.value = true } }
function dragOver(event: DragEvent) { if (event.dataTransfer?.types.includes('Files')) event.preventDefault() }
function dragLeave(event: DragEvent) { if (event.dataTransfer?.types.includes('Files')) { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) draggingFiles.value = false } }
function drop(event: DragEvent) {
  if (!event.dataTransfer?.types.includes('Files')) return
  event.preventDefault(); dragDepth = 0; draggingFiles.value = false
  void w.addFiles([...event.dataTransfer.files])
}
function escape(event: KeyboardEvent) {
  if (event.key !== 'Escape' || deleteDialog.value?.open || renameDialog.value?.open) return
  if (menuTarget.value) { closeSessionMenu(true); return }
  s.sidebarOpen = false; s.materialsOpen = false; s.previewOpen = false; draggingFiles.value = false; dragDepth = 0
}
watch(() => {
  const last = s.messages.at(-1)
  // Replaying finished history must not move the conversation on every row.
  return [s.messages.length, last?.text, last?.working ? last.activities.length : undefined,
    last?.working ? last.activities.at(-1)?.text : undefined, last?.working ? last.activities.at(-1)?.detail : undefined,
    last?.directoryReview?.phase]
}, async () => {
  const element = scroller.value
  const near = element && element.scrollHeight - element.scrollTop - element.clientHeight < 240
  await nextTick()
  if (element && near) element.scrollTo({ top: element.scrollHeight, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' })
})
watch(() => s.conversationId, () => { closeSessionMenu(); if (scroller.value) scroller.value.scrollTop = 0 })
watch(() => s.sidebarOpen, () => closeSessionMenu())
watch([() => s.directoryReview?.revision, () => rail.viewport.value <= 1150], () => {
  // Keep the confirmation controls visible when the preview would cover the conversation.
  if (rail.viewport.value <= 1150) s.previewOpen = false
})
onMounted(() => {
  document.addEventListener('dragenter', dragEnter); document.addEventListener('dragover', dragOver)
  document.addEventListener('dragleave', dragLeave); document.addEventListener('drop', drop)
  document.addEventListener('keydown', escape)
})
onBeforeUnmount(() => {
  document.removeEventListener('dragenter', dragEnter); document.removeEventListener('dragover', dragOver)
  document.removeEventListener('dragleave', dragLeave); document.removeEventListener('drop', drop)
  document.removeEventListener('keydown', escape)
})
</script>

<template>
  <div class="app-shell" :class="{ 'sidebar-resizing': rail.dragging.value, 'preview-resizing': previewRail.dragging.value, 'preview-open': s.previewOpen }" :style="{ '--sidebar-width': `${rail.width.value}px`, '--preview-width': `${previewRail.width.value}px` }" :data-ready="!s.restoring">
    <aside id="sidebar" class="sidebar" :class="{ 'is-open': s.sidebarOpen }" :inert="rail.viewport.value <= 760 && !s.sidebarOpen" :aria-label="tr('工作台导航')">
      <a class="brand" href="/" :aria-label="tr('Folio 首页')"><span class="brand-symbol" aria-hidden="true"><BrandMark :size="32" /></span><span class="brand-wordmark">Folio</span></a>
      <button id="new-chat" class="new-chat" :disabled="sessionBusy" @click="w.switchConversation(null)"><Icon name="plus" :size="18" />{{ tr('新建文档') }}<span class="new-chat-mark" aria-hidden="true">↗</span></button>
      <div class="sidebar-label history-label"><span>EXPLORER</span><span class="history-count">{{ String(s.historyItems.length).padStart(2, '0') }}</span></div>
      <nav id="session-list" class="session-list" :aria-label="tr('历史会话')">
        <p v-if="!s.historyItems.length && !s.restoring" class="history-empty">{{ tr('暂无会话') }}<span>{{ tr('你的下一份文档，从这里开始。') }}</span></p>
        <div v-for="item in s.historyItems" :key="item.id" class="session-row" :class="{ 'is-active': item.id === s.conversationId, 'is-menu-open': menuTarget?.id === item.id, 'is-pinned': item.pinned }">
          <button class="session-item" :class="{ 'is-active': item.id === s.conversationId }" :title="historyTitle(item)" :aria-current="item.id === s.conversationId ? 'page' : 'false'" :disabled="sessionBusy" @click="w.switchConversation(item.id)"><span class="session-title">{{ historyTitle(item) }}</span></button>
          <div class="session-actions">
            <button type="button" class="session-more" :aria-label="`${tr('会话操作')}：${historyTitle(item)}`" :title="tr('会话操作')" aria-haspopup="menu" :aria-expanded="menuTarget?.id === item.id" :aria-controls="menuTarget?.id === item.id ? 'session-actions-menu' : undefined" :disabled="sessionBusy" @click="openSessionMenu(item, $event)"><Icon name="more" :size="17" /></button>
            <button type="button" class="session-pin" :aria-label="`${item.pinned ? tr('取消置顶') : tr('置顶')}：${historyTitle(item)}`" :title="item.pinned ? tr('取消置顶') : tr('置顶')" :aria-pressed="!!item.pinned" :disabled="sessionBusy" @click="toggleSessionPin(item)"><Icon name="pin" :size="15" /></button>
          </div>
        </div>
      </nav>
      <div v-if="s.historyError" id="history-error" class="history-error">{{ tr('无法读取会话列表') }}<button id="history-retry" class="text-button" @click="w.refreshHistory()">{{ tr('重试') }}</button></div>
      <button v-if="s.historyMore" id="history-more" class="text-button history-more" @click="w.refreshHistory(true)">{{ tr('加载更多会话') }}</button>
      <div class="sidebar-bottom">
        <div class="sidebar-signature" aria-hidden="true"><span>LESS, BUT BETTER.</span><span class="signature-rule" /></div>
        <button id="model-settings-open" class="model-settings-trigger" aria-haspopup="dialog" :title="tr('模型设置')" @click="settings?.open()"><span class="settings-icon"><Icon name="settings" :size="18" /></span><span class="model-status-copy"><strong>{{ tr('模型设置') }}</strong><small id="model-status-label">{{ model.configured ? model.model : tr('配置模型以开始') }}</small></span><span id="model-status-dot" class="status-dot" :class="{ configured: model.configured }" /></button>
      </div>
      <div id="sidebar-resize-handle" class="sidebar-resize-handle" role="separator" :aria-label="tr('调整历史会话宽度')" aria-orientation="vertical" aria-controls="sidebar" :aria-valuenow="rail.width.value" :aria-valuemin="220" :aria-valuemax="rail.maximum.value" :tabindex="rail.viewport.value > 760 ? 0 : -1" @pointerdown="rail.startResize" @pointermove="rail.moveResize" @pointerup="rail.finishResize" @pointercancel="rail.cancelResize" @lostpointercapture="rail.finishResize" @keydown="rail.keyResize" />
    </aside>
    <button v-if="s.sidebarOpen" id="sidebar-scrim" class="sidebar-scrim" :aria-label="tr('关闭导航')" @click="s.sidebarOpen = false" />
    <main class="workspace" :class="{ 'is-empty': !s.messages.length }">
      <header class="topbar">
        <div class="breadcrumb"><button id="menu-button" class="icon-button mobile-menu" :aria-label="tr('打开导航')" :aria-expanded="s.sidebarOpen" @click="s.sidebarOpen = !s.sidebarOpen"><Icon name="menu" /></button><span class="breadcrumb-parent">workspace</span><span class="slash">/</span><strong id="conversation-title">{{ conversationTitle }}</strong></div>
        <div class="top-actions"><label class="language-setting" :aria-label="tr('语言')"><span>{{ tr('语言') }}</span><select id="language-select" :value="language" @change="changeLanguage"><option value="zh-CN">{{ tr('简体中文') }}</option><option value="en">English</option></select></label><span id="session-save-status" class="sr-only" :data-tone="saveFeedback.tone" role="status"><Icon :name="saveFeedback.icon" :size="13" />{{ tr(s.saveStatus) }}</span><span class="top-divider" /><button id="materials-toggle" class="materials-toggle" :aria-label="`${s.materialsOpen ? tr('关闭') : tr('打开')} ${tr('资料库')}，${s.files.length} ${tr('份资料')}`" :class="{ active: s.materialsOpen }" :aria-expanded="s.materialsOpen" aria-controls="materials-panel" @click="s.materialsOpen = !s.materialsOpen"><Icon name="file" :size="16" /><span>{{ tr('资料库') }}</span><span id="file-count" class="count-badge">{{ s.files.length }}</span></button><button class="icon-button preview-toggle" :aria-label="tr('打开文档预览')" :aria-expanded="s.previewOpen" @click="s.previewOpen = !s.previewOpen"><Icon name="panel" :size="18" /></button></div>
      </header>
      <div class="workspace-section-bar"><span class="workspace-section-label"><span class="section-index">01</span> {{ tr('文档工作台') }} <span class="section-slash">/</span> <span class="section-english">DOCUMENT WORKSPACE</span></span></div>
      <div v-if="s.restoreFailed" id="restore-failure" class="restore-failure" role="alert"><Icon name="warning" :size="18" /><span>{{ tr('会话暂时未能加载，历史内容仍保存在后台。') }}</span><button id="restore-retry" class="text-button" @click="w.switchConversation(s.conversationId)">{{ tr('重新加载') }}</button></div>
      <div id="conversation-scroll" ref="scroller" class="conversation-scroll">
        <section v-if="!s.messages.length" id="welcome" class="welcome">
          <div class="welcome-eyebrow"><span class="eyebrow-marker" /> A LITTLE LESS CHAOS. A LOT MORE CLARITY.</div>
          <div class="welcome-heading">
            <div class="welcome-copy"><h1>Sources in.<br><span>Docs out<span class="terminal-cursor" aria-hidden="true">_</span><span class="sr-only">.</span></span></h1><p class="welcome-description">{{ tr('汇集资料，通过对话构建你的文档。') }}</p></div>
            <div class="document-schema" aria-hidden="true">
              <div class="schema-top"><span>INPUT / STRUCTURE</span><span>↗</span></div>
              <div class="schema-body"><span class="schema-axis" /><div class="schema-node schema-root"><span class="node-square" /><span>sources</span><span class="node-extension">/</span></div><div class="schema-branch"><span class="branch-elbow" /><span class="node-square" /><span>request</span></div><div class="schema-branch"><span class="branch-elbow" /><span class="node-square" /><span>directory</span></div><div class="schema-branch schema-result"><span class="branch-elbow" /><span class="node-square" /><span>document</span><span class="node-extension">.docx</span></div></div>
              <div class="schema-bottom"><span>RAW → REFINED</span><span class="schema-cross">+</span></div>
            </div>
          </div>
          <div class="welcome-start"><span class="start-label">{{ tr('从这里开始') }}</span><div class="suggestion-grid"><button v-for="(item, index) in suggestions" :key="item.title" class="suggestion-card" :disabled="locked" @click="suggest(language === 'en' ? item.en : item.zh)"><span class="suggestion-index">0{{ index + 1 }}</span><span>{{ tr(item.title) }}</span><span class="suggestion-arrow" aria-hidden="true">↗</span></button></div></div>
        </section>
        <MessageList :workspace="w" />
        <ConfirmationPanel :workspace="w" />
      </div>
      <section id="composer-area" class="composer-area" :aria-label="tr('消息输入区')">
        <div v-if="!s.messages.length" class="source-context">
          <div class="source-context-copy"><Icon name="layers" :size="19" /><div><strong>{{ tr('资料库') }} <span>{{ uploadedCount }} {{ tr('份') }}</span></strong><small>{{ uploading ? `${tr('正在上传')} · ${uploadedCount} / ${s.files.length}` : tr('Word、Excel、PDF、PPTX、文本及图片') }}</small></div></div>
          <button id="upload-sources" class="secondary-button" :disabled="locked" @click="chooseFiles"><Icon name="plus" :size="14" />{{ tr('上传资料') }}</button>
        </div>
        <div v-if="s.running && s.files.length" class="composer-sources"><button class="text-button" @click="s.materialsOpen = true"><Icon name="file" :size="14" />{{ s.files.length }} {{ tr('份资料') }}<Icon name="right" :size="12" /></button></div>
        <div v-else id="staged-files" class="staged-files">
          <div v-for="file in s.files.slice(0, 3)" :key="file.id" class="file-chip" :class="{ 'is-failed': file.failed }"><Icon name="file" :size="15" /><span :title="file.file.name">{{ file.file.name }}</span><span v-if="file.uploading" class="tiny-spinner" /><span v-else-if="file.queued" class="file-state">{{ tr('排队中') }}</span><span v-else-if="file.failed" class="file-error">{{ tr('上传失败') }}</span><button class="icon-button" :aria-label="`${tr('移除')} ${file.file.name}`" :disabled="locked || file.uploading || file.removing" @click="w.removeFile(file.id)"><Icon name="close" :size="13" /></button></div>
          <button v-if="s.files.length > 3" class="source-more text-button" @click="s.materialsOpen = true">{{ tr('查看全部') }} {{ s.files.length }} {{ tr('份资料') }}<Icon name="right" :size="12" /></button>
        </div>
        <form id="composer" class="composer" :class="{ 'is-busy': s.running }" @submit.prevent="w.send('chat')">
          <label class="sr-only" for="message-input">{{ tr('告诉我文档要求') }}</label>
          <textarea id="message-input" ref="input" v-model="s.text" rows="2" :disabled="locked" :placeholder="prompt" maxlength="600000" @input="editText" @keydown="inputKey" />
          <div class="composer-toolbar">
            <div class="composer-tools"><button id="attach-button" type="button" class="attach-button" :title="tr('添加资料')" :disabled="locked" @click="chooseFiles"><Icon name="plus" :size="19" /><span>{{ tr('添加资料') }}</span></button></div>
            <div class="composer-send">
              <button v-if="!s.running" id="send-button" class="send-button" type="submit" :aria-label="tr('发送消息')" :title="tr('发送消息')" :disabled="locked || !filesReady || !s.text.trim()"><Icon name="arrow" :size="20" /></button>
              <button v-else-if="!s.generating || s.activeJob" id="stop-button" class="send-button stop-button" type="button" :aria-label="w.controlLabel.value" :title="w.controlLabel.value" :disabled="s.taskControlPending || s.activeJobStatus === 'pausing' || s.restoring" @click="w.stop()">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                  <path v-if="s.activeJobStatus === 'paused'" d="M9 5.9a1 1 0 0 1 1.5-.86l9 5.2a2 2 0 0 1 0 3.46l-9 5.2A1 1 0 0 1 9 18.04Z" />
                  <template v-else-if="s.activeJob"><rect x="7" y="5" width="3.5" height="14" rx="1.25" /><rect x="13.5" y="5" width="3.5" height="14" rx="1.25" /></template>
                  <rect v-else x="6" y="6" width="12" height="12" rx="2" />
                </svg>
              </button>
            </div>
          </div>
        </form>
        <div class="composer-footnote"><button id="upload-zone" class="upload-inline" :disabled="locked" @click="chooseFiles"><Icon name="upload" :size="12" /><span>{{ tr('可一次拖入多个文件') }}</span></button><span><kbd>Enter</kbd> {{ tr('发送') }} · <kbd>Shift ↵</kbd> {{ tr('换行') }}</span></div>
      </section>
    </main>
    <DocumentPreview :workspace="w" :class="{ 'is-open': s.previewOpen }" @close="s.previewOpen = false">
      <template #resize-handle>
        <div id="preview-resize-handle" class="preview-resize-handle" role="separator" :aria-label="tr('调整文档预览宽度')" aria-orientation="vertical" aria-controls="document-preview" :aria-valuenow="previewRail.width.value" :aria-valuemin="previewRail.minimum" :aria-valuemax="previewRail.maximum.value" :tabindex="previewRail.viewport.value > 1150 && s.previewOpen ? 0 : -1" @pointerdown="previewRail.startResize" @pointermove="previewRail.moveResize" @pointerup="previewRail.finishResize" @pointercancel="previewRail.cancelResize" @lostpointercapture="previewRail.finishResize" @keydown="previewRail.keyResize" />
      </template>
    </DocumentPreview>
    <div class="journey-rail" :aria-label="tr('文档流程')"><span v-for="(label, index) in ['收集资料', '描述需求', '确认目录', '生成文档']" :key="label" :class="{ current: index === step, complete: index < step }"><i><Icon v-if="index < step" name="check" :size="11" /><template v-else>{{ String(index + 1).padStart(2, '0') }}</template></i>{{ tr(label) }}<b v-if="index < 3" /></span><span class="journey-caption">BUILT FOR CLARITY<span class="status-cross" aria-hidden="true">+</span></span></div>
    <Transition name="slide"><MaterialsPanel v-if="s.materialsOpen" :workspace="w" @upload="chooseFiles" /></Transition>
    <ModelSettings ref="settings" @status="updateModel" />
    <SessionMenu v-if="menuTarget && menuAnchor" :key="menuTarget.id" :item="menuTarget" :anchor="menuAnchor" @close="closeSessionMenu" @rename="requestRenameConversation(menuTarget!)" @pin="toggleSessionPin(menuTarget!)" @delete="requestDeleteConversation(menuTarget!, $event)" />
    <dialog ref="renameDialog" id="rename-conversation-dialog" class="rename-conversation-dialog" aria-labelledby="rename-conversation-title" :aria-busy="!!s.updatingConversation" @cancel="cancelRenameConversation" @close="renameTarget = null">
      <form @submit.prevent="confirmRenameConversation">
        <h2 id="rename-conversation-title">{{ tr('重命名会话') }}</h2>
        <label for="rename-conversation-input">{{ tr('会话名称') }}</label>
        <input id="rename-conversation-input" ref="renameInput" v-model="renameTitle" type="text" maxlength="200" :disabled="!!s.updatingConversation" autocomplete="off" autofocus>
        <p v-if="renameError" class="delete-conversation-error" role="alert">{{ localizeRuntimeText(renameError) }}</p>
        <div class="delete-conversation-actions">
          <button id="cancel-rename-conversation" type="button" class="secondary-button" :disabled="!!s.updatingConversation" @click="cancelRenameConversation">{{ tr('取消') }}</button>
          <button id="confirm-rename-conversation" type="submit" class="rename-confirm-button" :disabled="!!s.updatingConversation || !renameTitle.trim()">{{ s.updatingConversation ? tr('保存中…') : tr('保存') }}</button>
        </div>
      </form>
    </dialog>
    <dialog ref="deleteDialog" id="delete-conversation-dialog" class="delete-conversation-dialog" aria-labelledby="delete-conversation-title" aria-describedby="delete-conversation-description" :aria-busy="!!s.deletingConversation" @cancel="cancelDeleteConversation" @close="deleteTarget = null">
      <span class="delete-dialog-icon" aria-hidden="true"><Icon name="trash" :size="22" /></span>
      <h2 id="delete-conversation-title">{{ deleteTarget?.files ? tr('删除对话和文件？') : tr('仅删除对话？') }}</h2>
      <p class="delete-conversation-name">{{ deleteTarget?.title }}</p>
      <p id="delete-conversation-description">{{ deleteTarget?.files ? tr('将删除这段对话，以及仅属于它的上传文件和生成文档，无法恢复。其他对话或任务使用的文件及共用缓存会保留。') : tr('将删除这段对话及聊天记录，无法恢复。已上传的文件和已生成的文档会保留。') }}</p>
      <p v-if="deleteError" id="delete-conversation-error" class="delete-conversation-error" role="alert">{{ localizeRuntimeText(deleteError) }}</p>
      <div class="delete-conversation-actions">
        <button id="cancel-delete-conversation" type="button" class="secondary-button" :disabled="!!s.deletingConversation" autofocus @click="cancelDeleteConversation">{{ tr('取消') }}</button>
        <button id="confirm-delete-conversation" type="button" class="delete-confirm-button" :disabled="!!s.deletingConversation" @click="confirmDeleteConversation">{{ s.deletingConversation ? tr('删除中…') : tr('删除会话') }}</button>
      </div>
    </dialog>
    <input id="file-input" ref="fileInput" type="file" accept=".docx,.md,.txt,.xlsx,.pdf,.pptx,.png,.jpg,.jpeg,.tif,.tiff,.webp" multiple hidden @change="receiveFiles">
    <Transition name="fade"><div v-if="s.toast" id="toast" class="toast" :data-tone="s.toastTone" role="status"><Icon :name="s.toastTone === 'success' ? 'check' : s.toastTone === 'error' || s.toastTone === 'warning' ? 'warning' : 'chat'" :size="18" /><span>{{ localizeRuntimeText(s.toast) }}</span><button class="icon-button" :aria-label="tr('关闭提示')" @click="s.toast = ''"><Icon name="close" :size="15" /></button></div></Transition>
    <div v-if="draggingFiles" id="drop-overlay" class="drop-overlay"><span class="drop-icon"><Icon name="upload" :size="38" /></span><strong>{{ tr('松开，让资料就位') }}</strong><span>{{ tr('批量上传资料') }} · DOCX / MD / TXT</span></div>
    <div id="announcement" class="sr-only" role="status" aria-live="polite">{{ localizeRuntimeText(s.announcement) }}</div>
  </div>
</template>
