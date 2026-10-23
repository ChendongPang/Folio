<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { WorkspaceController } from '../composables/useWorkspace'
import type { FileRole } from '../types'
import { localizeRuntimeText, tr, trf } from '../i18n'
import Icon from './Icon.vue'

interface SourceFile {
  id: string
  file: { name: string; size: number }
  server_id?: string
  uploading?: boolean
  queued?: boolean
  failed?: boolean
  removing?: boolean
  changingRole?: boolean
  role: FileRole
  error?: string
}
type SourceStatus = 'saved' | 'uploading' | 'queued' | 'failed'
const props = defineProps<{ workspace: WorkspaceController }>()
const emit = defineEmits<{ upload: [] }>()
const s = props.workspace.state
const search = ref('')
const filter = ref<'all' | 'saved' | 'pending' | 'failed'>('all')
const page = ref(1)
const pageSize = 50
const disabled = computed(() => s.running || s.restoring || s.restoreFailed)
const changingRole = computed(() => s.files.some(file => file.changingRole))
const statusLabels = computed<Record<SourceStatus, string>>(() => ({
  saved: tr('已保存'), uploading: tr('上传中'), queued: tr('排队中'), failed: tr('上传失败'),
}))
function status(file: SourceFile): SourceStatus {
  if (file.failed) return 'failed'
  if (file.uploading) return 'uploading'
  if (file.queued) return 'queued'
  return file.server_id ? 'saved' : 'queued'
}
const counts = computed(() => {
  const value = { saved: 0, uploading: 0, queued: 0, failed: 0 }
  for (const file of s.files) value[status(file)]++
  return value
})
const matches = computed(() => {
  const query = search.value.trim().toLocaleLowerCase()
  return s.files.filter(file => {
    const current = status(file)
    return (!query || file.file.name.toLocaleLowerCase().includes(query))
      && (filter.value === 'all' || (filter.value === 'pending' ? current === 'queued' || current === 'uploading' : current === filter.value))
  })
})
const pageCount = computed(() => Math.max(1, Math.ceil(matches.value.length / pageSize)))
const visibleFiles = computed(() => matches.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const firstVisible = computed(() => matches.value.length ? (page.value - 1) * pageSize + 1 : 0)
const lastVisible = computed(() => Math.min(page.value * pageSize, matches.value.length))
watch([search, filter], () => { page.value = 1 })
watch(pageCount, count => { page.value = Math.min(page.value, count) })
watch(() => s.conversationId, () => { search.value = ''; filter.value = 'all'; page.value = 1 })
function sizeLabel(size: number) {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}
function fileType(name: string) { return name.split('.').at(-1)?.toUpperCase() || 'FILE' }
function changeRole(item: SourceFile, event: Event) {
  const select = event.target as HTMLSelectElement
  const role = select.value as FileRole
  select.value = item.role
  void props.workspace.changeFileRole(item.id, role)
}
async function close() {
  s.materialsOpen = false
  await nextTick()
  document.getElementById('materials-toggle')?.focus()
}
</script>

<template>
  <aside id="materials-panel" class="materials-panel source-library" :aria-label="tr('资料库')" :hidden="!s.materialsOpen">
    <div class="panel-heading">
      <div><span class="sidebar-label">SOURCE LIBRARY</span><h2>{{ tr('资料库') }} <span class="panel-count">{{ s.files.length }} <span>/ 200</span></span></h2></div>
      <button id="close-materials" type="button" class="icon-button" :aria-label="tr('关闭资料库')" @click="close"><Icon name="close" /></button>
    </div>
    <p class="panel-description">{{ tr('业务资料提供事实；文档模板规定章节和格式（限一份 DOCX）；风格示例只借鉴写法和排版，不作为业务事实。多份风格示例以列表中的第一份为主参考。') }}</p>
    <div class="source-stats" :aria-label="tr('资料上传状态')" aria-live="polite">
      <div><strong>{{ counts.saved }}</strong><span>{{ tr('已保存') }}</span></div>
      <div><strong>{{ counts.uploading + counts.queued }}</strong><span>{{ counts.uploading ? `${counts.uploading} ${tr('上传中')} · ${counts.queued} ${tr('排队中')}` : tr('等待上传') }}</span></div>
      <div :class="{ 'has-failures': counts.failed > 0 }"><strong>{{ counts.failed }}</strong><span>{{ tr('上传失败') }}</span></div>
    </div>
    <div class="source-filters">
      <div class="source-search"><Icon name="search" :size="15" /><label class="sr-only" for="material-search">{{ tr('搜索资料文件名') }}</label><input id="material-search" v-model="search" type="search" :placeholder="tr('搜索文件名')" autocomplete="off"></div>
      <label class="sr-only" for="material-status-filter">{{ tr('筛选上传状态') }}</label><select id="material-status-filter" v-model="filter"><option value="all">{{ tr('全部状态') }}</option><option value="saved">{{ tr('已保存') }}</option><option value="pending">{{ tr('上传中 / 排队') }}</option><option value="failed">{{ tr('上传失败') }}</option></select>
    </div>
    <p v-if="counts.failed" class="source-error-note" role="status"><Icon name="warning" :size="14" />{{ trf('{count} 份资料上传失败，可在列表中重试或移除。', { count: counts.failed }) }}</p>
    <div id="material-list" class="source-list" :aria-busy="counts.uploading > 0">
      <div v-if="!s.files.length" class="source-empty"><Icon name="file" :size="28" /><strong>{{ tr('还没有资料') }}</strong><p>{{ tr('添加项目文档、会议记录或其他参考资料。') }}</p></div>
      <div v-else-if="!matches.length" class="source-empty"><Icon name="search" :size="25" /><strong>{{ tr('没有匹配的资料') }}</strong><p>{{ tr('试试其他文件名或上传状态。') }}</p><button type="button" class="text-button" @click="search = ''; filter = 'all'">{{ tr('清除筛选') }}</button></div>
      <ul v-else class="source-file-list">
        <li v-for="item in visibleFiles" :key="item.id" class="material-row source-file-row" :class="{ 'is-error': item.failed, 'is-uploading': item.uploading, 'is-removing': item.removing }">
          <span class="source-file-type" aria-hidden="true">{{ fileType(item.file.name) }}</span>
          <div class="material-file-info">
            <span :title="item.file.name">{{ item.file.name }}</span>
            <small>
              <span>{{ sizeLabel(item.file.size) }}</span>
              <span class="source-file-status" :class="item.removing ? 'status-removing' : `status-${status(item)}`">
                <i v-if="item.uploading || item.removing" class="tiny-spinner" />
                <Icon v-else-if="status(item) === 'saved'" name="check" :size="11" />
                {{ item.removing ? tr('移除中…') : statusLabels[status(item)] }}
              </span>
            </small>
            <p v-if="item.failed && item.error" class="source-file-error">{{ localizeRuntimeText(item.error) }}</p>
            <div v-if="item.server_id" class="source-file-purpose">
              <label :for="`source-role-${item.id}`">{{ tr('用途') }}</label>
              <select :id="`source-role-${item.id}`" :value="item.role" :aria-label="`${item.file.name} ${tr('文件用途')}`" :disabled="disabled || workspace.sourcesBusy.value" @change="changeRole(item, $event)">
                <option value="source">{{ tr('业务资料') }}</option>
                <option v-if="fileType(item.file.name) === 'DOCX'" value="template">{{ tr('文档模板') }}</option>
                <option value="style_reference">{{ tr('风格示例') }}</option>
              </select>
              <span v-if="item.changingRole" role="status">{{ tr('保存中…') }}</span>
            </div>
          </div>
          <div class="source-file-actions"><button v-if="item.failed" type="button" class="source-retry text-button" :aria-label="`${tr('重试上传')} ${item.file.name}`" :disabled="disabled || item.removing || changingRole" @click="workspace.retryFile(item.id)">{{ tr('重试') }}</button><button type="button" class="icon-button material-remove" :aria-label="`${tr('移除')} ${item.file.name}`" :disabled="disabled || item.uploading || item.removing || changingRole" @click="workspace.removeFile(item.id)"><Icon name="close" :size="15" /></button></div>
        </li>
      </ul>
    </div>
    <div v-if="matches.length" class="source-pagination"><span>{{ firstVisible }}–{{ lastVisible }} / {{ matches.length }}</span><div v-if="pageCount > 1"><button type="button" class="icon-button" :aria-label="tr('上一页资料')" :disabled="page <= 1" @click="page--"><Icon name="right" :size="15" class="previous-page-icon" /></button><span>{{ page }} / {{ pageCount }}</span><button type="button" class="icon-button" :aria-label="tr('下一页资料')" :disabled="page >= pageCount" @click="page++"><Icon name="right" :size="15" /></button></div></div>
    <div class="source-upload-footer"><button id="panel-upload" type="button" class="panel-upload" :disabled="disabled || s.files.length >= 200" @click="emit('upload')"><Icon name="plus" :size="17" />{{ s.files.length >= 200 ? tr('已达到 200 份资料上限') : tr('批量添加资料') }}</button><p>Word / PDF / Excel / PPT / {{ tr('文本') }} / {{ tr('图片') }}</p></div>
  </aside>
</template>
