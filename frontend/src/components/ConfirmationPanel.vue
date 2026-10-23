<script setup lang="ts">
import { computed } from 'vue'
import type { WorkspaceController } from '../composables/useWorkspace'
import { localizeRuntimeText, tr, trf } from '../i18n'
import Icon from './Icon.vue'

const props = defineProps<{ workspace: WorkspaceController }>()
const s = props.workspace.state
const disabled = computed(() => s.confirmationSubmitting || s.confirmationLoading || s.running || s.restoring)
const answered = computed(() => s.confirmation?.issues.filter(issue => s.confirmationAnswers[issue.issue_id]?.trim()).length || 0)
</script>

<template>
  <section v-if="s.confirmationJob" id="confirmation-panel" class="confirmation-panel" :class="{ 'is-collapsed': s.confirmationCollapsed }" aria-labelledby="confirmation-title" :aria-busy="s.confirmationLoading || s.confirmationSubmitting">
    <div class="confirmation-heading">
      <h3 id="confirmation-title">
        <button id="confirmation-toggle" type="button" class="confirmation-toggle" :aria-expanded="!s.confirmationCollapsed" aria-controls="confirmation-content" :aria-label="s.confirmationCollapsed ? tr('展开补充确认') : tr('收起补充确认')" :disabled="s.confirmationSubmitting || s.confirmationLoading" @click="workspace.toggleConfirmation()">
          <Icon name="chat" :size="18" />
          <span>{{ s.confirmationSkipped ? tr('已跳过本次补充') : tr('补充确认') }}</span>
          <span v-if="!s.confirmationSkipped" class="confirmation-optional">{{ tr('可选') }}</span>
          <Icon name="down" :size="16" class="confirmation-chevron" />
        </button>
      </h3>
      <button v-if="s.confirmationSkipped" id="confirmation-reopen" type="button" class="confirmation-text-button" :disabled="s.confirmationSubmitting || s.confirmationLoading" @click="workspace.toggleConfirmation()">{{ tr('重新补充') }}</button>
      <button v-else id="confirmation-skip" type="button" class="confirmation-text-button" :disabled="disabled || !s.confirmation" @click="workspace.skipConfirmation()">{{ tr('跳过本次补充') }}</button>
    </div>
    <div v-show="!s.confirmationCollapsed" id="confirmation-content" class="confirmation-content">
      <p>{{ tr('当前文档可直接下载，这一步可以跳过。可以只补充部分问题，其余留空；补充后会继续完善相关内容。') }}</p>
      <p v-if="s.confirmationLoading" role="status">{{ tr('正在读取待确认问题…') }}</p>
      <p v-if="s.confirmationError" class="confirmation-error" role="alert">{{ localizeRuntimeText(s.confirmationError) }}</p>
      <button v-if="!s.confirmation && !s.confirmationLoading" class="secondary-button" @click="workspace.loadConfirmation()">{{ tr('重新加载问题') }}</button>
      <form v-if="s.confirmation" @submit.prevent="workspace.submitConfirmation()">
        <div class="confirmation-questions">
          <div v-for="(issue, index) in s.confirmation.issues" :key="issue.issue_id" class="confirmation-question">
            <label :for="`confirmation-answer-${index}`"><span>{{ index + 1 }}.</span> {{ localizeRuntimeText(issue.description) }}</label>
            <textarea :id="`confirmation-answer-${index}`" v-model="s.confirmationAnswers[issue.issue_id]" rows="3" maxlength="600000" :disabled="disabled" :placeholder="tr('填写明确的业务规则或说明；暂时无法确认的可以留空。')" @input="workspace.saveConfirmationDraft()" />
            <label v-if="issue.key.startsWith('source-comment:')" class="confirmation-close-comment"><input v-model="s.confirmationClosedComments[issue.issue_id]" type="checkbox" :disabled="disabled || !s.confirmationAnswers[issue.issue_id]?.trim()" @change="workspace.saveConfirmationDraft()">{{ tr('此答复已解决这条来源批注') }}</label>
          </div>
        </div>
        <p v-if="!s.confirmation.issues.length">{{ tr('没有需要补充的具体问题。服务恢复后，可以重新检查已保存的文档。') }}</p>
        <div class="confirmation-budget">
          <label for="confirmation-calls">{{ tr('本轮最多调用模型') }}</label>
          <input id="confirmation-calls" v-model.number="s.confirmationCalls" type="number" min="1" max="10000" step="1" required :disabled="disabled" @input="workspace.saveConfirmationDraft()"><span>{{ tr('次') }}</span>
        </div>
        <p class="confirmation-budget-note">{{ trf('继续处理可能产生模型费用；达到本轮上限后停止新增调用，不会自动追加额度。此前已调用 {count} 次。', { count: s.confirmation.used_calls }) }}</p>
        <div class="confirmation-actions">
          <button id="confirmation-submit" class="primary-button" :disabled="disabled"><Icon :name="s.confirmationSubmitting ? 'clock' : 'play'" :size="15" />{{ s.confirmationSubmitting ? tr('正在提交…') : answered ? tr('补答并继续') : tr('重新检查') }}</button>
          <span>{{ answered ? trf('本次补充 {count} 项', { count: answered }) : tr('未填写答复，将重新检查已有内容') }}</span>
        </div>
      </form>
    </div>
  </section>
</template>

<style scoped>
.confirmation-panel { width: min(100%, 760px); margin: 20px auto; border: 1px solid var(--line); background: var(--surface); border-radius: 10px; }
.confirmation-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 12px 16px; }
.confirmation-heading h3 { min-width: 0; margin: 0; font-size: 14px; color: var(--text); }
.confirmation-toggle { display: flex; align-items: center; gap: 8px; border: 0; padding: 4px 0; background: none; color: inherit; font: inherit; font-weight: 600; cursor: pointer; text-align: left; }
.confirmation-toggle > svg:first-child { color: var(--muted); flex-shrink: 0; }
.confirmation-optional { font-size: 10px; font-weight: 400; color: var(--muted); border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px; white-space: nowrap; }
.confirmation-chevron { flex-shrink: 0; color: var(--muted); transform: rotate(180deg); }
.is-collapsed .confirmation-chevron { transform: none; }
.confirmation-text-button { flex-shrink: 0; border: 0; border-radius: 6px; padding: 6px 8px; background: none; color: var(--muted); font: inherit; font-size: 12px; cursor: pointer; }
.confirmation-text-button:hover:not(:disabled) { background: var(--line); color: var(--text); }
.confirmation-toggle:focus-visible, .confirmation-text-button:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: 4px; }
.confirmation-toggle:disabled, .confirmation-text-button:disabled { cursor: default; opacity: .5; }
.confirmation-content { padding: 0 20px 20px; }
.confirmation-panel p { font-size: 12px; color: var(--muted); line-height: 1.8; }
.confirmation-questions { display: grid; gap: 18px; margin: 20px 0; }
.confirmation-question label { display: block; line-height: 1.8; font-size: 13px; margin-bottom: 7px; white-space: pre-wrap; overflow-wrap: anywhere; }
.confirmation-question label span { color: var(--accent); }
.confirmation-question .confirmation-close-comment { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted); margin-top: 8px; }
.confirmation-question textarea { width: 100%; box-sizing: border-box; min-height: 78px; resize: vertical; font: inherit; font-size: 12px; line-height: 1.8; border: 1px solid var(--line); border-radius: 3px; padding: 10px 12px; background: var(--surface); color: var(--text); }
.confirmation-question textarea:focus, .confirmation-budget input:focus { outline: 2px solid var(--accent); outline-offset: 2px; }
.confirmation-budget { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 12px; }
.confirmation-budget input { width: 88px; border: 1px solid var(--line); border-radius: 3px; padding: 7px 9px; color: var(--text); background: var(--surface); }
.confirmation-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-top: 16px; }
.confirmation-actions span { font-size: 11px; color: var(--muted); }
.confirmation-panel .confirmation-error { color: var(--error); }
@media (max-width: 760px) {
  .confirmation-heading { gap: 6px; padding: 10px 12px; }
  .confirmation-toggle { gap: 6px; }
  .confirmation-content { padding: 0 14px 14px; }
  .confirmation-text-button { padding: 6px 4px; }
}
</style>
