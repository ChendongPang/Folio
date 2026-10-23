<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { acceptLanguageHeaders, registerRuntimeTranslations, runtimeSourceText, localizeModelTestCode, localizeRuntimeText, tr } from '../i18n'
import Icon from './Icon.vue'

type Service = 'llm' | 'decision' | 'tavily'
type Provider = 'custom'
interface ModelConfig {
  configured: boolean
  has_api_key?: boolean
  provider_mode?: Provider
  base_url?: string
  model?: string
  no_auth?: boolean
  decision_model?: string
  decision_enabled?: boolean
  decision_base_url?: string
  has_decision_api_key?: boolean
  search_provider?: 'tavily' | 'deepseek'
  has_deepseek_api_key?: boolean
  tavily_enabled?: boolean
  has_tavily_api_key?: boolean
  max_tokens?: number
  planning_max_tokens?: number | null
  max_input_tokens?: number | null
  context_window?: number | null
  tokenizer_encoding?: 'o200k_base' | 'cl100k_base' | null
  temperature?: number
  enable_thinking?: boolean | null
  structured_output?: string
  supports_image_input?: boolean
  test_status?: Partial<Record<Service, boolean>>
}
const emit = defineEmits<{ status: [value: ModelConfig]; error: [message: string] }>()
const dialog = ref<HTMLDialogElement | null>(null)
const form = ref<HTMLFormElement | null>(null)
const saved = ref<ModelConfig>({ configured: false, has_api_key: false })
const loaded = ref(false)
const busy = ref(false)
const action = ref('')
const message = ref('')
const messageState = ref<'' | 'success' | 'error'>('')
const tests = reactive<Record<Service, string | null>>({ llm: null, decision: null, tavily: null })
const results = reactive<Record<Service, { text: string; code?: string; state: string; statusCode?: number }>>({
  llm: { text: '尚未测试', state: '' },
  decision: { text: '测试通过后可启用', state: '' },
  tavily: { text: '测试通过后可启用', state: '' },
})
const fields = reactive({
  provider: 'custom' as Provider,
  baseUrl: '', model: '', apiKey: '', noAuth: false,
  decisionModel: '', decisionBaseUrl: '', decisionEnabled: false, decisionKey: '',
  searchProvider: 'tavily' as 'tavily' | 'deepseek', deepseekKey: '',
  tavilyEnabled: false, tavilyKey: '',
  supportsImages: false,
  maxTokens: 128000 as number | string, planningMaxTokens: '' as number | string,
  maxInputTokens: 600000 as number | string,
  temperature: 0.1 as number | string,
})
const controlsDisabled = computed(() => busy.value || !loaded.value)
const normalizedUrl = (value: string) => value.trim().replace(/\/+$/, '')
const nativeDecisionEndpoint = computed(() => {
  try {
    return new URL(fields.decisionBaseUrl.trim()).pathname.replace(/\/+$/, '').endsWith('/systemone')
  } catch {
    return false
  }
})
let pending: AbortController | null = null
let initial: AbortController | null = null
let everOpened = false

function result(text: string, state: '' | 'success' | 'error' = '') {
  message.value = text
  messageState.value = state
}
function serviceResult(service: Service, text: string, state = '', code = '', statusCode?: number) {
  results[service] = { text, code: code || undefined, state, statusCode }
}
function serviceText(result: { text: string; code?: string; state: string; statusCode?: number }) {
  // Failure messages are already sanitized and translated by the API. A generic
  // test code must never replace an actionable authentication or parameter error.
  const text = result.state === 'error' && result.text
    ? localizeRuntimeText(result.text)
    : localizeModelTestCode(result.code) || localizeRuntimeText(result.text)
  return result.statusCode ? `HTTP ${result.statusCode} · ${text}` : text
}
function data() {
  return {
    provider_mode: fields.provider,
    base_url: fields.baseUrl.trim(),
    model: fields.model.trim(),
    api_key: fields.apiKey || null,
    no_auth: fields.noAuth,
    decision_model: fields.decisionModel.trim(),
    decision_enabled: fields.decisionEnabled,
    decision_base_url: fields.decisionBaseUrl.trim(),
    decision_api_key: fields.decisionKey || null,
    search_provider: fields.searchProvider,
    deepseek_api_key: fields.deepseekKey || null,
    tavily_enabled: fields.tavilyEnabled,
    tavily_api_key: fields.tavilyKey || null,
    max_tokens: Number(fields.maxTokens),
    planning_max_tokens: fields.planningMaxTokens !== '' ? Number(fields.planningMaxTokens) : null,
    max_input_tokens: Number(fields.maxInputTokens),
    temperature: Number(fields.temperature),
    enable_thinking: normalizedUrl(fields.baseUrl) === normalizedUrl(saved.value.base_url || '') ? saved.value.enable_thinking ?? null : null,
    structured_output: saved.value.structured_output || 'prompt',
    supports_image_input: fields.supportsImages,
  }
}
function fingerprint(service: Service) {
  const value = data()
  if (service === 'tavily') return JSON.stringify([value.search_provider, value.tavily_api_key, value.deepseek_api_key, Boolean(saved.value.has_tavily_api_key), Boolean(saved.value.has_deepseek_api_key)])
  if (service === 'decision') return JSON.stringify([value.decision_base_url, value.decision_model, value.decision_api_key, Boolean(saved.value.has_decision_api_key)])
  return JSON.stringify([
    value.provider_mode, normalizedUrl(value.base_url), value.model, value.api_key, value.no_auth,
    value.max_tokens, value.planning_max_tokens, value.temperature, value.enable_thinking,
    value.structured_output, value.supports_image_input, Boolean(saved.value.has_api_key),
  ])
}
function tested(service: Service) {
  return tests[service] !== null && tests[service] === fingerprint(service)
}
function retainedLlmKey() {
  return Boolean(saved.value.has_api_key) && normalizedUrl(fields.baseUrl) === normalizedUrl(saved.value.base_url || '')
}
function clearSecrets() {
  fields.apiKey = fields.decisionKey = fields.tavilyKey = fields.deepseekKey = ''
}
function fill() {
  const value = saved.value
  Object.assign(fields, {
    provider: 'custom',
    baseUrl: value.base_url || '', model: value.model || '',
    decisionModel: value.decision_model || '', decisionBaseUrl: value.decision_base_url || '',
    searchProvider: value.search_provider || 'tavily',
    noAuth: Boolean(value.no_auth),
    maxTokens: value.max_tokens ?? 128000,
    planningMaxTokens: value.planning_max_tokens || '',
    maxInputTokens: value.max_input_tokens ?? 600000,
    temperature: value.temperature ?? 0.1,
    decisionEnabled: Boolean(value.decision_enabled),
    tavilyEnabled: Boolean(value.tavily_enabled),
    supportsImages: Boolean(value.supports_image_input),
  })
  clearSecrets()
  for (const service of ['llm', 'decision', 'tavily'] as Service[]) {
    tests[service] = value.test_status?.[service] ? fingerprint(service) : null
    serviceResult(service, tested(service) ? '测试通过' : service === 'llm' ? '尚未测试' : '测试通过后可启用', tested(service) ? 'success' : '')
  }
  if (!tested('decision')) fields.decisionEnabled = false
  if (!tested('tavily')) fields.tavilyEnabled = false
}
interface ValidationError {
  loc?: (string | number)[]
  msg?: string
  type?: string
}
const settingLabels: Record<string, string> = {
  provider_mode: '接口方式', base_url: '接口地址', model: '生成模型', api_key: 'LLM API Key',
  no_auth: '免密访问', decision_model: '判断模型', decision_enabled: '判断服务',
  decision_api_key: 'Decision API Key', decision_base_url: '接口地址', deepseek_api_key: 'DeepSeek API Key', search_provider: '网络搜索', tavily_enabled: '网络搜索', tavily_api_key: 'Tavily API Key',
  max_tokens: '最大输出长度', planning_max_tokens: '目录规划输出长度', temperature: '温度',
  max_input_tokens: '最大输入 Token', context_window: '模型上下文窗口', tokenizer_encoding: '本地 token 估算编码',
  enable_thinking: '思考模式', structured_output: '结构化输出',
  supports_image_input: '图片输入',
}
function validationMessage(errors: ValidationError[]) {
  const label = (error: ValidationError) => settingLabels[String(error.loc?.[1])] || '配置项'
  const unsupported = errors.filter(error => error.type === 'extra_forbidden')
  const messages = unsupported.length
    ? [`当前后台不支持以下配置项：${[...new Set(unsupported.map(label))].join('、')}。请更新或重启 Folio 后台，再刷新页面重试。`]
    : []
  for (const error of errors.filter(error => error.type !== 'extra_forbidden')) {
    if (error.msg) messages.push(`${label(error)}：${runtimeSourceText(error.msg)}`)
  }
  return messages.join('\n') || '模型配置校验失败，请检查填写内容。'
}
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...options, cache: 'no-store', headers: acceptLanguageHeaders(options.headers) })
  const value = await response.json().catch(() => ({}))
  registerRuntimeTranslations(value)
  if (!response.ok) {
    const detail = Array.isArray(value.detail)
      ? validationMessage(value.detail) : value.detail
    throw new Error(typeof detail === 'string' ? detail : '无法操作模型配置，请检查后台服务。')
  }
  return value as T
}
function errorText(error: unknown) {
  return error instanceof Error ? runtimeSourceText(error.message) : '操作失败，请检查后台服务。'
}
function finish(controller: AbortController) {
  if (pending === controller) { pending = null; busy.value = false; action.value = '' }
}
async function open() {
  if (!dialog.value || dialog.value.open) return
  everOpened = true
  initial?.abort()
  dialog.value.showModal()
  loaded.value = false
  busy.value = true
  action.value = 'load'
  result('正在读取配置…')
  const controller = new AbortController()
  pending = controller
  try {
    const value = await request<ModelConfig>('/settings/model', { signal: controller.signal })
    if (pending !== controller) return
    saved.value = value
    loaded.value = true
    emit('status', value)
    fill()
    result('')
  } catch (error) {
    if (!controller.signal.aborted && pending === controller) result(errorText(error), 'error')
  } finally { finish(controller) }
}
function closed() {
  pending?.abort()
  pending = null
  clearSecrets()
  tests.llm = tests.decision = tests.tavily = null
  busy.value = false
  loaded.value = false
  action.value = ''
}
function validFields(ids: string[]) {
  return ids.every(id => form.value?.querySelector<HTMLInputElement | HTMLSelectElement>(`#${id}`)?.reportValidity() ?? false)
}
function requireKey(service: Service, id: string, key: string, retained: boolean | undefined, noAuth = false) {
  if (noAuth || key.trim() || retained) return true
  serviceResult(service, '请填写此服务的 API Key。', 'error')
  form.value?.querySelector<HTMLInputElement>(`#${id}`)?.focus()
  return false
}
function validateService(service: Service) {
  if (service === 'tavily') return fields.searchProvider === 'deepseek'
    ? requireKey(service, 'deepseek-api-key', fields.deepseekKey, saved.value.has_deepseek_api_key)
    : requireKey(service, 'tavily-api-key', fields.tavilyKey, saved.value.has_tavily_api_key)
  if (service === 'decision') return validFields(['decision-base-url', 'decision-model']) && requireKey(service, 'decision-api-key', fields.decisionKey,
    saved.value.has_decision_api_key && normalizedUrl(fields.decisionBaseUrl) === normalizedUrl(saved.value.decision_base_url || ''))
  return validFields(['model-base-url', 'model-name']) && requireKey(service, 'model-api-key', fields.apiKey, retainedLlmKey(), fields.noAuth)
}
async function test(service: Service) {
  if (controlsDisabled.value || !validateService(service)) return
  const controller = new AbortController()
  pending = controller
  const inputFingerprint = fingerprint(service)
  tests[service] = null
  if (service === 'decision') fields.decisionEnabled = false
  if (service === 'tavily') fields.tavilyEnabled = false
  busy.value = true
  action.value = service
  result('')
  serviceResult(service, '正在测试，请稍候…')
  try {
    const response = await request<{ ok: boolean; code?: string; message?: string; diagnostics?: { status_code?: number } }>(service === 'llm' ? '/settings/model/test' : `/settings/model/test/${service === 'tavily' ? 'search' : service}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data()), signal: controller.signal,
    })
    if (pending !== controller) return
    if (response.ok && inputFingerprint === fingerprint(service)) tests[service] = inputFingerprint
    serviceResult(
      service,
      response.message || (response.ok ? '测试通过' : '测试未通过'),
      response.ok ? 'success' : 'error',
      response.code,
      Number.isInteger(response.diagnostics?.status_code) ? response.diagnostics?.status_code : undefined,
    )
  } catch (error) {
    if (!controller.signal.aborted && pending === controller) serviceResult(service, errorText(error), 'error')
  } finally { finish(controller) }
}
async function save() {
  if (controlsDisabled.value || !form.value?.reportValidity() || !validateService('llm')) return
  if ((fields.decisionEnabled && !tested('decision')) || (fields.tavilyEnabled && !tested('tavily'))) {
    result('请先测试当前服务配置，再勾选启用。', 'error')
    if (!tested('decision')) fields.decisionEnabled = false
    if (!tested('tavily')) fields.tavilyEnabled = false
    return
  }
  const controller = new AbortController()
  pending = controller
  const body = data()
  busy.value = true
  action.value = 'save'
  result('正在保存…')
  try {
    const value = await request<ModelConfig>('/settings/model', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: controller.signal,
    })
    if (pending !== controller) return
    saved.value = value
    emit('status', value)
    fill()
    result('配置已保存，新任务将使用此配置。', 'success')
  } catch (error) {
    if (!controller.signal.aborted && pending === controller) result(errorText(error), 'error')
  } finally { finish(controller) }
}
function changed(event: Event) {
  if (controlsDisabled.value) return
  const control = event.target as HTMLInputElement | HTMLSelectElement
  // Native selectors can yield between input and change. Their v-model commits
  // on change, so an earlier render would restore the old selection/checkbox.
  if (event.type === 'input' && (control instanceof HTMLSelectElement || ['checkbox', 'radio'].includes(control.type))) return
  result('')
  const id = control.id
  let affected: Service[] = []
  if (['decision-base-url', 'decision-model', 'decision-api-key'].includes(id)) affected = ['decision']
  else if (['tavily-api-key', 'deepseek-api-key', 'search-provider'].includes(id)) affected = ['tavily']
  else if (!['decision-enabled', 'tavily-enabled'].includes(id)) affected = ['llm']
  for (const service of affected) {
    tests[service] = null
    serviceResult(service, '配置已修改，请重新测试。')
    if (service === 'decision') fields.decisionEnabled = false
    if (service === 'tavily') fields.tavilyEnabled = false
  }
}
onMounted(async () => {
  initial = new AbortController()
  try {
    const value = await request<ModelConfig>('/settings/model', { signal: initial.signal })
    if (!everOpened) { saved.value = value; emit('status', value) }
  } catch (error) {
    if (!initial.signal.aborted) emit('error', '模型配置未读取：' + errorText(error))
  }
})
onBeforeUnmount(() => { initial?.abort(); closed() })
defineExpose({ open })
</script>

<template>
  <dialog ref="dialog" id="model-settings-dialog" class="settings-dialog" aria-labelledby="model-settings-title" @close="closed">
    <div class="settings-heading">
      <div><span class="sidebar-label">WORKSPACE SETTINGS</span><h2 id="model-settings-title">{{ tr('模型与服务') }}</h2></div>
      <button id="model-settings-close" type="button" class="icon-button" :aria-label="tr('关闭模型设置')" @click="dialog?.close()"><Icon name="close" /></button>
    </div>
    <p class="settings-description">{{ tr('连接你的生成模型，按需开启判断服务和网络搜索。') }}</p>
    <form ref="form" id="model-settings-form" autocomplete="off" @submit.prevent="save" @input="changed" @change="changed">
      <fieldset id="model-settings-fields" :disabled="controlsDisabled">
        <section class="model-service" aria-labelledby="llm-service-title">
          <h3 id="llm-service-title">{{ tr('生成模型') }} <small>LLM</small></h3>
          <fieldset id="custom-model-fields" class="model-mode-fields">
            <label for="model-base-url">{{ tr('接口地址') }} <span>Base URL</span></label>
            <input id="model-base-url" v-model="fields.baseUrl" name="base_url" type="url" required placeholder="https://api.example.com/v1" maxlength="3500" :spellcheck="false" autocomplete="off">
            <small class="field-help">{{ tr('填写基础地址，不包含 /chat/completions。') }}</small>
            <label for="model-name">{{ tr('模型名称') }}</label>
            <input id="model-name" v-model="fields.model" name="model" required :placeholder="tr('服务提供的模型 ID')" maxlength="200" :spellcheck="false" autocomplete="off">
            <label for="model-api-key">LLM API Key <span id="saved-key-label">{{ retainedLlmKey() ? tr('已保存 · 留空保持不变') : '' }}</span></label>
            <input id="model-api-key" v-model="fields.apiKey" name="api_key" type="password" :disabled="fields.noAuth" :placeholder="retainedLlmKey() ? tr('已保存，填写可替换') : tr('输入 API Key')" maxlength="4096" autocomplete="new-password" :spellcheck="false">
            <label class="checkbox-label"><input id="model-no-auth" v-model="fields.noAuth" type="checkbox">{{ tr('内网服务无需 API Key') }}</label>
          </fieldset>
          <div class="service-test-row"><button id="test-model-button" type="button" class="secondary-button" :disabled="controlsDisabled" @click="test('llm')">{{ action === 'llm' ? tr('测试中…') : tr('测试 LLM') }}</button><span id="model-test-status" class="service-test-status" :class="{ 'is-success': results.llm.state === 'success', 'is-error': results.llm.state === 'error' }" role="status" aria-live="polite">{{ serviceText(results.llm) }}</span></div>
        </section>
        <section class="model-service" aria-labelledby="decision-service-title">
          <h3 id="decision-service-title">{{ tr('判断服务') }} <small>{{ tr('可选') }}</small></h3>
          <label for="decision-base-url">{{ tr('接口地址') }} <span>Endpoint</span></label>
          <input id="decision-base-url" v-model="fields.decisionBaseUrl" type="url" placeholder="https://api.example.com/v1" :required="fields.decisionEnabled" aria-describedby="decision-endpoint-help">
          <small id="decision-endpoint-help" class="field-help">{{ nativeDecisionEndpoint ? tr('Jev 原生接口：地址原样使用。') : tr('OpenAI 兼容接口：填写 Base URL，不含 /chat/completions。') }}</small>
          <label for="decision-model">{{ tr('模型名称') }}</label>
          <input id="decision-model" v-model="fields.decisionModel" :placeholder="nativeDecisionEndpoint ? 'jev-latest' : tr('服务提供的模型 ID')" :required="fields.decisionEnabled" :aria-describedby="nativeDecisionEndpoint ? 'decision-model-help' : undefined">
          <small v-if="nativeDecisionEndpoint" id="decision-model-help" class="field-help">{{ tr('填写服务实际提供的模型 ID，例如 jev-latest。') }}</small>
          <label for="decision-api-key">API Key <span>{{ saved.has_decision_api_key ? tr('已保存 · 留空保持不变') : '' }}</span></label>
          <input id="decision-api-key" v-model="fields.decisionKey" type="password" autocomplete="new-password" :placeholder="tr('输入 API Key')">
          <div class="service-test-row"><button id="test-decision-button" type="button" class="secondary-button" :disabled="controlsDisabled" @click="test('decision')">{{ action === 'decision' ? tr('测试中…') : tr('测试判断模型') }}</button><span id="decision-test-status" class="service-test-status" :class="{ 'is-success': results.decision.state === 'success', 'is-error': results.decision.state === 'error' }" role="status" aria-live="polite">{{ serviceText(results.decision) }}</span></div>
          <label class="checkbox-label"><input id="decision-enabled" v-model="fields.decisionEnabled" type="checkbox" :disabled="controlsDisabled || !tested('decision')">{{ tr('启用判断服务') }}</label>
        </section>
        <section class="model-service" aria-labelledby="tavily-service-title">
          <h3 id="tavily-service-title">{{ tr('网络搜索') }} <small>{{ tr('可选') }}</small></h3>
          <label for="search-provider">{{ tr('搜索服务') }}</label>
          <select id="search-provider" v-model="fields.searchProvider"><option value="tavily">Tavily</option><option value="deepseek">DeepSeek Search</option></select>
          <template v-if="fields.searchProvider === 'deepseek'">
            <label for="deepseek-api-key">DeepSeek API Key <span>{{ saved.has_deepseek_api_key ? tr('已保存 · 留空保持不变') : '' }}</span></label>
            <input id="deepseek-api-key" v-model="fields.deepseekKey" type="password" autocomplete="new-password" :placeholder="tr('输入 API Key')">
          </template>
          <template v-else>
          <label for="tavily-api-key">Tavily API Key <span id="tavily-saved-key-label">{{ saved.has_tavily_api_key ? tr('已保存 · 留空保持不变') : '' }}</span></label>
          <input id="tavily-api-key" v-model="fields.tavilyKey" type="password" :placeholder="saved.has_tavily_api_key ? tr('已保存，填写可替换') : tr('选填 Tavily API Key')" maxlength="4096" autocomplete="new-password" :spellcheck="false">
          </template>
          <div class="service-test-row"><button id="test-tavily-button" type="button" class="secondary-button" :disabled="controlsDisabled" @click="test('tavily')">{{ action === 'tavily' ? tr('测试中…') : tr('测试搜索服务') }}</button><span id="tavily-test-status" class="service-test-status" :class="{ 'is-success': results.tavily.state === 'success', 'is-error': results.tavily.state === 'error' }" role="status" aria-live="polite">{{ serviceText(results.tavily) }}</span></div>
          <label class="checkbox-label"><input id="tavily-enabled" v-model="fields.tavilyEnabled" type="checkbox" :disabled="controlsDisabled || !tested('tavily')">{{ tr('启用网络搜索') }}</label>
        </section>
        <details class="advanced-settings">
          <summary>{{ tr('高级参数') }}</summary>
          <div class="advanced-grid">
            <div><label for="model-max-input-tokens">{{ tr('最大输入 Token') }}</label><input id="model-max-input-tokens" v-model="fields.maxInputTokens" type="number" min="1" step="1" required></div>
            <div><label for="model-max-tokens">{{ tr('最大输出 Token') }}</label><input id="model-max-tokens" v-model="fields.maxTokens" type="number" min="256" step="1" required></div>
            <div><label for="model-temperature">Temperature</label><input id="model-temperature" v-model="fields.temperature" type="number" min="0" max="2" step="0.1" required></div>
          </div>
          <small class="field-help">{{ tr('默认输入 600,000 Token、输出 128,000 Token。输出是额度上限，模型可以提前结束；实际请求仍须符合服务支持的模型容量。') }}</small>
          <label for="model-planning-max-tokens">{{ tr('目录规划最大输出 Token') }}</label>
          <input id="model-planning-max-tokens" v-model="fields.planningMaxTokens" type="number" min="256" step="1" :placeholder="tr('留空则沿用最大输出 Token')">
          <small class="field-help">{{ tr('仅用于目录规划和目录修正。') }}</small>
          <label class="checkbox-label"><input id="supports-image-input" v-model="fields.supportsImages" type="checkbox">{{ tr('当前生成模型支持图片输入') }}</label>
        </details>
      </fieldset>
      <div v-if="message" id="model-settings-result" class="settings-result" :class="{ error: messageState === 'error', 'is-success': messageState === 'success' }" role="status" aria-live="polite">{{ localizeRuntimeText(message) }}</div>
      <p class="settings-note">{{ tr('密钥仅保存在服务端，不写入浏览器存储。可选服务测试通过后可启用，保存后对新任务生效。') }}</p>
      <div class="settings-actions model-settings-actions"><button id="save-model-button" type="submit" class="primary-button" :disabled="controlsDisabled"><Icon name="check" />{{ action === 'save' ? tr('保存中…') : tr('保存配置') }}</button></div>
    </form>
  </dialog>
</template>
