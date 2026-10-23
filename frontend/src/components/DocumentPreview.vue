<script setup lang="ts">
import { computed } from 'vue'
import { usePreviewSplit } from '../composables/usePreviewSplit'
import type { WorkspaceController } from '../composables/useWorkspace'
import Icon from './Icon.vue'
import { language, localizeRuntimeText, tr, trf } from '../i18n'
import DirectoryResearch from './DirectoryResearch.vue'

const props = defineProps<{ workspace: WorkspaceController }>()
const emit = defineEmits<{ close: [] }>()
const s = props.workspace.state
const review = computed(() => s.directoryReview)
const research = computed(() => review.value?.research || review.value?.directory.research)
const hasPreview = computed(() => Boolean(s.preview.downloadUrl))
const previewTitle = computed(() => s.preview.titleIsLabel ? localizeRuntimeText(s.preview.title) : s.preview.title)
function pageLabel(number: number) {
  return language.value === 'en' ? `Page ${number}` : `第 ${number} 页`
}
const pageCount = computed(() => String(s.preview.pages.length).padStart(2, '0'))
const { panes, dragging, splitStyle, percentage, minimumPercentage, maximumPercentage, startResize, moveResize, finishResize, cancelResize, keyResize, resetSplit } = usePreviewSplit()
</script>

<template>
  <aside id="document-preview" class="document-preview" :class="{ 'is-open': s.previewOpen }" :aria-label="tr('文档目录预览')">
    <slot name="resize-handle" />
    <header class="preview-heading">
      <div><span class="sidebar-label preview-section-label">PREVIEW <span class="preview-index" aria-hidden="true">/ {{ pageCount }}</span></span><h2 id="preview-title">{{ previewTitle || tr('完整目录') }}</h2></div>
      <div class="preview-heading-actions">
        <a v-if="hasPreview" id="preview-download" class="download-link" :href="s.preview.downloadUrl" :title="tr('下载目录 Word')"><Icon name="download" :size="16" /><span>{{ tr('下载目录') }}</span></a>
        <button type="button" class="icon-button preview-close" :aria-label="tr('关闭文档预览')" @click="emit('close')"><Icon name="close" :size="18" /></button>
      </div>
    </header>
    <div id="preview-panes" ref="panes" class="preview-panes" :style="splitStyle">
      <section id="preview-directory-pane" class="preview-pane" aria-labelledby="preview-directory-title">
        <header class="preview-pane-heading"><h3 id="preview-directory-title">{{ tr('目录结构') }}</h3><span v-if="review?.directory">{{ trf('{count} 个节点', { count: review.directory.sections.length }) }}</span></header>
        <div class="preview-directory-scroll" tabindex="0" :aria-label="tr('目录结构内容')">
          <div v-if="!review" id="preview-empty" class="preview-empty">
            <div class="preview-empty-frame" aria-hidden="true"><i class="preview-corner top-left" /><i class="preview-corner top-right" /><i class="preview-corner bottom-left" /><i class="preview-corner bottom-right" /><span class="preview-crosshair">+</span></div>
            <p>{{ tr('描述文档需求后，在这里查看完整目录。') }}</p>
          </div>
          <p v-if="review?.reason" class="preview-reason">{{ localizeRuntimeText(review.reason) }}</p>
          <DirectoryResearch v-if="research" :key="review?.revision" :research="research" />
          <div v-if="review?.directory" id="directory-toc" class="directory-toc">
            <ol class="directory-node-list"><li v-for="(section, index) in review.directory.sections" :key="`${section.number}-${index}`" :style="{ paddingLeft: `${Math.max(0, section.level - 1) * 16}px` }"><span>{{ section.number }}</span> {{ section.title }}</li></ol>
          </div>
        </div>
      </section>
      <div id="preview-split-handle" class="preview-split-handle" :class="{ 'is-dragging': dragging }" role="separator" tabindex="0" :aria-label="tr('调整目录与页面预览高度')" aria-orientation="horizontal" aria-controls="preview-directory-pane preview-page-pane" :aria-valuenow="percentage" :aria-valuemin="minimumPercentage" :aria-valuemax="maximumPercentage" :aria-valuetext="trf('目录 {directory}%，页面预览 {page}%', { directory: percentage, page: 100 - percentage })" :title="tr('拖动调整高度，双击恢复默认')" @pointerdown="startResize" @pointermove="moveResize" @pointerup="finishResize" @pointercancel="cancelResize" @lostpointercapture="cancelResize" @keydown="keyResize" @dblclick="resetSplit"><span aria-hidden="true" /></div>
      <section id="preview-page-pane" class="preview-pane" aria-labelledby="preview-page-title">
        <header class="preview-pane-heading"><h3 id="preview-page-title">{{ tr('页面预览') }}</h3><span v-if="s.preview.pages.length">{{ trf('{count} 页', { count: s.preview.pages.length }) }}</span></header>
        <div class="preview-page-scroll" tabindex="0" :aria-label="tr('文档页面内容')">
          <div v-if="hasPreview" id="directory-preview" class="directory-preview">
            <p id="preview-status" role="status">{{ localizeRuntimeText(s.preview.status) }}</p>
            <div v-if="s.preview.pages.length && !s.preview.showStructure" id="preview-pages" class="preview-pages" :aria-label="tr('目录版式预览')">
              <figure v-for="(url, index) in s.preview.pages" :key="url" class="preview-page"><img :src="url" :alt="`${previewTitle} · ${pageLabel(index + 1)}`" :loading="index ? 'lazy' : 'eager'" @error="workspace.previewImageFailed()"><figcaption>{{ pageLabel(index + 1) }} / {{ s.preview.pages.length }}</figcaption></figure>
            </div>
            <div v-if="s.preview.showStructure" id="preview-structure" class="preview-structure">
              <template v-for="(block, index) in s.preview.blocks" :key="index">
                <table v-if="block.kind === 'table'"><tbody><tr v-for="(row, rowIndex) in block.rows" :key="rowIndex"><td v-for="(cell, cellIndex) in row" :key="cellIndex">{{ cell }}</td></tr></tbody></table>
                <component :is="block.kind === 'heading' ? `h${Math.min(6, Math.max(2, block.level || 2))}` : 'p'" v-else-if="block.text">{{ block.text }}</component>
              </template>
            </div>
          </div>
          <div v-else class="preview-empty preview-pages-empty"><p>{{ tr('生成目录后，在这里逐页查看 Word 排版。') }}</p></div>
          <p v-show="review" id="preview-footnote" class="preview-footnote">{{ localizeRuntimeText(s.previewFootnote) }}</p>
        </div>
      </section>
    </div>
  </aside>
</template>
