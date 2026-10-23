<script setup lang="ts">
import { computed, ref } from 'vue'
import type { WorkspaceController } from '../composables/useWorkspace'
import type { DirectoryReview } from '../types'
import { localizeRuntimeText, tr, trf } from '../i18n'
import Icon from './Icon.vue'

const props = defineProps<{ workspace: WorkspaceController; review: DirectoryReview }>()
const s = props.workspace.state
const advice = ref(props.review.failed_advice || '')
const active = computed(() => props.review.revision === s.directoryReview?.revision)
const disabled = computed(() => !active.value || s.running || s.restoring || s.restoreFailed || s.directoryReview?.phase === 'generating' || s.files.some(file => file.uploading || file.queued || file.failed || file.removing))
// Matches RESUMABLE_TERMINATIONS: research that stopped on a timeout keeps its replayable record.
const resumable = computed(() => props.review.directory.confirmable === false && ['model_timeout', 'time_limit'].includes(props.review.directory.research?.termination_reason || ''))
function action(name: string, label: string) {
  void props.workspace.directoryAction(name, {}, label, props.review.revision)
}
</script>

<template>
  <div v-if="review.phase !== 'generating'" class="workflow-actions directory-review" :class="{ 'is-obsolete': !active }">
    <div class="directory-review-heading">
      <div><span class="workflow-label">DIRECTORY / REVIEW</span><h3>{{ review.directory.confirmable === false ? tr('目录初稿') : tr('确认目录') }}</h3></div>
      <span class="directory-count">{{ trf('{count} 个节点', { count: review.directory.sections.length }) }}</span>
    </div>
    <p v-if="!active" class="directory-obsolete">{{ tr('这是较早的目录版本，请在最新目录中操作。') }}</p>
    <p v-if="review.operation_error" class="directory-error" role="status">{{ tr('修改未完成，下面仍为上一版目录。可调整建议后重试。') }}</p>
    <div v-if="review.directory.confirmable === false" class="directory-error" role="status">
      <template v-if="review.directory.confirmation_issues?.length">
        <p v-for="(issue, index) in review.directory.confirmation_issues" :key="`${issue.code}-${index}`">{{ localizeRuntimeText(issue.message) }}</p>
      </template>
      <p v-else>{{ tr('目录检查尚未完成，请查看研究记录中的具体原因。') }}</p>
    </div>
    <ol class="directory-review-tree" :aria-label="tr('待确认的完整目录')">
      <li v-for="(section, index) in review.directory.sections" :key="`${section.number}-${index}`" :style="{ paddingLeft: `${Math.max(0, section.level - 1) * 16}px` }">
        <span>{{ section.number }}</span><strong>{{ section.title }}</strong>
      </li>
    </ol>
    <div class="directory-review-actions">
      <button class="primary-button directory-action" :disabled="disabled || review.directory.confirmable === false" @click="action('confirm_directory', tr('确认目录，生成文档'))"><Icon name="check" :size="16" />{{ tr('确认目录，生成文档') }}</button>
      <button v-if="resumable" class="secondary-button directory-action" :disabled="disabled" @click="action('resume_directory_research', tr('从中断处继续资料研究'))">{{ tr('从中断处继续研究') }}<Icon name="right" :size="15" /></button>
      <button class="text-button directory-action" :disabled="disabled" @click="action('regenerate_directory', tr('根据当前资料和需求重新生成完整目录'))">{{ tr('重新生成目录') }}</button>
      <a v-if="review.directory.style_url" class="text-button" :href="review.directory.style_url" target="_blank" rel="noopener">{{ tr('查看风格说明') }}</a>
    </div>
    <form class="directory-feedback" @submit.prevent="workspace.reviseDirectory(advice, review.revision)">
      <label :for="`advice-${review.revision}`">{{ tr('调整目录') }}</label>
      <textarea :id="`advice-${review.revision}`" v-model="advice" class="directory-action directory-advice" :placeholder="tr('例如：把实施计划单独列为一章，补充风险分析，并调整章节顺序。')" rows="2" maxlength="600000" :disabled="disabled" required />
      <button class="secondary-button directory-action" :disabled="disabled || !advice.trim()">{{ tr('按建议修改目录') }}<Icon name="right" :size="15" /></button>
    </form>
  </div>
</template>
