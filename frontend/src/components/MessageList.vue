<script setup lang="ts">
import { computed } from 'vue'
import { requestDiagnostics, type WorkspaceController } from '../composables/useWorkspace'
import type { Artifact, Message } from '../types'
import { elapsedLabel, modelStatusLabel } from '../progress'
import { localizeRuntimeText, localizeStep, tr, trf } from '../i18n'
import Icon from './Icon.vue'
import BrandMark from './BrandMark.vue'
import FailureDetails from './FailureDetails.vue'
import ReviewDetails from './ReviewDetails.vue'
import TaskUsagePanel from './TaskUsagePanel.vue'
import WorkflowActions from './WorkflowActions.vue'
const props = defineProps<{ workspace: WorkspaceController }>()
const archivedDownloads = computed(() => props.workspace.state.confirmationHistory.filter(item => !props.workspace.state.messages.some(message => message.artifact?.download_url === item.download_url)))
const retryMessageId = computed(() => props.workspace.state.retryJob ? [...props.workspace.state.messages].reverse().find(message => message.jobId === props.workspace.state.retryJob)?.id : undefined)
function showTaskProgress(message: Message) {
  return (message.historyDeferred || message.activities.length > 0) && (message.progressKind !== 'message' || Boolean(message.jobId))
}
function artifactTone(artifact: Artifact) {
  if (artifact.archived) return ''
  if (artifact.pending_count || artifact.proposal_count || artifact.issues?.length || artifact.status === 'needs_confirmation') return 'is-warning'
  return artifact.status === 'published' ? 'is-success' : ''
}
</script>

<template>
  <section id="messages" class="messages" :aria-label="tr('对话记录')">
    <article v-for="message in workspace.state.messages" :key="message.id" class="message" :class="message.role === 'user' ? 'user-message' : 'assistant-message'" :aria-busy="message.working">
      <div v-if="message.role === 'user'" class="user-content">{{ message.text }}<details v-if="message.files?.length" class="message-files"><summary><Icon name="file" :size="13" />{{ trf('本次资料 · {count} 份', { count: message.files.length }) }}</summary><div class="message-source-list"><span v-for="(file, index) in message.files" :key="index">{{ file }}</span></div></details></div>
      <template v-else>
        <div class="assistant-head"><span class="mini-logo" aria-hidden="true"><BrandMark :size="20" /></span><span>Folio</span><small>{{ tr('文档助手') }}</small></div>
        <div class="assistant-body">
          <details v-if="showTaskProgress(message)" class="activity" @toggle="($event.target as HTMLDetailsElement).open && workspace.loadJobHistory(message)" :class="{ 'is-running': message.working && !message.activityComplete, 'directory-activity': message.directoryProgress }" :open="!message.activityComplete">
            <summary><span class="activity-status" /><span class="activity-label">{{ message.working && !message.activityComplete && message.progressKind === 'directory' ? tr('资料研究与目录规划') : localizeRuntimeText(message.activityLabel) || tr('处理记录') }}</span><span v-if="message.directoryProgress?.elapsedSeconds !== undefined" class="activity-elapsed">{{ message.working && !message.activityComplete ? tr('已用时') : tr('用时') }} {{ elapsedLabel(message.directoryProgress.elapsedSeconds) }}</span><Icon name="down" :size="16" /></summary>
            <p v-if="message.working && !message.activityComplete && message.directoryProgress" class="activity-wait" role="status"><span>{{ modelStatusLabel(message.directoryProgress.modelStatus) || tr('正在执行当前步骤') }}</span><span v-if="message.directoryProgress.stepElapsedSeconds !== undefined">{{ tr('本步已用时') }} {{ elapsedLabel(message.directoryProgress.stepElapsedSeconds) }}</span></p>
            <p v-if="message.historyLoading" role="status">{{ tr('正在加载...') }}</p>
            <ol class="activity-list">
              <li v-for="activity in message.activities" :key="activity.id" class="activity-item" :class="{ 'is-current': message.working && !message.activityComplete && activity.id === message.currentActivityId, 'is-failed': activity.failure || activity.review?.status === 'failed' || activity.status === 'failed', 'is-cancelled': activity.review?.status === 'cancelled' }">
                <Icon v-if="activity.status === 'completed'" class="activity-check" name="check" :size="13" /><span v-else class="activity-point" />
                <details v-if="activity.failure" class="failure-activity activity-copy"><summary>{{ localizeStep(activity.text) }}</summary><FailureDetails :failure="activity.failure" :job-id="activity.jobId" compact /></details>
                <details v-else-if="activity.review" class="review-activity activity-copy"><summary><span class="review-activity-title">{{ localizeStep(activity.text) }}</span><Icon name="down" :size="14" /></summary><ReviewDetails :review="activity.review" /></details>
                <span v-else class="activity-copy"><span>{{ localizeStep(activity.text) }}</span><span v-if="activity.detail" class="activity-detail">{{ localizeStep(activity.detail) }}</span></span>
              </li>
            </ol>
          </details>
          <p v-if="message.text" class="message-text">{{ message.contentOrigin === 'model' ? message.text : localizeRuntimeText(message.text) }}</p>
          <p v-for="(note, index) in message.notes" :key="index" class="result-note">{{ localizeRuntimeText(note) }}</p>
          <FailureDetails v-if="message.failure" :failure="message.failure" :job-id="message.jobId" />
          <div v-if="message.id === retryMessageId" class="job-retry">
            <button class="primary-button" :disabled="workspace.state.running || workspace.state.taskControlPending || workspace.state.restoring || workspace.state.restoreFailed" @click="workspace.retryFailedJob(message.jobId)">{{ workspace.state.taskControlPending ? tr('正在恢复…') : tr('重试并继续') }}<Icon name="right" :size="15" /></button>
            <span>{{ tr('沿用已确认目录，从保存的进度继续。') }}</span>
          </div>
          <details v-if="requestDiagnostics(message.diagnostics).length" class="failure-technical request-error-details"><summary>{{ tr('错误详情') }}</summary><dl class="failure-fields"><template v-for="field in requestDiagnostics(message.diagnostics)" :key="field.label"><dt>{{ tr(field.label) }}</dt><dd>{{ localizeRuntimeText(field.value) }}</dd></template></dl></details>
          <WorkflowActions v-if="message.directoryReview" :workspace="workspace" :review="message.directoryReview" />
          <div v-if="message.styleAnalysis" class="download-card style-analysis-card" :class="message.styleAnalysis.status === 'analyzed' ? 'is-success' : 'is-warning'">
            <div class="word-icon"><Icon name="file" :size="24" /></div>
            <div class="download-info">
              <strong>{{ tr('风格说明') }}</strong>
              <small>{{ message.styleAnalysis.status === 'analyzed' ? tr('分析完成') : message.styleAnalysis.status === 'measured_only' ? tr('已提取结构与格式，写作风格待确认') : tr('部分特征待确认') }}{{ message.styleAnalysis.cache_hit ? ` · ${tr('已复用分析结果')}` : '' }}</small>
              <ul class="style-analysis-files" :aria-label="tr('已分析的风格示例')"><li v-for="(name, index) in message.styleAnalysis.document_names" :key="index">{{ name }}</li></ul>
            </div>
            <a class="download-link" :href="message.styleAnalysis.style_url" target="_blank" rel="noopener">{{ tr('查看') }} style.md<Icon name="right" :size="15" /></a>
          </div>
          <template v-if="message.artifact">
            <div class="download-card" :class="artifactTone(message.artifact)"><div class="word-icon"><Icon name="file" :size="24" /></div><div class="download-info"><strong>{{ message.artifact.filename }}</strong><small>{{ message.artifact.archived ? tr('之前的草稿 · 补答前版本') : message.artifact.pending_count ? tr('Word 草稿 · 请核对待确认项') : message.artifact.proposal_count ? tr('Word 草稿 · 含模型建议') : message.artifact.status === 'published' ? tr('Word 文档 · 检查通过') : tr('Word 草稿') }}</small></div><a class="download-link" :href="message.artifact.download_url" :download="message.artifact.filename"><Icon name="download" :size="16" />{{ message.artifact.archived ? tr('下载之前的草稿') : tr('下载文档') }}</a></div>
            <details v-if="message.artifact.issues?.length" class="activity artifact-issues"><summary>{{ trf('查看 {count} 项待确认问题', { count: message.artifact.issues.length }) }}</summary><ul><li v-for="(issue, index) in message.artifact.issues" :key="index">{{ localizeRuntimeText(issue) }}</li></ul></details>
          </template>
          <TaskUsagePanel :elapsed-ms="message.artifact?.elapsed_ms" :items="message.taskUsage || message.directoryReview?.directory.task_usage || message.artifact?.task_usage || []" />
          <div v-if="message.working && !message.text.trim() && !showTaskProgress(message) && !message.styleAnalysis" class="agent-working" role="status"><span class="working-orbit" aria-hidden="true"><i /><i /><i /></span><span class="agent-working-label">{{ localizeRuntimeText(message.workingLabel) || tr('正在思考...') }}</span></div>
        </div>
      </template>
    </article>
    <details v-if="archivedDownloads.length" class="activity artifact-history">
      <summary>{{ trf('之前的草稿 · {count} 份', { count: archivedDownloads.length }) }}</summary>
      <div v-for="artifact in archivedDownloads" :key="artifact.request_id" class="download-card">
        <div class="word-icon"><Icon name="file" :size="24" /></div>
        <div class="download-info"><strong>{{ artifact.filename }}</strong><small>{{ tr('之前的草稿 · 补答前版本') }}</small></div>
        <a class="download-link" :href="artifact.download_url" :download="artifact.filename"><Icon name="download" :size="16" />{{ tr('下载之前的草稿') }}</a>
      </div>
    </details>
  </section>
</template>
