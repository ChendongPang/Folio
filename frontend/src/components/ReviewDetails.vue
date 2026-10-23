<script setup lang="ts">
import { computed } from 'vue'
import { language, localizeRuntimeText, tr } from '../i18n'
const props = defineProps<{ review: object }>()
const phases = computed<Record<string, string>>(() => ({ consistency: tr('跨章节一致性检查'), drill_down: tr('核对疑点与原文'), repair: tr('修正文档') }))
const outcomes = computed<Record<string, string>>(() => ({ confirmed: tr('模型判定疑点成立'), dismissed: tr('模型判定疑点不成立'), needs_confirmation: tr('仍需确认'), hard_check_retained: tr('模型认为不成立，但结构或来源校验仍失败，保留待确认') }))
const statuses = computed<Record<string, string>>(() => ({ started: tr('进行中'), completed: tr('已完成'), failed: tr('未完成'), cancelled: tr('已中止'), summary: tr('本轮汇总') }))
const isCount = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n) && n >= 0
const str = (v: unknown) => typeof v === 'string' ? v : ''
const fields = computed(() => {
  const r = props.review as Record<string, unknown>
  const result: { label: string; values: string[] }[] = []
  const add = (label: string, value: string | string[]) => { const values = (Array.isArray(value) ? value : [value]).filter(Boolean); if (values.length) result.push({ label, values }) }
  const status = r.status === 'summary' ? ({ started: tr('开始'), completed: tr('已结束') }[str(r.step)] || tr('本轮汇总')) : statuses.value[str(r.status)]
  add(tr('当前步骤'), [phases.value[str(r.phase)], status].filter(Boolean).join(' · '))
  if (isCount(r.index)) add(tr('检查单元'), `${tr('第')} ${r.index} ${r.phase === 'consistency' ? tr('组') : tr('项')}${isCount(r.total) ? ` / ${tr('共')} ${r.total} ${r.phase === 'consistency' ? tr('组') : tr('项')}` : ''}`)
  if (isCount(r.completed)) add(tr('本轮进度'), `${tr('已完成')} ${r.completed}${isCount(r.total) ? ` / ${r.total}` : ''}`)
  if (Array.isArray(r.sections)) add(tr('涉及章节'), r.sections.filter(s => s && typeof s === 'object').map(s => `${s.title || s.section_id || tr('未命名章节')}${isCount(s.revision) ? (language.value === 'en' ? ` (revision ${s.revision})` : `（第 ${s.revision} 版）`) : ''}`))
  if (Array.isArray(r.reasons)) add(tr('检查原因'), r.reasons.filter(v => typeof v === 'string').map(localizeRuntimeText))
  if (Array.isArray(r.findings)) add(tr('发现的疑点'), r.findings.map(v => localizeRuntimeText(str(v?.description))))
  add(tr('核查结果'), outcomes.value[str(r.outcome)] || '')
  add(tr('判断说明'), localizeRuntimeText(str(r.reason)))
  if (isCount(r.elapsed_ms)) add(tr('耗时'), r.elapsed_ms >= 60000 ? `${(r.elapsed_ms / 60000).toFixed(1)} ${language.value === 'en' ? 'min' : '分钟'}` : `${(r.elapsed_ms / 1000).toFixed(1)} ${language.value === 'en' ? 's' : '秒'}`)
  const counts = (v: unknown) => {
    if (!v || typeof v !== 'object') return ''
    const c = v as Record<string, unknown>
    return [['model_calls', tr('模型任务调用')], ['cache_hits', tr('复用缓存')], ['failed_calls', tr('失败调用')]].filter(([key]) => isCount(c[key!])).map(([key, label]) => `${label} ${c[key!]} ${tr('次')}`).join(' · ')
  }
  add(tr('调用统计'), counts(r.calls))
  const calls = r.calls as { tasks?: Record<string, unknown> } | undefined
  const names = computed<Record<string, string>>(() => ({ review: tr('一致性检查'), review_work: tr('业务任务审阅'), review_focus: tr('审阅重点核对'), plan_review: tr('安排审阅范围'), drill_down: tr('精查原文'), repair: tr('修正文档'), validate: tr('核查修订内容'), extract: tr('抽取内容事实'), select_context: tr('筛选相关资料'), read_review_materials: tr('读取审阅资料'), complete_gaps: tr('补充内容缺口') }))
  if (calls?.tasks) add(tr('调用明细'), Object.entries(calls.tasks).filter(([, v]) => counts(v)).map(([key, v]) => `${names.value[key] || key}${language.value === 'en' ? ': ' : '：'}${counts(v)}`))
  return result
})
</script>

<template>
  <div class="review-activity-content"><div v-for="field in fields" :key="field.label" class="review-detail"><strong class="review-detail-label">{{ field.label }}</strong><span v-if="field.values.length === 1">{{ field.values[0] }}</span><ul v-else><li v-for="(value, index) in field.values" :key="index">{{ value }}</li></ul></div></div>
</template>
