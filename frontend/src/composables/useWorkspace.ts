import { computed, onBeforeUnmount, onMounted, reactive } from 'vue';
import { protocolDiagnosticFields } from '../diagnostics';
import { acceptLanguageHeaders, registerRuntimeTranslations, runtimeSourceText, tr, trf } from '../i18n';
import type { Activity, Artifact, ConfirmationData, ConfirmationHistory, DisplayField, FailureDetails, FileRole, HistoryItem, Message, PreviewBlock, ReviewDetails, DirectoryModelStatus, DirectoryProgressActivity, DirectoryPreviewSource, DirectoryReview, StyleAnalysisReport, TaskUsage, UploadedFile, WorkspaceState } from '../types';

const MAX_FILES = 200;
const UPLOAD_CONCURRENCY = 3;
// UI keys also need to work when a self-hosted instance uses HTTP on a LAN.
// randomUUID is restricted to secure contexts; getRandomValues is not.
function uiId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
const storage = {
  get(key: string) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key: string, value: string) { try { localStorage.setItem(key, value); } catch { /* Server remains authoritative. */ } },
  remove(key: string) { try { localStorage.removeItem(key); } catch { /* Storage may be unavailable. */ } },
};
const errorText = (error: unknown) => error instanceof Error ? runtimeSourceText(error.message) : '请求失败，请稍后重试。';
class ApiError extends Error { constructor(message: string, readonly status: number) { super(message); } }
async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...options, headers: acceptLanguageHeaders(options.headers) });
  const data = await response.json(); registerRuntimeTranslations(data);
  if (!response.ok) throw new ApiError(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入或服务配置。', response.status);
  return data as T;
}
const json = (value: unknown): RequestInit => ({ headers: acceptLanguageHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(value) });
interface ConversationData {
  id: string;
  title?: string;
  title_is_default?: boolean;
  pinned?: boolean;
  files?: { id: string; name: string; size: number; role?: FileRole }[];
  draft?: string;
  messages: { role: 'user' | 'assistant'; content: string; content_origin?: 'model' | 'system'; interrupted?: boolean; error_diagnostics?: Record<string, unknown>; activities?: DirectoryProgressActivity[]; activity_label?: string; style_analysis?: StyleAnalysisReport }[];
  directory_review?: DirectoryReview;
  active_job?: string;
  active_job_status?: string;
  active_job_failure?: FailureDetails;
  pending_confirmation_job?: string;
  confirmation_history?: ConfirmationHistory[];
}
interface StreamEvent extends Partial<DirectoryReview>, Partial<Artifact> {
  content_origin?: 'model' | 'system';
  layout_note?: string;
  confirmation_history?: ConfirmationHistory[];
  text?: string;
  job_id?: string;
  resumed?: boolean;
  elapsed_seconds?: number;
  step_elapsed_seconds?: number;
  model_status?: DirectoryModelStatus;
  activity?: DirectoryProgressActivity;
  event_id?: number | string;
  review?: ReviewDetails;
  failure?: FailureDetails;
  diagnostics?: Record<string, unknown>;
  task_usage?: TaskUsage[];
  document_names?: string[];
  style_url?: string;
  summary?: string;
  cache_hit?: boolean;
}
interface LiveResponse {
  receive(type: string, data: StreamEvent): void;
  prefillFailure(data: StreamEvent): void;
  prefillJobState(data: StreamEvent): void;
  detach(): void;
  end(): void;
  error(message: string): void;
}
interface PendingRequest {
  epoch: number;
  id: string;
  controller: AbortController;
  view: LiveResponse;
  events: { type: string; data: StreamEvent }[];
}

export function requestDiagnostics(diagnostics?: Record<string, unknown>): DisplayField[] {
  if (!diagnostics) return [];
  const fields: DisplayField[] = [];
  const stages: Record<string, string> = { intent: tr('意图识别'), source_upload: tr('资料上传'), style_analysis: tr('风格分析'), directory_planning: tr('目录规划'), directory_revision: tr('目录修改'), job_creation: tr('启动文档任务'), generation: tr('正文生成'), chat: tr('对话回复') };
  if (typeof diagnostics.stage === 'string' && Object.hasOwn(stages, diagnostics.stage)) fields.push({ label: tr('阶段'), value: stages[diagnostics.stage]! });
  if (typeof diagnostics.status_code === 'number' && Number.isInteger(diagnostics.status_code) && diagnostics.status_code >= 100 && diagnostics.status_code <= 599) fields.push({ label: tr('HTTP 状态'), value: String(diagnostics.status_code) });
  if (typeof diagnostics.elapsed_ms === 'number' && Number.isFinite(diagnostics.elapsed_ms) && diagnostics.elapsed_ms >= 0) fields.push({ label: tr('耗时'), value: `${(diagnostics.elapsed_ms / 1000).toFixed(2)} ${tr('秒')}` });
  const safeCodes = new Set(['call_budget', 'decision_unavailable', 'selection_context_limit', 'directory_context_limit', 'directory_invalid', 'context_limit', 'authentication', 'billing', 'rate_limit', 'timeout', 'configuration', 'length_limit', 'repetition', 'protocol', 'schema_unsupported', 'model_error']);
  if (typeof diagnostics.code === 'string' && safeCodes.has(diagnostics.code)) fields.push({ label: tr('错误代码'), value: diagnostics.code });
  fields.push(...protocolDiagnosticFields(diagnostics));
  if (diagnostics.backend_error === 'invalid_json_schema') fields.push({ label: tr('接口诊断'), value: tr('结构化输出 Schema 无效') });
  if (diagnostics.schema_constraint === 'required_all_properties') fields.push({ label: tr('Schema 约束'), value: tr('所有对象字段都必须声明为必填') });
  return fields;
}

export function useWorkspace() {
  const state = reactive<WorkspaceState>({
    files: [], messages: [], conversationId: null, title: '新的文档', titleIsLabel: true, text: '', saveStatus: '已保存',
    historyItems: [], historyMore: false, historyError: false, deletingConversation: null, updatingConversation: null, restoring: true, restoreFailed: false,
    running: false, generating: false, activeJob: null, activeJobStatus: null, retryJob: null, taskControlPending: false,
    directoryReview: null,
    confirmationJob: null, confirmation: null, confirmationAnswers: {}, confirmationCalls: 200,
    confirmationClosedComments: {}, confirmationHistory: [],
    confirmationLoading: false, confirmationSubmitting: false, confirmationError: '',
    confirmationCollapsed: false, confirmationSkipped: false,
    preview: { title: '文档预览', titleIsLabel: true, downloadUrl: '', kind: 'directory', status: '', pages: [], blocks: [], showStructure: true },
    previewFootnote: '根据资料和对话规划目录，确认后生成正文。', previewOpen: false,
    materialsOpen: false, sidebarOpen: false, toast: '', toastTone: 'neutral', announcement: '',
  });
  let viewEpoch = 0;
  let disposed = false;
  let controller: AbortController | null = null;
  let activeResponse: LiveResponse | null = null;
  const pendingMessages = new Map<string, PendingRequest>();
  let creatingConversation: Promise<void> | null = null;
  let draftTimer: ReturnType<typeof setTimeout> | undefined;
  let toastTimer: ReturnType<typeof setTimeout> | undefined;
  let draftDirty = false;
  let draftQueue: Promise<void> = Promise.resolve();
  let previewVersion = 0;
  let historyRequest = 0;
  let confirmationVersion = 0;
  const pendingConfirmations = new Set<string>();
  let resolveReady!: () => void;
  const ready = new Promise<void>(resolve => { resolveReady = resolve; });
  const reviewing = computed(() => state.directoryReview?.phase === 'directory_review');
  const sourcesBusy = computed(() => state.files.some(file => file.queued || file.uploading || file.removing || file.changingRole));
  const controlLabel = computed(() => state.activeJob ? state.activeJobStatus === 'paused' ? tr('继续生成') : state.activeJobStatus === 'pausing' ? tr('正在停止') : tr('停止生成') : tr('停止回答'));

  function toast(message: string, tone: WorkspaceState['toastTone'] = 'neutral') {
    clearTimeout(toastTimer);
    state.toast = message;
    state.toastTone = tone;
    toastTimer = setTimeout(() => { state.toast = ''; }, 5000);
  }
  function isCurrentView(epoch: number, id: string | null) { return !disposed && epoch === viewEpoch && id === state.conversationId; }
  function setRunning(value: boolean) { state.running = value || Boolean(state.activeJob) || Boolean(state.confirmationJob && pendingConfirmations.has(state.confirmationJob)); }
  function createMessage(role: 'user' | 'assistant', text: string, withFiles = false): Message {
    const message = reactive<Message>({ id: uiId(), role, text, working: false, notes: [], activities: [], ...(withFiles && state.files.length ? { files: state.files.map(file => file.file.name) } : {}) });
    state.messages.push(message);
    return message;
  }
  async function ensureConversation() {
    if (state.conversationId) return;
    if (!creatingConversation) {
      const epoch = viewEpoch;
      creatingConversation = api<ConversationData>('/conversations', { method: 'POST' }).then(result => {
        if (epoch !== viewEpoch || disposed) return;
        state.conversationId = result.id;
        storage.set('folio.conversation', result.id);
      }).finally(() => { creatingConversation = null; });
    }
    await creatingConversation;
  }
  interface UploadTask { item: UploadedFile; file: File; conversationId: string; epoch: number; resolve: () => void }
  const uploadQueue: UploadTask[] = [];
  const uploadControllers = new Map<string, AbortController>();
  const sourceFiles = new Map<string, File>();
  function cancelUploads() {
    for (const task of uploadQueue.splice(0)) { task.item.queued = false; task.resolve(); }
    for (const abort of uploadControllers.values()) abort.abort();
    uploadControllers.clear();
    sourceFiles.clear();
  }
  function applySourceMutation(data: { directory_invalidated?: boolean }) {
    if (data.directory_invalidated) clearDirectoryReview();
  }
  function enqueueUpload(item: UploadedFile, file: File, conversationId: string, epoch: number) {
    item.queued = true; item.uploading = false; item.failed = false; item.error = '';
    return new Promise<void>(resolve => { uploadQueue.push({ item, file, conversationId, epoch, resolve }); pumpUploads(); });
  }
  function pumpUploads() {
    while (uploadControllers.size < UPLOAD_CONCURRENCY && uploadQueue.length) {
      const task = uploadQueue.shift()!;
      if (!isCurrentView(task.epoch, task.conversationId) || !state.files.some(file => file.id === task.item.id)) { task.resolve(); continue; }
      const abort = new AbortController();
      uploadControllers.set(task.item.id, abort);
      task.item.queued = false; task.item.uploading = true;
      void (async () => {
        try {
          const result = await api<{ id: string; directory_invalidated?: boolean }>(`/conversations/${task.conversationId}/sources?filename=${encodeURIComponent(task.file.name)}`, { method: 'POST', body: task.file, signal: abort.signal });
          if (!isCurrentView(task.epoch, task.conversationId)) return;
          task.item.server_id = result.id; task.item.failed = false;
          sourceFiles.delete(task.item.id);
          applySourceMutation(result);
          state.saveStatus = draftDirty ? '保存中' : '已保存';
        } catch (error) {
          if (!isCurrentView(task.epoch, task.conversationId)) return;
          task.item.failed = true;
          task.item.error = error instanceof Error && error.name === 'AbortError' ? '上传已取消，请重试。' : errorText(error);
        } finally {
          task.item.uploading = false;
          uploadControllers.delete(task.item.id);
          task.resolve(); pumpUploads();
        }
      })();
    }
  }
  async function addFiles(selected: File[] | FileList) {
    if (state.running || state.restoring || state.restoreFailed) { toast('请等待当前处理结束，再调整资料。', state.restoreFailed ? 'warning' : 'neutral'); return; }
    const epoch = viewEpoch;
    try { await ensureConversation(); } catch (error) { toast(errorText(error), 'error'); return; }
    if (!isCurrentView(epoch, state.conversationId) || !state.conversationId) return;
    const id = state.conversationId;
    const errors: string[] = []; const uploads: Promise<void>[] = [];
    for (const file of Array.from(selected)) {
      if (!/\.(docx|md|txt|xlsx|pdf|pptx|png|jpg|jpeg|tif|tiff|webp)$/i.test(file.name)) { errors.push(`${file.name}：请使用 Word、Excel、PDF、PPTX、文本或 PNG/JPEG/TIFF/WebP 图片。`); continue; }
      if (!file.size) { errors.push(`${file.name}：文件不能为空。`); continue; }
      if (state.files.some(item => item.file.name === file.name && item.file.size === file.size && item.file.lastModified === file.lastModified)) continue;
      if (state.files.length >= MAX_FILES) { errors.push('每个会话最多上传 200 份资料。'); break; }
      const item = reactive<UploadedFile>({ id: uiId(), file: { name: file.name, size: file.size, lastModified: file.lastModified }, role: 'source', queued: true });
      state.files.push(item); sourceFiles.set(item.id, file);
      uploads.push(enqueueUpload(item, file, id, epoch));
    }
    if (state.files.length) state.materialsOpen = true;
    if (errors.length) toast(errors.slice(0, 4).join('\n') + (errors.length > 4 ? `\n另有 ${errors.length - 4} 份文件未加入。` : ''), 'warning');
    await Promise.all(uploads);
    if (!isCurrentView(epoch, id)) return;
    if (uploads.length) {
      const failed = state.files.filter(file => file.failed).length;
      toast(failed ? `有 ${failed} 份资料上传失败，可在资料库中重试。` : '资料已上传并保存，可继续描述文档需求。', failed ? 'error' : 'success');
      void refreshHistory();
    }
  }
  async function retryFile(id: string) {
    const item = state.files.find(file => file.id === id); const file = sourceFiles.get(id);
    if (!item || !file || !item.failed || !state.conversationId || state.running || state.restoring) return;
    await enqueueUpload(item, file, state.conversationId, viewEpoch);
  }
  async function removeFile(id: string) {
    if (state.running || state.restoring || state.restoreFailed) return;
    const item = state.files.find(file => file.id === id);
    if (!item || item.removing || state.files.some(file => file.changingRole)) return;
    if (item.uploading) { toast('请等待这份资料上传完成后再移除。'); return; }
    if (!item.server_id) {
      const queued = uploadQueue.findIndex(task => task.item.id === id);
      if (queued >= 0) uploadQueue.splice(queued, 1)[0]!.resolve();
      sourceFiles.delete(id); state.files = state.files.filter(file => file.id !== id); return;
    }
    const epoch = viewEpoch; const conversationId = state.conversationId;
    item.removing = true;
    try {
      const result = await api<{ directory_invalidated?: boolean }>(`/conversations/${conversationId}/sources/${item.server_id}`, { method: 'DELETE' });
      if (!isCurrentView(epoch, conversationId)) return;
      state.files = state.files.filter(file => file.id !== id); applySourceMutation(result);
      void refreshHistory();
    } catch (error) { if (isCurrentView(epoch, conversationId)) toast(`无法移除资料：${errorText(error)}`, 'error'); }
    finally { item.removing = false; }
  }
  async function changeFileRole(id: string, role: FileRole) {
    const item = state.files.find(file => file.id === id);
    if (!item?.server_id || !state.conversationId || item.role === role || sourcesBusy.value || state.running || state.restoring || state.restoreFailed) return;
    const epoch = viewEpoch; const conversationId = state.conversationId;
    item.changingRole = true;
    try {
      const result = await api<{ files: NonNullable<ConversationData['files']>; directory_invalidated: boolean }>(`/conversations/${conversationId}/sources/${item.server_id}`, { method: 'PATCH', ...json({ role }) });
      if (!isCurrentView(epoch, conversationId)) return;
      const savedRoles = new Map(result.files.map(file => [file.id, file.role || 'source']));
      for (const file of state.files) {
        if (file.server_id && savedRoles.has(file.server_id)) file.role = savedRoles.get(file.server_id)!;
      }
      applySourceMutation(result);
      const roleNotes: Record<FileRole, string> = {
        source: '已设为业务资料，请重新生成目录。',
        template: '已设为文档模板，将按其中的章节和格式生成。请重新生成目录。',
        style_reference: '已设为风格示例，只参考写法和排版，不作为业务事实。请重新生成目录。',
      };
      toast(roleNotes[role], 'success');
    } catch (error) { if (isCurrentView(epoch, conversationId)) toast(`文件用途未保存：${errorText(error)}`, 'error'); }
    finally { item.changingRole = false; }
  }
  function draftSnapshot() { return { text: state.text }; }
  function scheduleDraft() {
    if (state.restoring || state.running) return;
    draftDirty = true;
    if (state.conversationId) storage.set(`folio.draft.${state.conversationId}`, JSON.stringify(draftSnapshot()));
    state.saveStatus = '保存中';
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => { void flushDraft().catch(error => toast(errorText(error), 'error')); }, 400);
  }
  async function flushDraft() {
    clearTimeout(draftTimer);
    if (draftDirty) {
      await ensureConversation();
      const id = state.conversationId;
      if (!id) return;
      const snapshot = JSON.stringify(draftSnapshot());
      storage.set(`folio.draft.${id}`, snapshot);
      draftDirty = false;
      draftQueue = draftQueue.catch(() => {}).then(async () => {
        try {
          await api(`/conversations/${id}/draft`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: snapshot });
          if (storage.get(`folio.draft.${id}`) === snapshot) storage.remove(`folio.draft.${id}`);
          if (state.conversationId === id && !draftDirty) state.saveStatus = '已保存';
          await refreshHistory();
        } catch (error) {
          if (state.conversationId === id) { draftDirty = true; state.saveStatus = '保存失败 · 草稿暂存本机'; }
          throw error;
        }
      });
    }
    await draftQueue;
  }
  async function refreshHistory(append = false) {
    const request = ++historyRequest;
    try {
      const data = await api<{ conversations: HistoryItem[]; has_more: boolean }>(`/conversations?limit=50&offset=${append ? state.historyItems.length : 0}`);
      if (request !== historyRequest || disposed) return;
      state.historyItems = append ? [...state.historyItems, ...data.conversations.filter(item => !state.historyItems.some(existing => existing.id === item.id))] : data.conversations;
      state.historyMore = data.has_more;
      state.historyError = false;
    } catch { if (request === historyRequest) state.historyError = true; }
  }
  function clearDirectoryReview() {
    previewVersion++; state.directoryReview = null;
    state.preview = { title: '文档目录', titleIsLabel: true, downloadUrl: '', kind: 'directory', status: '', pages: [], blocks: [], showStructure: true };
  }
  function resetConversationView() {
    clearConfirmation();
    state.confirmationHistory = [];
    state.activeJob = null; state.activeJobStatus = null; state.retryJob = null; state.taskControlPending = false;
    state.generating = false; state.running = false;
    draftDirty = false; clearTimeout(draftTimer);
    state.restoreFailed = false; state.files = []; state.messages = [];
    clearDirectoryReview();
    state.saveStatus = '已保存'; state.text = ''; state.title = '新的文档'; state.titleIsLabel = true;
    state.materialsOpen = false; state.sidebarOpen = false; state.previewOpen = false;
  }
  async function switchConversation(id: string | null) {
    if (state.restoring || (id === state.conversationId && !state.restoreFailed)) return;
    const previousController = controller;
    const wasJob = Boolean(state.activeJob);
    state.restoring = true;
    try {
      if (!wasJob) await flushDraft();
      const persisted = id ? await api<ConversationData>(`/conversations/${id}`) : null;
      viewEpoch++; cancelUploads(); controller = null;
      activeResponse?.detach(); activeResponse = null;
      if (wasJob) previousController?.abort();
      resetConversationView(); state.conversationId = id;
      storage.set('folio.conversation', id || 'new');
      if (persisted) await restoreState(persisted);
      await refreshHistory();
    } catch (error) { toast(`无法切换会话：${errorText(error)}。当前内容已保留。`, 'error'); }
    finally { state.restoring = false; setRunning(Boolean(state.conversationId && pendingMessages.has(state.conversationId))); }
  }
  async function updateConversation(id: string, changes: { title?: string; pinned?: boolean }): Promise<boolean> {
    if (state.restoring || state.deletingConversation || state.updatingConversation) return false;
    state.updatingConversation = id;
    try {
      const updated = await api<HistoryItem>(`/conversations/${id}`, { method: 'PATCH', ...json(changes) });
      if (disposed) return true;
      historyRequest++;
      state.historyItems = state.historyItems.map(item => item.id === id ? { ...item, ...updated } : item);
      if (id === state.conversationId) { state.title = updated.title; state.titleIsLabel = Boolean(updated.title_is_default); }
      await refreshHistory();
      return true;
    } catch (error) {
      toast(`无法${changes.title !== undefined ? '重命名' : '更新置顶状态'}：${errorText(error)}`, 'error');
      return false;
    } finally { state.updatingConversation = null; }
  }
  async function renameConversation(id: string, title: string): Promise<boolean> {
    const cleaned = title.trim();
    if (!cleaned || cleaned.length > 200) {
      toast('对话名称需要填写 1～200 个字符。', 'error');
      return false;
    }
    return updateConversation(id, { title: cleaned });
  }
  async function toggleConversationPin(item: HistoryItem): Promise<boolean> {
    return updateConversation(item.id, { pinned: !item.pinned });
  }
  async function deleteConversation(id: string, deleteFiles = false): Promise<boolean> {
    if (state.restoring || state.deletingConversation || state.updatingConversation) return false;
    const current = id === state.conversationId;
    if (pendingMessages.has(id) || (current && state.confirmationSubmitting)) {
      toast('这个会话仍在处理，请先停止处理后再删除。', 'error');
      return false;
    }
    if (current && sourcesBusy.value) {
      toast('资料正在更新，请等待完成后再删除会话。', 'error');
      return false;
    }
    state.deletingConversation = id;
    state.restoring = true;
    if (current) clearTimeout(draftTimer);
    let deleted = false;
    let alreadyMissing = false;
    let retainedFiles = 0;
    try {
      // Let an in-flight save finish, but do not save a draft being discarded.
      if (current) await draftQueue.catch(() => {});
      try {
        const result = await api<{ deleted: string; files_retained?: number }>(`/conversations/${id}${deleteFiles ? '?delete_files=true' : ''}`, { method: 'DELETE' });
        retainedFiles = result.files_retained || 0;
      } catch (error) {
        // Another browser tab may already have deleted this conversation.
        if (!(error instanceof ApiError && error.status === 404)) throw error;
        alreadyMissing = true;
      }
      historyRequest++;
      state.historyItems = state.historyItems.filter(item => item.id !== id);
      storage.remove(`folio.draft.${id}`);
      try {
        for (const key of Object.keys(localStorage)) {
          if (key.startsWith(`folio.confirmation.${id}.`)) storage.remove(key);
        }
      } catch { /* Browser storage may be unavailable. */ }
      if (current) {
        viewEpoch++; cancelUploads();
        activeResponse?.detach(); activeResponse = null;
        controller?.abort(); controller = null;
        resetConversationView(); state.conversationId = null;
        storage.set('folio.conversation', 'new');
      } else if (storage.get('folio.conversation') === id) {
        storage.set('folio.conversation', state.conversationId || 'new');
      }
      deleted = true;
      await refreshHistory();
      toast(alreadyMissing ? '对话已不存在，列表已更新' : deleteFiles
        ? `对话及其独有文件已删除${retainedFiles ? '，共用文件已保留' : ''}`
        : '对话已删除，文件已保留');
      return true;
    } catch (error) {
      toast(`无法删除会话：${errorText(error)}`, 'error');
      return false;
    } finally {
      state.deletingConversation = null;
      state.restoring = false;
      setRunning(Boolean(state.conversationId && pendingMessages.has(state.conversationId)));
      if (!deleted && current && draftDirty) scheduleDraft();
    }
  }
  async function consumeStream(response: Response, callback: (type: string, data: StreamEvent) => void) {
    if (!response.ok) { const data = await response.json(); registerRuntimeTranslations(data); throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查材料或模型配置。'); }
    if (!response.headers.get('content-type')?.includes('text/event-stream') || !response.body) throw new Error('后台未返回事件流。');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, '\n');
        let end: number;
        while ((end = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
          let type = ''; const lines: string[] = [];
          for (const line of frame.split('\n')) {
            if (line.startsWith('event:')) type = line.slice(6).trim();
            if (line.startsWith('data:')) lines.push(line.slice(5).trimStart());
          }
          if (type && lines.length) {
            const data = JSON.parse(lines.join('\n')) as StreamEvent;
            registerRuntimeTranslations(data);
            callback(type, data);
          }
        }
      }
    } finally { reader.releaseLock(); }
  }
  function liveResponse(epoch = viewEpoch, id = state.conversationId): LiveResponse {
    const message = createMessage('assistant', '');
    message.working = true; message.workingLabel = '正在思考...';
    let accumulated = '', terminal = false, failure = false, detached = false;
    let replyStarted = false;
    let finalJobFailure = false, finalRequestFailure = false, knownFailure = false;
    const reviewRows = new Map<string, Activity>();
    const directoryRows = new Map<string, Activity>();
    const seenEvents = new Set<number | string>();
    function finishReplyPreparation() {
      if (message.progressKind !== 'message' && message.progressKind !== 'style') return;
      for (const entry of directoryRows.values()) if (entry.status === 'running') entry.status = 'completed';
      message.activityComplete = true;
      message.activityLabel = message.progressKind === 'style' ? '风格分析过程 · 展开查看' : '处理过程 · 展开查看';
      message.currentActivityId = undefined;
    }
    function showFailure(data: StreamEvent) {
      message.jobId = data.job_id;
      if (data.failure && typeof data.failure === 'object' && !Array.isArray(data.failure)) message.failure = data.failure;
    }
    const view: LiveResponse = {
      receive(type, data) {
        if (detached || !isCurrentView(epoch, id)) return;
        if (finalJobFailure && type !== 'job.failed') return;
        if (finalRequestFailure && !['message.failed', 'directory.review'].includes(type)) return;
        if (type === 'job.snapshot') {
          view.prefillJobState(data);
        } else if (type === 'message.started') {
          message.workingLabel = '正在思考...';
        } else if (type === 'message.delta') {
          accumulated += data.text || ''; message.text = accumulated; message.contentOrigin = 'model';
          if (!replyStarted && accumulated.trim()) {
            replyStarted = true;
            finishReplyPreparation();
          }
        } else if (type === 'message.completed') {
          message.text = data.text || ''; message.contentOrigin = data.content_origin || message.contentOrigin || 'system'; terminal = true;
          finishReplyPreparation();
        } else if (type === 'directory.update' || type === 'style.update' || type === 'message.update') {
          // Reply preparation must not reopen a completed analysis or displace
          // text that is already streaming.
          if ((replyStarted || message.styleAnalysis) && type !== 'directory.update') return;
          const style = type === 'style.update';
          const taskStarting = type !== 'message.update' && message.progressKind !== (style ? 'style' : 'directory');
          if (type !== 'message.update') message.progressKind = style ? 'style' : 'directory';
          else message.progressKind ??= 'message';
          if (taskStarting && !accumulated) message.text = style
            ? '正在按容量分批阅读示例全文，分析写法、结构和可提取的排版信息。'
            : '我会先查看资料清单和文件结构，阅读相关原文，再编排并核对目录。';
          message.directoryProgress = {
            elapsedSeconds: Number.isFinite(data.elapsed_seconds) ? Math.max(0, data.elapsed_seconds!) : message.directoryProgress?.elapsedSeconds,
            stepElapsedSeconds: Number.isFinite(data.step_elapsed_seconds) ? Math.max(0, data.step_elapsed_seconds!) : message.directoryProgress?.stepElapsedSeconds,
            modelStatus: data.model_status,
          };
          const progress = data.activity;
          // Heartbeats update the current row; a completed tool call never turns
          // into a fresh line on every transport notification.
          if (progress?.id && progress.text) {
            let entry = directoryRows.get(progress.id);
            if (!entry) {
              for (const prior of directoryRows.values()) if (prior.status === 'running') prior.status = 'completed';
              entry = reactive<Activity>({ ...progress });
              directoryRows.set(progress.id, entry); message.activities.push(entry);
            } else Object.assign(entry, progress);
            message.currentActivityId = entry.id;
            message.activityLabel = entry.text;
          } else if (data.text && data.text !== message.activities.at(-1)?.text) {
            // Keep compatibility with servers that send descriptive text only.
            const entry = reactive<Activity>({ id: uiId(), text: data.text, status: 'running' });
            for (const prior of directoryRows.values()) if (prior.status === 'running') prior.status = 'completed';
            directoryRows.set(entry.id, entry); message.activities.push(entry);
            message.currentActivityId = entry.id; message.activityLabel = entry.text;
          }
          message.activityComplete = false;
          message.workingLabel = message.progressKind === 'message' ? '正在思考...'
            : message.activities.at(-1)?.text || (
              message.progressKind === 'style' ? '正在分析风格示例…' : '正在研究资料并规划目录…'
            );
          if (message.progressKind === 'style') message.activityLabel = '风格分析过程';
          else if (message.progressKind === 'message') message.activityLabel = '处理过程';
          state.announcement = message.workingLabel;
        } else if (type === 'style.completed') {
          message.progressKind = 'style';
          if (data.revision && data.status && data.style_url && Array.isArray(data.document_names)) {
            message.styleAnalysis = {
              revision: data.revision, status: data.status, style_url: data.style_url,
              document_names: data.document_names, summary: data.summary, cache_hit: data.cache_hit,
            };
          }
          if (data.summary) message.text = data.summary;
          for (const entry of directoryRows.values()) if (entry.status === 'running') entry.status = 'completed';
          message.activityComplete = true;
          message.activityLabel = '风格分析过程 · 展开查看';
          message.currentActivityId = undefined;
        } else if (type === 'directory.review') {
          terminal = true;
          const taskUsage = Array.isArray(data.task_usage)
            ? data.task_usage
            : Array.isArray(data.directory?.task_usage) ? data.directory.task_usage : undefined;
          if (taskUsage) message.taskUsage = taskUsage;
          if (data.operation_error) {
            failure = true; finalRequestFailure = true;
            message.text = data.operation_error.text; message.diagnostics = data.operation_error.diagnostics;
          } else if (!failure) {
            message.text = data.reason || '目录已规划，请确认章节安排。';
          }
          if (message.directoryProgress) {
            for (const entry of directoryRows.values()) if (entry.status === 'running') entry.status = failure ? 'failed' : 'completed';
            message.activityComplete = !failure;
            message.activityLabel = failure ? '本次目录处理未完成 · 查看过程' : '目录处理结束 · 展开查看过程';
            message.currentActivityId = undefined;
          }
          if (data.phase && data.revision) void showDirectoryReview(data as DirectoryReview, message);
        } else if (type === 'job.created') {
          state.activeJob = data.job_id || null; state.activeJobStatus = 'running';
          state.retryJob = null; message.jobId = data.job_id;
          message.workingLabel = data.resumed ? '正在恢复已保存的生成进度…' : '正在准备文档任务…';
          state.previewFootnote = '正在生成文档正文，右侧保留已确认目录。';
          if (state.directoryReview) state.directoryReview.phase = 'generating';
          state.generating = true; setRunning(true);
          message.text = data.resumed
            ? '正在沿用已确认目录，从保存的进度继续生成正文。'
            : '文档任务已启动，任务在后台执行，刷新页面后可恢复查看。';
        } else if (type === 'job.pausing') {
          state.activeJobStatus = 'pausing'; message.workingLabel = '正在暂停并保存当前进度…';
        } else if (type === 'job.paused') {
          terminal = true; state.activeJobStatus = 'paused'; setRunning(false);
          message.text = data.text || '任务已暂停，已完成内容已保存。点击“继续生成”可接着完成。';
          message.activityLabel = '任务已暂停 · 已保存进度'; message.activityComplete = true;
        } else if (type === 'work.update') {
          if (data.event_id !== undefined) { if (seenEvents.has(data.event_id)) return; seenEvents.add(data.event_id); }
          if (!message.activities.length && !knownFailure && !terminal) message.text = '正在根据已上传资料和确认目录生成文档。';
          const review = data.review && typeof data.review === 'object' && !Array.isArray(data.review) ? data.review : undefined;
          const failedCall = data.failure && typeof data.failure === 'object' && !Array.isArray(data.failure) ? data.failure : undefined;
          if (Array.isArray(data.task_usage)) {
            const merged = new Map((message.taskUsage || []).map(item => [item.task_id, item]));
            for (const item of data.task_usage) merged.set(item.task_id, { ...merged.get(item.task_id), ...item });
            message.taskUsage = [...merged.values()];
          }
          const key = review && review.status !== 'summary' && review.round_id != null && review.unit_id != null ? JSON.stringify([review.round_id, review.unit_id]) : null;
          let entry = key ? reviewRows.get(key) : undefined;
          if (!entry) {
            entry = reactive<Activity>({ id: uiId(), text: data.text || '', review, failure: failedCall, jobId: data.job_id });
            message.activities.push(entry);
            if (key) reviewRows.set(key, entry);
          }
          if (review && !failedCall) { entry.review = { ...entry.review, ...review }; entry.text = data.text || ''; }
          if (!knownFailure && !terminal) {
            message.currentActivityId = entry.id;
            const sourceText = data.text || '';
            message.activityLabel = sourceText; message.workingLabel = sourceText; state.announcement = sourceText;
          }
        } else if (type === 'artifact.ready') {
          const jobId = data.job_id || state.activeJob || data.confirmation_url?.match(/^\/jobs\/([^/]+)\//)?.[1];
          if (jobId) applyConfirmationHistory(data.confirmation_history || state.confirmationHistory, jobId, true);
          terminal = true;
          state.activeJob = null; state.activeJobStatus = null; state.retryJob = null; state.generating = false; setRunning(false);
          state.previewFootnote = '完整文档已生成，可在对话中下载。';
          message.text = data.pending_count ? `文档草稿已生成，还有 ${data.pending_count} 项需要确认。` : '文档已生成，可以下载了。';
          if (data.proposal_count) message.notes.push(`已自动补全资料缺口，正文包含 ${data.proposal_count} 处标注为「模型建议」的内容。`);
          if (data.layout_note) message.notes.push(data.layout_note);
          message.activityComplete = true; message.activityLabel = '处理结束 · 展开查看过程';
          message.jobId = jobId;
          message.taskUsage = data.task_usage || message.taskUsage;
          if (data.filename && data.download_url) message.artifact = { elapsed_ms: data.elapsed_ms, filename: data.filename, download_url: data.download_url, pending_count: data.pending_count, proposal_count: data.proposal_count, status: data.status, issues: data.issues, confirmation_url: data.confirmation_url, archive_round: nextArchiveRound(), task_usage: data.task_usage };
          if (jobId && data.status === 'needs_confirmation') void loadConfirmation(jobId);
          else clearConfirmation();
        } else if (type === 'message.failed' || type === 'job.failed') {
          terminal = true; failure = true;
          if (type === 'job.failed') {
            finalJobFailure = true;
            state.retryJob = data.job_id || state.activeJob;
            state.activeJob = null; state.activeJobStatus = null; state.generating = false; setRunning(false);
            state.previewFootnote = '本次任务未完成，目录和已保存的生成进度仍保留。';
            showFailure({ ...data, job_id: state.retryJob || undefined });
          } else { finalRequestFailure = true; message.diagnostics = data.diagnostics; }
          if (!accumulated) { message.text = data.text || '本次请求未完成。'; message.contentOrigin = 'system'; }
          else if (data.text) message.notes.push(data.text);
          if (data.diagnostics?.stage === 'style_analysis') message.progressKind = 'style';
          message.activityComplete = Boolean(message.styleAnalysis) || !message.directoryProgress;
          message.activityLabel = message.styleAnalysis ? '风格分析过程 · 已保存结果' : message.progressKind === 'style' ? '风格分析未完成 · 查看过程' : message.progressKind === 'message' ? '处理过程 · 未完成' : '任务未完成';
          for (const entry of directoryRows.values()) if (entry.status === 'running') entry.status = 'failed';
        }
        if (terminal) message.working = false;
      },
      prefillFailure(data) {
        knownFailure = true; failure = true; message.working = false;
        message.text = data.text || '本次生成未完成，已保留完成的进度。';
        message.activityLabel = '任务未完成'; message.activityComplete = true;
        state.previewFootnote = '本次任务未完成，目录和已保存的生成进度仍保留。';
        showFailure(data);
      },
      prefillJobState(data) {
        if (detached || !isCurrentView(epoch, id) || !data.job_id) return;
        const status = data.status || '';
        const inactive = ['published', 'draft', 'needs_confirmation', 'failed', 'paused'].includes(status);
        if (!inactive && !['queued', 'running', 'pausing'].includes(status)) return;
        message.jobId = data.job_id;
        terminal = inactive;
        if (inactive && !message.activities.length) message.historyDeferred = true;
        message.working = !inactive;
        message.activityComplete = inactive;
        message.currentActivityId = undefined;
        state.activeJob = inactive && status !== 'paused' ? null : data.job_id;
        state.activeJobStatus = state.activeJob ? status : null;
        state.generating = !inactive;
        state.retryJob = status === 'failed' ? data.job_id : null;
        setRunning(false);
        if (status !== 'failed') { knownFailure = false; failure = false; message.failure = undefined; }
        if (status === 'failed') {
          view.prefillFailure(data);
        } else if (status === 'paused') {
          message.text = '任务已暂停，已完成的内容和检索进度已保存，可继续生成。';
          message.activityLabel = '任务已暂停 · 已保存进度';
        } else if (inactive) {
          message.text = status === 'needs_confirmation' ? '文档草稿已生成，可下载查看或按需补充确认。' : '文档已生成，可以下载了。';
          message.activityLabel = '处理结束 · 展开查看过程';
          state.previewFootnote = '完整文档已生成，可在对话中下载。';
        } else {
          // A different tab may have resumed the job since the conversation
          // snapshot. The stream's current state takes precedence.
          message.text = '正在根据已上传资料和确认目录生成文档。';
          message.workingLabel = status === 'pausing' ? '正在暂停并保存当前进度…' : '正在读取任务进度…';
          message.activityLabel = message.workingLabel;
        }
      },
      detach() { detached = true; message.working = false; },
      end() {
        message.working = false;
        if (!detached && !knownFailure && isCurrentView(epoch, id) && !terminal) view.error('连接已断开，回答或任务尚未完成。文档任务可刷新页面恢复查看。');
      },
      error(text) {
        message.working = false;
        if (detached || knownFailure || finalJobFailure || finalRequestFailure || !isCurrentView(epoch, id)) return;
        failure = true;
        if (!accumulated) message.text = text; else message.notes.push(text);
        message.activityComplete = Boolean(message.styleAnalysis) || !message.directoryProgress;
        message.activityLabel = message.styleAnalysis ? '风格分析过程 · 已保存结果' : message.progressKind === 'style' ? '风格分析连接已结束 · 查看过程' : message.progressKind === 'message' ? '处理过程 · 连接已结束' : '处理连接已结束';
        for (const entry of directoryRows.values()) if (entry.status === 'running') entry.status = 'failed';
      },
    };
    return view;
  }
  function receiveRequestEvent(request: PendingRequest, type: string, data: StreamEvent) {
    const previous = request.events.at(-1);
    // Keep the running request's state when its conversation is not visible.
    // Heartbeats and streamed text need only their latest/combined value.
    if (previous?.type === type && type === 'message.delta') {
      previous.data = { ...previous.data, text: (previous.data.text || '') + (data.text || '') };
    } else if (previous?.type === type && ['message.update', 'directory.update', 'style.update'].includes(type)
      && previous.data.activity?.id === data.activity?.id
      && (data.activity?.id || previous.data.text === data.text)) {
      previous.data = data;
    } else request.events.push({ type, data });
    request.view.receive(type, data);
  }
  async function send(action = 'chat', extra: Record<string, unknown> = {}, label: string | null = null) {
    if (state.running) return;
    await ready;
    if (state.restoreFailed) { toast('请先恢复当前会话，或新建会话后再发送。', 'warning'); return; }
    const entered = state.text.trim();
    const text = label || entered || (action === 'generate' ? tr('请根据已上传资料和我们的讨论，规划完整文档目录供我确认。') : '');
    if (!text) return;
    if (text.length > 600000) { toast('请将单条消息控制在 600,000 字符以内。', 'warning'); return; }
    if (sourcesBusy.value) { toast('资料正在更新，请完成后再发送。'); state.materialsOpen = true; return; }
    if (state.files.some(file => file.failed || !file.server_id)) { toast('请重试或移除上传失败的资料后再发送。', 'warning'); state.materialsOpen = true; return; }
    if (['generate', 'confirm_directory'].includes(action) && !state.files.some(file => file.server_id)) { toast('请先上传至少一份资料，再生成目录。', 'warning'); state.materialsOpen = true; return; }
    const epoch = viewEpoch;
    try { await flushDraft(); await ensureConversation(); }
    catch (error) { if (epoch === viewEpoch) toast(`请先恢复连接并保存草稿：${errorText(error)}`, 'error'); return; }
    if (epoch !== viewEpoch || state.running || state.restoring || !state.conversationId || pendingMessages.has(state.conversationId)) return;
    if (sourcesBusy.value || state.files.some(file => file.failed || !file.server_id)) { toast('资料状态已改变，请等待上传完成后再发送。', sourcesBusy.value ? 'neutral' : 'warning'); return; }
    if (['generate', 'confirm_directory'].includes(action) && !state.files.some(file => file.server_id)) { toast('请先上传至少一份资料，再生成目录。', 'warning'); state.materialsOpen = true; return; }
    const id = state.conversationId;
    state.generating = action === 'confirm_directory'; setRunning(true); state.materialsOpen = false;
    const requestController = new AbortController(); controller = requestController;
    createMessage('user', text, true);
    if (state.titleIsLabel) {
      const existing = state.historyItems.find(item => item.id === id);
      state.title = existing && !existing.title_is_default ? existing.title : (text.length > 24 ? `${text.slice(0, 24)}…` : text);
      state.titleIsLabel = false;
    }
    if (!label) state.text = '';
    activeResponse?.detach();
    const request: PendingRequest = { epoch, id, controller: requestController, view: liveResponse(), events: [] };
    activeResponse = request.view; pendingMessages.set(id, request);
    let accepted = false;
    try {
      const response = await fetch(`/conversations/${id}/messages`, {
        method: 'POST', signal: requestController.signal,
        ...json({ text, action, ...extra }),
      });
      accepted = response.ok;
      if (accepted) storage.remove(`folio.draft.${id}`);
      await consumeStream(response, (type, data) => receiveRequestEvent(request, type, data)); request.view.end();
    } catch (error) { request.view.error(error instanceof Error && error.name === 'AbortError' ? '已停止回答。' : errorText(error)); }
    finally {
      if (pendingMessages.get(id) === request) pendingMessages.delete(id);
      if (isCurrentView(request.epoch, id) && controller === requestController) {
        controller = null; state.generating = Boolean(state.activeJob); setRunning(false);
        if (!accepted && !label) { state.text = entered; scheduleDraft(); }
      }
      void refreshHistory();
    }
  }
  async function previewDirectory(preview: DirectoryPreviewSource) {
    const version = ++previewVersion;
    state.preview = { title: preview.name, titleIsLabel: true, downloadUrl: preview.download_url, kind: 'directory', status: '正在加载目录版式…', pages: [], blocks: [], showStructure: true };
    void api<{ blocks: PreviewBlock[] }>(`/previews/${preview.id}`).then(data => {
      if (version === previewVersion && !disposed) state.preview.blocks = data.blocks || [];
    }).catch(error => { if (version === previewVersion && !disposed) state.preview.status = errorText(error); });
    try {
      const data = await api<{ pages: string[] }>(`/previews/${preview.id}/pages`);
      if (version !== previewVersion || disposed) return;
      state.preview.pages = data.pages; state.preview.showStructure = !data.pages.length;
      state.preview.status = `目录预览 · ${data.pages.length} 页`;
    } catch (error) { if (version === previewVersion && !disposed) state.preview.status = errorText(error); }
  }
  function previewImageFailed() { state.preview.showStructure = true; state.preview.status = '页面图片加载失败，已显示目录结构。'; }
  async function showDirectoryReview(review: DirectoryReview, message: Message | null) {
    state.directoryReview = review;
    if (message) message.directoryReview = structuredClone(review);
    state.previewOpen = true;
    state.previewFootnote = review.directory.confirmable === false
      ? review.directory.confirmation_issues?.[0]?.message || '目录检查尚未完成，请查看研究记录中的具体原因。'
      : '请结合资料研究记录确认章节结构，确认后开始生成正文。';
    if (review.directory.preview) void previewDirectory(review.directory.preview);
    else {
      previewVersion++;
      state.preview = { title: review.title || '文档目录', titleIsLabel: !review.title, downloadUrl: '', kind: 'directory', status: `${review.directory.sections.length} 个目录节点`, pages: [], blocks: [], showStructure: true };
    }
  }
  const directoryLabels: Record<string, string> = { confirm_directory: '确认目录，生成文档', regenerate_directory: '根据最新要求重新生成目录', resume_directory_research: '从中断处继续资料研究' };
  function directoryAction(action: string, extra: Record<string, unknown> = {}, label?: string, revision = state.directoryReview?.revision) {
    if (!revision || revision !== state.directoryReview?.revision) { toast('请操作最新的文档目录。', 'warning'); return; }
    if (action === 'confirm_directory' && state.directoryReview?.directory.confirmable === false) {
      toast(state.directoryReview.directory.confirmation_issues?.[0]?.message || '目录检查尚未完成，请查看研究记录中的具体原因。', 'warning'); return;
    }
    return send(action, { selection_revision: revision, ...extra }, label || tr(directoryLabels[action] || '修改目录'));
  }
  function reviseDirectory(advice: string, revision = state.directoryReview?.revision) {
    advice = advice.trim();
    if (!advice) { toast('请填写目录修改建议。', 'warning'); return; }
    return directoryAction('revise_directory', { advice }, trf('修改建议：{advice}', { advice }), revision);
  }
  interface ConfirmationDraft {
    answers?: Record<string, string>;
    closedComments?: Record<string, boolean>;
    calls?: number;
    payloadKey?: string;
    requestId?: string;
    view?: { revision?: string; collapsed: boolean; skipped: boolean };
  }
  function nextArchiveRound() { return Math.max(0, ...state.confirmationHistory.map(item => item.round)) + 1; }
  function applyConfirmationHistory(history: ConfirmationHistory[], jobId: string, removeUnknown = false) {
    state.confirmationHistory = history;
    for (const message of state.messages) {
      const artifact = message.artifact;
      if (!artifact || artifact.archived || !(message.jobId === jobId || artifact.download_url === `/jobs/${jobId}/artifact`)) continue;
      const previous = history.find(item => item.round === artifact.archive_round);
      if (previous) { artifact.download_url = previous.download_url; artifact.archived = true; }
      else if (removeUnknown) {
        message.artifact = undefined;
        message.notes.push('文档已继续处理，最新版本会另行提供下载。');
      }
    }
  }
  function confirmationStorageKey(id: string, jobId: string) { return `folio.confirmation.${id}.${jobId}`; }
  function readConfirmationDraft(id: string, jobId: string): ConfirmationDraft {
    try { return JSON.parse(storage.get(confirmationStorageKey(id, jobId)) || '{}') as ConfirmationDraft; }
    catch { return {}; }
  }
  function saveConfirmationDraft() {
    const id = state.conversationId, jobId = state.confirmationJob;
    if (!id || !jobId) return;
    storage.set(confirmationStorageKey(id, jobId), JSON.stringify({
      ...readConfirmationDraft(id, jobId), answers: state.confirmationAnswers, closedComments: state.confirmationClosedComments, calls: state.confirmationCalls,
      view: { revision: state.confirmation?.revision, collapsed: state.confirmationCollapsed, skipped: state.confirmationSkipped },
    }));
  }
  function toggleConfirmation() {
    if (!state.confirmationJob || state.confirmationSubmitting || state.confirmationLoading) return;
    state.confirmationCollapsed = !state.confirmationCollapsed;
    if (!state.confirmationCollapsed) state.confirmationSkipped = false;
    saveConfirmationDraft();
  }
  function skipConfirmation() {
    if (!state.confirmation || state.confirmationSubmitting || state.confirmationLoading || state.running || state.restoring) return;
    state.confirmationSkipped = true;
    state.confirmationCollapsed = true;
    saveConfirmationDraft();
    toast('已跳过本次补充，当前文档可直接下载，之后仍可重新补充。');
  }
  function clearConfirmation() {
    confirmationVersion++;
    state.confirmationJob = null; state.confirmation = null; state.confirmationAnswers = {};
    state.confirmationClosedComments = {};
    state.confirmationCalls = 200; state.confirmationLoading = false;
    state.confirmationSubmitting = false; state.confirmationError = '';
    state.confirmationCollapsed = false; state.confirmationSkipped = false;
  }
  async function loadConfirmation(jobId = state.confirmationJob) {
    if (!jobId || !state.conversationId) return;
    const epoch = viewEpoch, id = state.conversationId, version = ++confirmationVersion;
    state.confirmationJob = jobId; state.confirmationLoading = true; state.confirmationError = '';
    try {
      const result = await api<ConfirmationData>(`/jobs/${jobId}/confirmation?conversation_id=${encodeURIComponent(id)}`);
      if (!isCurrentView(epoch, id) || version !== confirmationVersion) return;
      applyConfirmationHistory(result.history || [], jobId, result.status !== 'needs_confirmation');
      if (result.status !== 'needs_confirmation') {
        clearConfirmation();
        state.activeJob = ['published', 'draft', 'failed'].includes(result.status) ? null : jobId;
        state.activeJobStatus = state.activeJob ? result.status : null;
        state.generating = Boolean(state.activeJob); setRunning(false);
        observeJob(jobId, null, false, result.status);
        return;
      }
      const draft = readConfirmationDraft(id, jobId);
      state.confirmation = result;
      const sameRevision = draft.view?.revision === result.revision;
      state.confirmationSkipped = sameRevision && draft.view?.skipped === true;
      state.confirmationCollapsed = state.confirmationSkipped || sameRevision && draft.view?.collapsed === true;
      state.confirmationAnswers = Object.fromEntries(Object.entries(draft.answers || {}).filter((entry): entry is [string, string] => typeof entry[1] === 'string'));
      state.confirmationClosedComments = Object.fromEntries(Object.entries(draft.closedComments || {}).filter((entry): entry is [string, boolean] => typeof entry[1] === 'boolean'));
      state.confirmationCalls = Number.isInteger(draft.calls) ? draft.calls! : result.default_additional_calls || 200;
      state.confirmationSubmitting = pendingConfirmations.has(jobId);
      if (state.confirmationSubmitting) setRunning(true);
    } catch (error) {
      if (isCurrentView(epoch, id) && version === confirmationVersion) state.confirmationError = `待确认问题未能加载：${errorText(error)}`;
    } finally {
      if (isCurrentView(epoch, id) && version === confirmationVersion) state.confirmationLoading = false;
    }
  }
  async function submitConfirmation() {
    const review = state.confirmation, id = state.conversationId;
    if (!review || !id || state.running || state.restoring || pendingConfirmations.has(review.job_id)) return;
    const additionalCalls = Number(state.confirmationCalls);
    if (!Number.isInteger(additionalCalls) || additionalCalls < 1 || additionalCalls > 10000) {
      state.confirmationError = '请填写 1 到 10000 之间的整数作为本轮调用上限。'; return;
    }
    const answers = review.issues.map(issue => ({
      issue_id: issue.issue_id, answer: (state.confirmationAnswers[issue.issue_id] || '').trim(),
      ...(issue.key.startsWith('source-comment:') && state.confirmationClosedComments[issue.issue_id] ? { close_comment: true } : {}),
    })).filter(answer => answer.answer);
    const payload = { conversation_id: id, revision: review.revision, answers, additional_calls: additionalCalls };
    const payloadKey = JSON.stringify(payload), draft = readConfirmationDraft(id, review.job_id);
    const requestId = draft.payloadKey === payloadKey && draft.requestId ? draft.requestId : uiId().replaceAll('-', '');
    const draftKey = confirmationStorageKey(id, review.job_id);
    storage.set(draftKey, JSON.stringify({ answers: state.confirmationAnswers, closedComments: state.confirmationClosedComments, calls: additionalCalls, payloadKey, requestId }));
    pendingConfirmations.add(review.job_id); state.confirmationSubmitting = true; state.confirmationError = ''; setRunning(true);
    // Navigation must not cancel an accepted answer; its idempotency key remains
    // durable if the response is lost or the page is reloaded.
    try {
      const result = await api<{ job_id: string; status: string; text?: string }>(`/jobs/${review.job_id}/confirmation`, { method: 'POST', ...json({ ...payload, request_id: requestId }) });
      if (readConfirmationDraft(id, review.job_id).payloadKey === payloadKey) storage.remove(draftKey);
      if (disposed || state.conversationId !== id || state.confirmationJob !== review.job_id) return;
      for (const message of state.messages) {
        if (message.artifact && !message.artifact.archived && (message.jobId === review.job_id || message.artifact.download_url === `/jobs/${review.job_id}/artifact`)) {
          message.artifact.download_url = `/jobs/${review.job_id}/confirmations/${requestId}/artifact`;
          message.artifact.archived = true;
        }
      }
      const archive = { round: nextArchiveRound(), request_id: requestId, download_url: `/jobs/${review.job_id}/confirmations/${requestId}/artifact`, filename: tr('补答前草稿.docx') };
      if (!state.confirmationHistory.some(item => item.request_id === requestId)) state.confirmationHistory.push(archive);
      createMessage('user', `${answers.length ? trf('已补充 {count} 项说明，继续检查文档。', { count: answers.length }) : tr('重新检查文档。')} ${trf('本轮最多 {count} 次模型调用。', { count: additionalCalls })}`);
      clearConfirmation();
      state.activeJob = ['published', 'draft', 'needs_confirmation', 'failed'].includes(result.status) ? null : result.job_id;
      state.activeJobStatus = state.activeJob ? result.status : null;
      state.generating = Boolean(state.activeJob); setRunning(false);
      controller?.abort(); observeJob(result.job_id, null, false, result.status); void refreshHistory();
    } catch (error) {
      if (!disposed && state.conversationId === id && state.confirmationJob === review.job_id) {
        state.confirmationError = `未能继续处理，填写内容已保留：${errorText(error)}`;
        if (error instanceof ApiError && error.status === 409) await loadConfirmation(review.job_id);
      }
    } finally {
      pendingConfirmations.delete(review.job_id);
      if (!disposed && state.conversationId === id) { state.confirmationSubmitting = false; setRunning(false); }
    }
  }
  async function loadJobHistory(message: Message) {
    if (!message.historyDeferred || message.historyLoading || !message.jobId) return;
    message.historyLoading = true;
    const activities: Activity[] = [];
    const rows = new Map<string, Activity>();
    try {
      await consumeStream(await fetch(`/jobs/${message.jobId}/stream`, {
        headers: acceptLanguageHeaders(),
      }), (type, data) => {
        if (type !== 'work.update') return;
        const review = data.review;
        const key = review && review.status !== 'summary' && review.round_id != null && review.unit_id != null
          ? JSON.stringify([review.round_id, review.unit_id]) : null;
        const existing = key ? rows.get(key) : undefined;
        if (existing) { existing.text = data.text || ''; existing.review = { ...existing.review, ...review }; }
        else {
          const activity = { id: uiId(), text: data.text || '', review, failure: data.failure };
          activities.push(activity);
          if (key) rows.set(key, activity);
        }
      });
      message.activities = activities;
      message.historyDeferred = false;
    } catch (error) { toast(errorText(error), 'error'); }
    finally { message.historyLoading = false; }
  }
  function observeJob(jobId: string, failure: FailureDetails | null = null, resumed = false, knownStatus?: string) {
    const epoch = viewEpoch; const id = state.conversationId;
    const observer = new AbortController(); controller = observer;
    activeResponse?.detach();
    const view = liveResponse(epoch, id); activeResponse = view;
    if (knownStatus) view.prefillJobState({ job_id: jobId, status: knownStatus });
    if (failure) view.prefillFailure({ job_id: jobId, failure });
    else if (resumed) view.receive('job.created', { job_id: jobId, resumed: true });
    void (async () => {
      try {
        await consumeStream(await fetch(`/jobs/${jobId}/stream?replay_history=false`, {
          signal: observer.signal,
          headers: acceptLanguageHeaders(),
        }), view.receive);
        view.end();
      } catch (error) { if (!(error instanceof Error && error.name === 'AbortError')) view.error(errorText(error)); }
      finally { if (isCurrentView(epoch, id) && controller === observer) { controller = null; state.generating = Boolean(state.activeJob); setRunning(false); } }
    })();
  }
  async function retryFailedJob(jobId = state.retryJob) {
    if (!jobId || jobId !== state.retryJob || !state.conversationId || state.running || state.taskControlPending || state.restoring || state.restoreFailed) return;
    const epoch = viewEpoch, id = state.conversationId;
    state.taskControlPending = true; setRunning(true);
    try {
      await api<{ status: string }>(`/jobs/${jobId}/resume`, { method: 'POST', ...json({ conversation_id: id }) });
      if (!isCurrentView(epoch, id) || state.retryJob !== jobId) return;
      controller?.abort(); observeJob(jobId, null, true);
      void refreshHistory();
    } catch (error) {
      if (isCurrentView(epoch, id)) toast(`未能继续生成，已保存的进度仍保留：${errorText(error)}`, 'error');
    } finally { if (isCurrentView(epoch, id)) { state.taskControlPending = false; setRunning(false); } }
  }
  async function controlTask() {
    if (!state.activeJob || state.taskControlPending || state.activeJobStatus === 'pausing') return;
    const epoch = viewEpoch; const id = state.conversationId; const jobId = state.activeJob;
    const resume = state.activeJobStatus === 'paused';
    state.taskControlPending = true;
    try {
      const result = await api<{ status: string }>(`/jobs/${jobId}/${resume ? 'resume' : 'pause'}`, { method: 'POST', ...json({ conversation_id: id }) });
      if (!isCurrentView(epoch, id) || state.activeJob !== jobId) return;
      if (resume || state.activeJobStatus !== 'paused') state.activeJobStatus = result.status;
      if (resume) { controller?.abort(); observeJob(jobId); }
      else if (result.status === 'paused') state.activeJobStatus = 'paused';
      setRunning(false);
    } catch (error) { if (isCurrentView(epoch, id)) toast(`${resume ? '继续' : '暂停'}失败：${errorText(error)}`, 'error'); }
    finally { if (isCurrentView(epoch, id)) state.taskControlPending = false; }
  }
  function stop() { if (state.activeJob) return controlTask(); controller?.abort(); }
  async function restoreState(persisted: ConversationData) {
    state.confirmationHistory = persisted.confirmation_history || [];
    state.files = (persisted.files || []).map(item => ({ id: item.id, server_id: item.id, role: item.role || 'source', file: { name: item.name, size: item.size, lastModified: 0 } }));
    state.text = persisted.draft || '';
    const localDraft = storage.get(`folio.draft.${state.conversationId}`);
    if (localDraft) {
      try {
        const backup = JSON.parse(localDraft) as { text?: string };
        if (typeof backup.text === 'string') state.text = backup.text;
        draftDirty = true;
      } catch { /* Ignore malformed local backup. */ }
    }
    let lastAssistant: Message | null = null;
    for (const item of persisted.messages || []) {
      const styleActivity = Boolean(item.style_analysis || ['风格分析过程', 'Style analysis'].includes(item.activity_label || '') || item.error_diagnostics?.stage === 'style_analysis');
      const text = item.interrupted && styleActivity && !item.content
        ? '风格分析已中断，已保存完成的步骤。'
        : item.content;
      const message = createMessage(item.role, text);
      if (item.interrupted && !styleActivity) message.notes.push('此回答未完整结束');
      if (item.role === 'assistant') {
        message.contentOrigin = item.content_origin || (item.error_diagnostics || text !== item.content ? 'system' : 'model');
        lastAssistant = message; message.diagnostics = item.error_diagnostics;
        message.styleAnalysis = item.style_analysis;
        message.progressKind = styleActivity ? 'style' : ['处理过程', 'Process'].includes(item.activity_label || '') ? 'message' : 'directory';
        if (item.activities?.length) {
          message.activities = item.activities.map(activity => ({ ...activity, status: activity.status === 'running' ? undefined : activity.status }));
          message.directoryProgress = {};
          message.activityComplete = (Boolean(item.style_analysis) || !item.interrupted && !item.error_diagnostics) && !item.activities.some(activity => activity.status === 'failed');
          message.activityLabel = message.progressKind === 'style'
            ? message.activityComplete ? '风格分析过程 · 展开查看' : '风格分析未完成 · 查看过程'
            : message.progressKind === 'message' ? message.activityComplete ? '处理过程 · 展开查看' : '处理过程 · 未完成'
            : message.activityComplete ? '目录处理结束 · 展开查看过程' : '本次目录处理未完成 · 查看过程';
        }
      }
    }
    state.title = persisted.title || persisted.messages?.find(message => message.role === 'user')?.content.slice(0, 30) || '新的文档';
    state.titleIsLabel = persisted.title_is_default ?? (!persisted.title && !persisted.messages?.some(message => message.role === 'user'));
    state.saveStatus = localDraft ? '草稿待同步' : '已保存';
    if (persisted.directory_review) {
      const selection = persisted.directory_review;
      const previous = persisted.messages?.at(-1);
      const sameFailure = selection.operation_error && previous?.role === 'assistant' && previous.content === selection.operation_error.text;
      const message = selection.phase === 'generating' ? null : sameFailure ? lastAssistant : createMessage('assistant', selection.operation_error?.text || selection.reason || '请确认文档目录。');
      if (message) message.diagnostics = selection.operation_error?.diagnostics;
      await showDirectoryReview(selection, message);
    }
    if (persisted.active_job) {
      const failed = persisted.active_job_status === 'failed';
      state.retryJob = failed ? persisted.active_job! : null;
      const terminal = ['published', 'draft', 'needs_confirmation', 'failed'].includes(persisted.active_job_status || '');
      state.activeJob = terminal ? null : persisted.active_job;
      state.activeJobStatus = terminal ? null : persisted.active_job_status || 'running';
      state.generating = !terminal; setRunning(false);
      if (state.conversationId) pendingMessages.get(state.conversationId)?.controller.abort();
      observeJob(persisted.active_job, failed ? persisted.active_job_failure || {} : null, false, persisted.active_job_status);
    } else if (state.conversationId && pendingMessages.has(state.conversationId)) {
      const request = pendingMessages.get(state.conversationId)!;
      request.epoch = viewEpoch; request.view = liveResponse(); controller = request.controller; activeResponse = request.view; setRunning(true);
      for (const event of request.events) request.view.receive(event.type, event.data);
    }
    if (persisted.pending_confirmation_job) void loadConfirmation(persisted.pending_confirmation_job);
  }
  async function restore() {
    await refreshHistory();
    if (disposed) return;
    const remembered = storage.get('folio.conversation');
    state.conversationId = remembered === 'new' ? null : remembered || state.historyItems[0]?.id || null;
    if (!state.conversationId) return;
    try {
      const persisted = await api<ConversationData>(`/conversations/${state.conversationId}`);
      if (disposed) return;
      storage.set('folio.conversation', state.conversationId);
      await restoreState(persisted);
    } catch (error) {
      state.restoreFailed = true;
      toast(`暂时无法恢复会话：${errorText(error)}。历史记录仍保留，请刷新或从左侧重试。`, 'error');
      if (error instanceof ApiError && error.status === 404) { state.conversationId = null; storage.remove('folio.conversation'); }
    }
  }
  function persistBeforeLeave() {
    if (!draftDirty || !state.conversationId) return;
    const body = JSON.stringify(draftSnapshot());
    storage.set(`folio.draft.${state.conversationId}`, body);
    void fetch(`/conversations/${state.conversationId}/draft`, {
      method: 'PUT',
      headers: acceptLanguageHeaders({ 'Content-Type': 'application/json' }),
      body,
      keepalive: true,
    }).catch(() => {});
  }
  onMounted(() => {
    window.addEventListener('pagehide', persistBeforeLeave);
    void restore().finally(() => { if (!disposed) { state.restoring = false; setRunning(Boolean(state.conversationId && pendingMessages.has(state.conversationId))); } resolveReady(); });
  });
  onBeforeUnmount(() => {
    persistBeforeLeave(); disposed = true; viewEpoch++; previewVersion++; cancelUploads();
    clearTimeout(draftTimer); clearTimeout(toastTimer);
    activeResponse?.detach(); controller?.abort();
    for (const request of pendingMessages.values()) { request.view.detach(); request.controller.abort(); }
    pendingMessages.clear();
    window.removeEventListener('pagehide', persistBeforeLeave);
    resolveReady();
  });
  return {
    loadJobHistory,
    state, reviewing, sourcesBusy, controlLabel,
    toast, addFiles, removeFile, retryFile, changeFileRole, send, switchConversation, deleteConversation, renameConversation, toggleConversationPin, refreshHistory,
    scheduleDraft, flushDraft, controlTask, retryFailedJob, stop, directoryAction, reviseDirectory,
    previewImageFailed,
    loadConfirmation, saveConfirmationDraft, submitConfirmation, toggleConfirmation, skipConfirmation,
  };
}
export type WorkspaceController = ReturnType<typeof useWorkspace>;
