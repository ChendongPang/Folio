<script setup lang="ts">
import { computed } from 'vue'
import { formatElapsed, formatTokens, tr } from '../i18n'
import type { TaskUsage } from '../types'
import Icon from './Icon.vue'

const props = defineProps<{ items: TaskUsage[]; elapsedMs?: number | null }>()
const total = computed(() => props.items.reduce((sum, item) => sum + (item.total_tokens || 0), 0))
const totalElapsed = computed(() => {
  if (props.elapsedMs != null) return props.elapsedMs;
  const starts = props.items.map(item => Date.parse(item.started_at || '')).filter(Number.isFinite);
  const ends = props.items.map(item => Date.parse(item.finished_at || '')).filter(Number.isFinite);
  return starts.length && ends.length ? Math.max(0, Math.max(...ends) - Math.min(...starts)) : null;
})
</script>

<template>
  <details v-if="items.length" class="activity task-usage">
    <summary>
      <span class="activity-status" />
      <span class="activity-label">{{ tr('任务统计') }}</span>
      <span class="activity-elapsed"><template v-if="totalElapsed != null">{{ tr('实际耗时') }} {{ formatElapsed(totalElapsed / 1000) }} · </template> {{ formatTokens(total) }} Token</span>
      <Icon name="down" :size="14" />
    </summary>
    <div class="task-usage-list">
      <article v-for="item in items" :key="item.task_id" class="task-usage-row">
        <strong>{{ item.title || item.task_id }}</strong>
        <span>{{ tr('耗时') }}: {{ formatElapsed((item.elapsed_ms || 0) / 1000) }}</span>
        <span>{{ tr('输入 Token') }}: {{ formatTokens(item.input_tokens) }}</span>
        <span>{{ tr('输出 Token') }}: {{ formatTokens(item.output_tokens) }}</span>
        <span>{{ tr('总 Token') }}: {{ formatTokens(item.total_tokens) }}</span>
        <span>{{ tr('模型调用') }}: {{ item.model_calls || 0 }}</span>
        <span>{{ tr('缓存命中') }}: {{ item.cache_hits || 0 }}</span>
        <span v-if="item.failed_calls">{{ tr('失败调用') }}: {{ item.failed_calls }}</span>
      </article>
    </div>
  </details>
</template>
