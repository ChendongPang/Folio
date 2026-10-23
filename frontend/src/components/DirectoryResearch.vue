<script setup lang="ts">
import { computed } from 'vue'
import type { DirectoryResearch, DirectoryResearchFile, DirectoryWebSearch } from '../types'
import { language, localizeRuntimeText, tr, trf } from '../i18n'

const props = defineProps<{ research: DirectoryResearch }>()
const labels = computed(() => ({
  ready: tr('已完成目录研究'), partial: tr('研究尚未充分'), no_sources: tr('未使用业务资料'),
}))
const readCount = computed(() => props.research.files.filter(file => file.read).length)
const webResearch = computed(() => props.research.web_research)
function name(file: DirectoryResearchFile) {
  return (file.name || file.path.split(/[\\/]/).filter(Boolean).at(-1) || tr('未命名资料')).replace(/^[a-f0-9]{32,64}[_-]+/i, '')
}
function characters(value?: number) {
  return Number.isFinite(value) && (value ?? 0) > 0 ? `${Math.trunc(value!).toLocaleString(language.value)} ${tr('字符')}` : ''
}
function sources(search: DirectoryWebSearch) {
  const ids = new Set(search.evidence_ids)
  return (webResearch.value?.evidence || []).filter(source => ids.has(source.evidence_id))
}
function webStatus(search: DirectoryWebSearch) {
  if (search.status === 'completed') {
    const count = search.source_count ?? new Set(sources(search).map(source => source.url)).size
    return `${search.cached ? tr('已复用缓存') : tr('搜索完成')} · ${trf('{count} 个公开来源', { count })}`
  }
  return { failed: tr('搜索未成功'), unavailable: tr('联网搜索暂不可用'), interrupted: tr('搜索已中断') }[search.status]
}
function publicUrl(value: string) {
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? value : undefined
  } catch { return undefined }
}
</script>

<template>
  <details class="directory-research" :data-status="research.status" :open="research.status === 'partial'">
    <summary><span>{{ tr('资料研究记录') }}</span><span class="research-status">{{ labels[research.status] }}</span></summary>
    <div class="research-content">
      <p v-if="research.summary" class="research-summary">{{ localizeRuntimeText(research.summary) }}</p>
      <p v-if="research.files.length" class="research-count">{{ trf('{files} 份资料，已读取其中 {read} 份的原文。', { files: research.files.length, read: readCount }) }}</p>
      <ul v-if="research.files.length" class="research-files" :aria-label="tr('资料阅读记录')">
        <li v-for="(file, index) in research.files" :key="`${file.path}-${index}`">
          <span class="research-file-name">{{ name(file) }}</span>
          <span class="research-file-state" :data-read="file.read">
            {{ file.inspected ? tr('已查看结构') : tr('未查看结构') }} · {{ file.read ? tr('已读取原文') : tr('未读取原文') }}<template v-if="file.read && characters(file.characters)"> · {{ characters(file.characters) }}</template>
          </span>
          <ul v-if="file.readings?.length" class="research-readings" :aria-label="tr('已读取内容')">
            <li v-for="(reading, readingIndex) in file.readings" :key="readingIndex">{{ localizeRuntimeText(reading.label) }}<span v-if="characters(reading.characters)"> · {{ characters(reading.characters) }}</span></li>
          </ul>
        </li>
      </ul>
      <section v-if="webResearch?.searches.length" class="research-web" :aria-label="tr('联网补充记录')">
        <p>{{ trf('联网补充 · {count} 次查询', { count: webResearch.searches.length }) }}</p>
        <ul class="research-files research-web-searches">
          <li v-for="(search, index) in webResearch.searches" :key="index">
            <span class="research-file-name">{{ search.query || tr('公开资料查询') }}</span>
            <span class="research-file-state">{{ webStatus(search) }}</span>
            <p v-if="search.purpose">{{ tr('目录用途：') }}{{ search.purpose }}</p>
            <ul v-if="sources(search).length" class="research-readings" :aria-label="tr('公开来源')">
              <li v-for="source in sources(search)" :key="source.evidence_id">
                <a v-if="publicUrl(source.url)" :href="publicUrl(source.url)" target="_blank" rel="noopener noreferrer">{{ source.title || source.url }}</a>
                <span v-else>{{ source.title || tr('公开来源') }}</span>
                <span v-if="source.retrieved_at"> · {{ tr('获取于') }} {{ source.retrieved_at }}</span>
              </li>
            </ul>
          </li>
        </ul>
        <ul v-if="webResearch.findings?.length" class="research-readings" :aria-label="tr('联网研究结论')">
          <li v-for="(finding, index) in webResearch.findings" :key="index">{{ finding.text }}</li>
        </ul>
        <p class="research-footnote">{{ tr('联网资料用于补充公开背景，不能替代上传资料或证明用户项目事实。') }}</p>
      </section>
      <div v-if="research.unresolved.length" class="research-unresolved" role="status">
        <p>{{ tr('尚待解决') }}</p>
        <ul><li v-for="(issue, index) in research.unresolved" :key="index">{{ localizeRuntimeText(issue) }}</li></ul>
      </div>
      <p v-if="research.status === 'partial'" class="research-next-step">{{ tr('当前目录仍是初稿，请补充资料或调整要求后重新生成目录。') }}</p>
      <p class="research-footnote">{{ tr('阅读记录展示工具实际返回的原文范围，不代表全文读完或理解率。') }}</p>
      <a v-if="research.download_url" :href="research.download_url" download>{{ tr('下载完整阅读记录') }}</a>
    </div>
  </details>
</template>
