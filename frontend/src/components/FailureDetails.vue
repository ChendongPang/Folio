<script setup lang="ts">
import { computed } from 'vue'
import { protocolDiagnosticFields } from '../diagnostics'
import { localizeRuntimeText, tr, trf } from '../i18n'
const props = defineProps<{ failure: object; compact?: boolean; jobId?: string | null }>()
const failure = computed(() => props.failure as Record<string, unknown>)
const string = (value: unknown) => typeof value === 'string' ? value : ''
const count = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
const progress = computed(() => localizeRuntimeText(string(failure.value.progress_note)) || (count(failure.value.saved_sections) !== null && count(failure.value.total_sections) !== null ? trf('已保存 {saved}/{total} 节正文。', { saved: failure.value.saved_sections as number, total: failure.value.total_sections as number }) : ''))
const technical = computed(() => {
  const f = failure.value
  const fields = [['定位编号', string(f.diagnostic_id)], ['错误类型', string(f.code)], ['模型动作', string(f.action)], ['任务编号', props.jobId || '']]
  const names: Record<string, string> = { received_chars: '已接收字符', trailing_whitespace_chars: '结尾空白字符', reasoning_chars: '思考字符', elapsed_seconds: '耗时（秒）', first_content_seconds: '首字等待（秒）', timeout_seconds: '超时上限（秒）', prompt_tokens: '输入 Token', completion_tokens: '输出 Token' }
  const details = f.details as Record<string, unknown> | undefined
  fields.push(...protocolDiagnosticFields(details).map(({ label, value }) => [label, value]))
  for (const [key, label] of Object.entries(names)) if (count(details?.[key]) !== null) fields.push([label, String(details?.[key])])
  return fields.filter(([, value]) => value).map(([label, value]) => [tr(label), value])
})
</script>

<template>
  <section :class="compact ? 'failure-details' : 'failure-card'" :aria-label="tr('失败诊断')">
    <strong v-if="!compact" class="failure-title">{{ tr('本次生成未完成') }}</strong>
    <p v-if="string(failure.reason)" class="failure-reason">{{ localizeRuntimeText(string(failure.reason)) }}</p>
    <dl class="failure-fields">
      <template v-if="failure.stage_label || failure.action_label"><dt>{{ tr('步骤') }}</dt><dd>{{ [localizeRuntimeText(string(failure.stage_label)), localizeRuntimeText(string(failure.action_label))].filter(Boolean).join(' · ') }}</dd></template>
      <template v-if="string(failure.task_title)"><dt>{{ tr('任务') }}</dt><dd>{{ failure.task_title }}</dd></template>
      <template v-if="Array.isArray(failure.section_labels) && failure.section_labels.length"><dt>{{ tr('章节') }}</dt><dd>{{ failure.section_labels.filter(item => typeof item === 'string').join(tr('、')) }}</dd></template>
      <template v-if="count(failure.attempts) !== null"><dt>{{ tr('尝试') }}</dt><dd>{{ trf('已尝试 {count} 次', { count: failure.attempts as number }) }}</dd></template>
      <template v-if="progress"><dt>{{ tr('保留进度') }}</dt><dd>{{ progress }}</dd></template>
    </dl>
    <details v-if="technical.length" class="failure-technical"><summary>{{ tr('技术定位') }}</summary><dl class="failure-fields"><template v-for="[label, value] in technical" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></template></dl></details>
  </section>
</template>
