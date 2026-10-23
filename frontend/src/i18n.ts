import { reactive, ref } from 'vue'

export type Language = 'zh-CN' | 'en'

const STORAGE_KEY = 'folio.language'
const initial = (() => {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    return saved === 'en' ? 'en' : 'zh-CN'
  } catch {
    return 'zh-CN'
  }
})()

export const language = ref<Language>(initial)


const catalog: Record<string, string> = {
  '修改建议：{advice}': 'Revision request: {advice}',
  '文档预览': 'Document preview',
  'Folio · 智能文档工作台': 'Folio · Intelligent document workspace',
  'Folio：从资料到成文的智能文档工作台。': 'Folio: an intelligent workspace that turns sources into documents.',
  '联网补充 · {count} 次查询': 'Web research · {count} searches',
  '{count} 份资料上传失败，可在列表中重试或移除。': '{count} source(s) failed to upload. Retry or remove them from the list.',
  '、': ', ',
  '免密访问': 'Access without authentication',
  '最大输出长度': 'Maximum output length',
  '目录规划输出长度': 'Outline output length',
  '温度': 'Temperature',
  '模型上下文窗口': 'Model context window',
  '本地 token 估算编码': 'Local token estimation encoding',
  '思考模式': 'Reasoning mode',
  '结构化输出': 'Structured output',
  '图片输入': 'Image input',
  '配置项': 'Setting',
  '模型配置校验失败，请检查填写内容。': 'Model settings failed validation. Check the values you entered.',
  '无法操作模型配置，请检查后台服务。': 'Unable to access model settings. Check the server.',
  '操作失败，请检查后台服务。': 'The operation failed. Check the server.',
  '正在读取配置…': 'Loading settings…',
  '请填写此服务的 API Key。': 'Enter an API key for this service.',
  '正在测试，请稍候…': 'Testing; please wait…',
  '测试未通过': 'Test failed',
  '请先测试当前服务配置，再勾选启用。': 'Test the current service settings before enabling it.',
  '正在保存…': 'Saving…',
  '配置已保存，新任务将使用此配置。': 'Settings saved. New tasks will use this configuration.',
  '配置已修改，请重新测试。': 'Settings changed. Test again.',
  '对话及其独有文件已删除，共用文件已保留': 'The conversation and its exclusive files were deleted; shared files were retained',
  '请使用 Word、Excel、PDF、PPTX、文本或 PNG/JPEG/TIFF/WebP 图片。': 'Use Word, Excel, PDF, PPTX, text, or PNG/JPEG/TIFF/WebP images.',
  '文件不能为空。': 'Files must not be empty.',
  '已选择模板，请先移除原模板，或明确将其改为普通资料后再选择。': 'A template is already selected. Remove it or change its purpose to a business source first.',
  '本轮最多 {count} 次模型调用。': 'This round allows at most {count} model calls.',
  '已补充 {count} 项说明，继续检查文档。': 'Added {count} answer(s) and continuing document checks.',
  '文档助手': 'Document assistant',
  '待正文补充': 'Body pending',
  '待补充': 'Pending',
  '对话记录': 'Conversation',
  '文档工作台': 'Document workspace',
  '收集资料': 'Collect sources',
  '描述需求': 'Describe requirements',
  '确认目录': 'Confirm outline',
  '生成文档': 'Generate document',
  '资料库': 'Sources',
  '模型设置': 'Model settings',
  '模型与服务': 'Models and services',
  '本地工作空间': 'Local workspace',
  '新的文档': 'New document',
  '新文档': 'New document',
  '保存中': 'Saving',
  '已保存': 'Saved',
  '保存失败 · 草稿暂存本机': 'Save failed · draft kept locally',
  '草稿待同步': 'Draft pending sync',
  '简体中文': 'Simplified Chinese',
  'English': 'English',
  '语言': 'Language',
  '资料研究记录': 'Source research',
  '处理记录': 'Process log',
  '处理过程': 'Process',
  '风格分析过程': 'Style analysis',
  '正在执行当前步骤': 'Running the current step',
  '任务统计': 'Task usage',
  '耗时': 'Time',
  '输入 Token': 'Input tokens',
  '输出 Token': 'Output tokens',
  '总 Token': 'Total tokens',
  '模型调用': 'Model calls',
  '缓存命中': 'Cache hits',
  '失败调用': 'Failed calls',
  '任务': 'Task',
  '任务已暂停 · 已保存进度': 'Task paused · progress saved',
  '任务未完成': 'Task incomplete',
  '处理结束 · 展开查看过程': 'Processing complete · expand process',
  '下载文档': 'Download document',
  '文档已生成，可以下载了。': 'The document is ready to download.',
  '文档草稿已生成，可下载查看或按需补充确认。': 'A draft is ready to download or confirm.',
  '完整文档已生成，可在对话中下载。': 'The complete document is ready in this conversation.',
  '文档目录': 'Document outline',
  '目录结构': 'Outline structure',
  '目录': 'Contents',
  '完整目录预览': 'Complete outline preview',
  '页面预览': 'Page preview',
  '下载目录': 'Download outline',
  '关闭文档预览': 'Close preview',
  '资料上传状态': 'Upload status',
  '上传中': 'Uploading',
  '排队中': 'Queued',
  '上传失败': 'Upload failed',
  '搜索文件名': 'Search filenames',
  '全部状态': 'All statuses',
  '上传中 / 排队': 'Uploading / queued',
  '业务资料': 'Business source',
  '文档模板': 'Document template',
  '风格示例': 'Style reference',
  '批量添加资料': 'Add sources',
  '调整目录': 'Adjust outline',
  '重新生成目录': 'Regenerate outline',
  '查看风格说明': 'View style guidance',
  '按建议修改目录': 'Apply outline changes',
  '补充确认': 'Additional confirmation',
  '技术定位': 'Technical details',
  '错误详情': 'Error details',
  '失败诊断': 'Failure details',
  '本次生成未完成': 'Generation incomplete',
  '保留进度': 'Saved progress',
  '已保存 {saved}/{total} 节正文。': '{saved}/{total} sections saved.',
  '正在思考...': 'Thinking…',
  '正在恢复…': 'Resuming…',
  '重试并继续': 'Retry and continue',
  '沿用已确认目录，从保存的进度继续。': 'Continue from the saved progress using the confirmed outline.',
  '查看 {count} 项待确认问题': 'View {count} pending issues',
  '之前的草稿 · {count} 份': '{count} previous drafts',
  '下载之前的草稿': 'Download previous draft',
  '之前的草稿 · 补答前版本': 'Previous draft · before follow-up',
  'Word 文档 · 检查通过': 'Word document · checks passed',
  'Word 草稿': 'Word draft',
  'Word 草稿 · 含模型建议': 'Word draft · includes model proposals',
  'Word 草稿 · 请核对待确认项': 'Word draft · review pending items',
  '本次资料 · {count} 份': '{count} sources in this message',
  '新建文档': 'New document',
  '暂无会话': 'No conversations',
  '你的下一份文档，从这里开始。': 'Your next document starts here.',
  '无法读取会话列表': 'Unable to load conversations',
  '重试': 'Retry',
  '加载更多会话': 'Load more conversations',
  '关闭': 'Close',
  '打开': 'Open',
  '份资料': 'sources',
  '汇集资料，通过对话构建你的文档。': 'Collect sources and build your document through conversation.',
  '从这里开始': 'Start here',
  '标书': 'Bid proposal',
  '需求说明书': 'Requirements specification',
  '研究报告': 'Research report',
  '技术方案': 'Technical proposal',
  '项目报告': 'Project report',
  '正在上传': 'Uploading',
  'Word、Excel、PDF、PPTX、文本及图片': 'Word, Excel, PDF, PPTX, text and images',
  '上传资料': 'Upload sources',
  '份': 'sources',
  '移除': 'Remove',
  '查看全部': 'View all',
  '告诉我文档要求': 'Describe the document requirements',
  '添加资料': 'Add sources',
  '发送消息': 'Send message',
  '可一次拖入多个文件': 'Drop multiple files at once',
  '发送': 'Send',
  '换行': 'New line',
  '文档流程': 'Document workflow',
  '重命名会话': 'Rename conversation',
  '会话名称': 'Conversation name',
  '取消': 'Cancel',
  '保存中…': 'Saving…',
  '保存': 'Save',
  '删除对话和文件？': 'Delete conversation and files?',
  '仅删除对话？': 'Delete conversation only?',
  '将删除这段对话，以及仅属于它的上传文件和生成文档，无法恢复。其他对话或任务使用的文件及共用缓存会保留。': 'This deletes the conversation and its exclusive uploads and generated documents. This cannot be undone; files and caches shared with other conversations or tasks are kept.',
  '将删除这段对话及聊天记录，无法恢复。已上传的文件和已生成的文档会保留。': 'This deletes the conversation and chat history. This cannot be undone; uploaded files and generated documents are kept.',
  '删除中…': 'Deleting…',
  '删除会话': 'Delete conversation',
  '松开，让资料就位': 'Drop to add sources',
  '批量上传资料': 'Upload multiple sources',
  '配置模型以开始': 'Configure a model to begin',
  '份资料上传失败，可在列表中重试或移除。': 'sources failed to upload. Retry or remove them from the list.',
  '打开文档预览': 'Open document preview',
  '文档目录预览': 'Document outline preview',
  '完整目录': 'Complete outline',
  '下载目录 Word': 'Download outline Word file',
  '描述文档需求后，在这里查看完整目录。': 'Describe the document requirements to see the complete outline here.',
  '生成目录后，在这里逐页查看 Word 排版。': 'After an outline is generated, inspect the Word layout page by page here.',
  '尚未生成正文。确认目录后，将研究上传资料并生成本节内容。': 'Body not generated yet. After you confirm the outline, Folio will research the uploaded sources and generate this section.',
  '目录结构内容': 'Outline structure content',
  '文档页面内容': 'Document page content',
  '目录版式预览': 'Outline layout preview',
  '页': 'pages',
  '{count} 页': '{count} pages',
  '{count} 个节点': '{count} nodes',
  '已用时': 'Elapsed',
  '用时': 'Time',
  '本步已用时': 'Current step',
  '风格说明': 'Style guidance',
  '分析完成': 'Analysis complete',
  '已提取结构与格式，写作风格待确认': 'Structure and formatting extracted; writing style still needs confirmation',
  '部分特征待确认': 'Some characteristics need confirmation',
  '已复用分析结果': 'Reused analysis results',
  '已分析的风格示例': 'Analysed style references',
  '查看': 'View',
  '风格分析过程 · 展开查看': 'Style analysis · expand',
  '处理过程 · 展开查看': 'Process · expand',
  '目录处理结束 · 展开查看过程': 'Outline processing complete · expand',
  '本次目录处理未完成 · 查看过程': 'Outline processing incomplete · view process',
  '被中止': 'Cancelled',
  '资料研究与目录规划': 'Source research and outline planning',
  '目录初稿': 'Outline draft',
  '这是较早的目录版本，请在最新目录中操作。': 'This is an older outline version. Use the latest outline.',
  '修改未完成，下面仍为上一版目录。可调整建议后重试。': 'The revision failed; the previous outline is still shown. Adjust the request and retry.',
  '目录检查尚未完成，请查看研究记录中的具体原因。': 'Outline checks are incomplete. Review the source research record for details.',
  '待确认的完整目录': 'Complete outline pending confirmation',
  '确认目录，生成文档': 'Confirm outline and generate document',
  '从中断处继续资料研究': 'Resume source research',
  '从中断处继续研究': 'Resume research',
  '根据当前资料和需求重新生成完整目录': 'Regenerate the complete outline from current sources and requirements',
  '例如：把实施计划单独列为一章，补充风险分析，并调整章节顺序。': 'For example: make implementation planning a separate chapter, add risk analysis, and reorder sections.',
  '已完成目录研究': 'Outline research complete',
  '研究尚未充分': 'Research incomplete',
  '未使用业务资料': 'No business sources used',
  '未命名资料': 'Unnamed source',
  '字符': 'characters',
  '已复用缓存': 'Reused cache',
  '搜索完成': 'Search complete',
  '{count} 个公开来源': '{count} public sources',
  '搜索未成功': 'Search failed',
  '联网搜索暂不可用': 'Web search unavailable',
  '搜索已中断': 'Search interrupted',
  '{files} 份资料，已读取其中 {read} 份的原文。': '{files} sources; original text read from {read}.',
  '资料阅读记录': 'Source reading record',
  '已查看结构': 'Structure inspected',
  '未查看结构': 'Structure not inspected',
  '已读取原文': 'Original text read',
  '未读取原文': 'Original text not read',
  '已读取内容': 'Content read',
  '联网补充记录': 'Web research record',
  '{count} 次查询': '{count} searches',
  '公开资料查询': 'Public source search',
  '目录用途': 'Outline purpose',
  '目录用途：': 'Outline purpose: ',
  '公开来源': 'Public source',
  '获取于': 'Retrieved',
  '联网研究结论': 'Web research findings',
  '联网资料用于补充公开背景，不能替代上传资料或证明用户项目事实。': 'Web sources provide public background only; they do not replace uploaded sources or prove project facts.',
  '尚待解决': 'Still unresolved',
  '当前目录仍是初稿，请补充资料或调整要求后重新生成目录。': 'The outline remains a draft. Add sources or adjust the requirements, then regenerate it.',
  '阅读记录展示工具实际返回的原文范围，不代表全文读完或理解率。': 'The reading record shows the actual source ranges returned by tools; it does not mean the full source was read or understood.',
  '下载完整阅读记录': 'Download full research record',
  '关闭资料库': 'Close sources',
  '业务资料提供事实；文档模板规定章节和格式（限一份 DOCX）；风格示例只借鉴写法和排版，不作为业务事实。多份风格示例以列表中的第一份为主参考。': 'Business sources provide facts; a document template defines sections and formatting (one DOCX); style references inform writing and layout but are not business facts. The first style reference is the main reference.',
  '等待上传': 'Waiting to upload',
  '搜索资料文件名': 'Search source filenames',
  '筛选上传状态': 'Filter upload status',
  '还没有资料': 'No sources yet',
  '添加项目文档、会议记录或其他参考资料。': 'Add project documents, meeting notes or other reference material.',
  '没有匹配的资料': 'No matching sources',
  '试试其他文件名或上传状态。': 'Try another filename or upload status.',
  '清除筛选': 'Clear filters',
  '移除中…': 'Removing…',
  '用途': 'Purpose',
  '文件用途': 'file purpose',
  '重试上传': 'Retry upload',
  '上一页资料': 'Previous source page',
  '下一页资料': 'Next source page',
  '已达到 200 份资料上限': '200-source limit reached',
  '文本': 'text',
  '图片': 'images',
  '正在恢复已保存的生成进度…': 'Resuming saved generation progress…',
  '正在准备文档任务…': 'Preparing the document task…',
  '文档任务已启动，任务在后台执行，刷新页面后可恢复查看。': 'The document task is running in the background. Refresh to restore its progress.',
  '正在沿用已确认目录，从保存的进度继续生成正文。': 'Continuing from saved progress with the confirmed outline.',
  '正在生成文档正文，右侧保留已确认目录。': 'Generating the document body; the confirmed outline remains on the right.',
  '任务已暂停，已完成内容已保存。点击“继续生成”可接着完成。': 'The task is paused and saved progress is kept. Select Resume to continue.',
  '任务已暂停，已完成的内容和检索进度已保存，可继续生成。': 'The task is paused. Generated content and research progress are saved.',
  '正在暂停并保存当前进度…': 'Pausing and saving current progress…',
  '正在读取任务进度…': 'Loading task progress…',
  '正在根据已上传资料和确认目录生成文档。': 'Generating the document from uploaded sources and the confirmed outline.',
  '本次任务未完成，目录和已保存的生成进度仍保留。': 'The task is incomplete; the outline and saved progress are retained.',
  '连接已断开，回答或任务尚未完成。文档任务可刷新页面恢复查看。': 'The connection closed before completion. Refresh to restore document task progress.',
  '已停止回答。': 'Response stopped.',
  '正在研究资料并规划目录…': 'Researching sources and planning the outline…',
  '正在分析风格示例…': 'Analysing style examples…',
  '正在判断你的请求是否需要分析风格': 'Checking whether the request requires style analysis',
  '正在分析代表性片段': 'Analysing representative excerpts',
  '正在根据风格说明准备回答': 'Preparing an answer from the style guidance',
  '正在载入已保存的风格说明': 'Loading saved style guidance',
  '我会先查看资料清单和文件结构，阅读相关原文，再编排并核对目录。': 'I will inspect the source list and structure, read relevant originals, then plan and verify the outline.',
  '正在按容量分批阅读示例全文，分析写法、结构和可提取的排版信息。': 'Reading the example in capacity-bounded batches to analyse writing, structure and measurable layout.',
  '目录已规划，请确认章节安排。': 'The outline is ready. Confirm the section structure.',
  '本次生成未完成，已保留完成的进度。': 'Generation is incomplete; completed progress is retained.',
  '本次请求未完成。': 'The request is incomplete.',
  '文档草稿已生成，还有 {count} 项需要确认。': 'A draft is ready with {count} item(s) requiring confirmation.',
  '已自动补全资料缺口，正文包含 {count} 处标注为「模型建议」的内容。': 'Source gaps were completed automatically; the body contains {count} item(s) marked as model proposals.',
  '已根据资料研究生成目录，请查看阅读记录并确认或提出修改建议。确认后才会生成文档正文。': 'The outline was generated from source research. Review the reading record, then confirm or revise it before body generation.',
  '目录初稿已生成，检查尚未完成；请查看研究记录中的具体原因。': 'An outline draft was generated, but checks are incomplete. Review the source research record.',
  '模型未正常完成回答，请检查连接或稍后重试。': 'The model did not complete its response. Check the connection or try again later.',
  '模型返回的内容未通过必要的数据结构或内容校验。': 'The model output did not pass the required structure or content validation.',
  '正文生成与章节核验': 'Body generation and section verification',
  '提取事实要点': 'Extract facts',
  '撰写正文': 'Write body',
  '修正正文': 'Repair body',
  '补写资料缺口': 'Fill source gaps',
  '整合补充内容': 'Integrate supplemental content',
  '核验章节事实': 'Verify section facts',
  '审阅业务任务': 'Review business task',
  '当前文档可直接下载，这一步可以跳过。可以只补充部分问题，其余留空；补充后会继续完善相关内容。': 'The current document is downloadable and this step is optional. Answer any subset of questions; completed answers will be used to refine the document.',
  '正在读取待确认问题…': 'Loading confirmation questions…',
  '填写明确的业务规则或说明；暂时无法确认的可以留空。': 'Enter a clear business rule or explanation; leave unknown items blank.',
  '重新加载问题': 'Reload questions',
  '此答复已解决这条来源批注': 'This answer resolves the source comment',
  '没有需要补充的具体问题。服务恢复后，可以重新检查已保存的文档。': 'No specific questions remain. Recheck the saved document after the service recovers.',
  '本轮最多调用模型': 'Maximum model calls this round',
  '次': 'calls',
  '继续处理可能产生模型费用；达到本轮上限后停止新增调用，不会自动追加额度。此前已调用 {count} 次。': 'Further processing may incur model charges. New calls stop at this round limit without an automatic allowance. {count} call(s) were made previously.',
  '正在提交…': 'Submitting…',
  '补答并继续': 'Submit answers and continue',
  '重新检查': 'Recheck',
  '本次补充 {count} 项': '{count} answer(s) this round',
  '未填写答复，将重新检查已有内容': 'No answers entered; existing content will be rechecked',
  '已跳过本次补充': 'Skipped this confirmation',
  '可选': 'Optional',
  '重新补充': 'Add answers',
  '跳过本次补充': 'Skip confirmation',
  '展开补充确认': 'Expand confirmation',
  '收起补充确认': 'Collapse confirmation',
  '跨章节一致性检查': 'Cross-section consistency check',
  '核对疑点与原文': 'Verify issues against sources',
  '修正文档': 'Repair document',
  '模型判定疑点成立': 'Model confirmed the issue',
  '模型判定疑点不成立': 'Model dismissed the issue',
  '仍需确认': 'Still needs confirmation',
  '模型认为不成立，但结构或来源校验仍失败，保留待确认': 'Model dismissed it, but structural or source checks still fail; kept pending',
  '进行中': 'In progress',
  '已完成': 'Completed',
  '未完成': 'Incomplete',
  '已中止': 'Stopped',
  '本轮汇总': 'Round summary',
  '开始': 'Started',
  '已结束': 'Finished',
  '当前步骤': 'Current step',
  '检查单元': 'Check unit',
  '第': 'No.',
  '组': 'groups',
  '项': 'items',
  '共': 'of',
  '本轮进度': 'Round progress',
  '涉及章节': 'Sections involved',
  '未命名章节': 'Unnamed section',
  'revision': 'revision',
  '检查原因': 'Review reasons',
  '发现的疑点': 'Issues found',
  '核查结果': 'Verification result',
  '判断说明': 'Reasoning',
  '模型任务调用': 'Model task calls',
  '复用缓存': 'Cache reused',
  '调用统计': 'Call statistics',
  '调用明细': 'Call details',
  '一致性检查': 'Consistency check',
  '业务任务审阅': 'Business task review',
  '审阅重点核对': 'Review focus verification',
  '安排审阅范围': 'Plan review scope',
  '精查原文': 'Inspect original sources',
  '核查修订内容': 'Verify revised content',
  '抽取内容事实': 'Extract content facts',
  '筛选相关资料': 'Select relevant sources',
  '读取审阅资料': 'Read review sources',
  '补充内容缺口': 'Complete content gaps',
  '步骤': 'Step',
  '章节': 'Sections',
  '尝试': 'Attempt',
  '已尝试 {count} 次': 'Attempted {count} time(s)',
  '定位编号': 'Diagnostic ID',
  '错误类型': 'Error type',
  '模型动作': 'Model action',
  '任务编号': 'Task ID',
  '已接收字符': 'Received characters',
  '结尾空白字符': 'Trailing whitespace characters',
  '思考字符': 'Reasoning characters',
  '耗时（秒）': 'Time (s)',
  '首字等待（秒）': 'Time to first token (s)',
  '超时上限（秒）': 'Timeout limit (s)',
  '重命名': 'Rename',
  '取消置顶': 'Unpin',
  '置顶': 'Pin',
  '仅删除对话': 'Delete conversation only',
  '删除对话和文件': 'Delete conversation and files',
  '{title}的会话操作': 'Conversation actions for {title}',
  '关闭模型设置': 'Close model settings',
  '连接你的生成模型，按需开启判断服务和网络搜索。': 'Connect a generation model and optionally enable decision and web-search services.',
  '接口方式': 'API mode',
  '自定义接口 · OpenAI 兼容': 'Custom API · OpenAI compatible',
  '生成模型': 'Generation model',
  '接口地址': 'Endpoint',
  '填写基础地址，不包含 /chat/completions。': 'Enter the base URL without /chat/completions.',
  'Jev 原生接口：地址原样使用。': 'Native Jev endpoint: the URL is used as entered.',
  'OpenAI 兼容接口：填写 Base URL，不含 /chat/completions。': 'OpenAI-compatible endpoint: enter the base URL without /chat/completions.',
  '填写服务实际提供的模型 ID，例如 jev-latest。': 'Enter a model ID offered by the service, such as jev-latest.',
  '模型名称': 'Model name',
  '服务提供的模型 ID': 'Provider model ID',
  '内网服务无需 API Key': 'Internal service requires no API key',
  '测试中…': 'Testing…',
  '测试 LLM': 'Test LLM',
  '判断服务': 'Decision service',
  '判断模型': 'Decision model',
  '测试判断模型': 'Test decision model',
  '测试 Jev': 'Test Jev',
  '启用判断服务': 'Enable decision service',
  '网络搜索': 'Web search',
  '测试 Tavily': 'Test Tavily',
  '启用 Tavily 搜索': 'Enable Tavily search',
  '高级参数': 'Advanced settings',
  '最大输入 Token': 'Maximum input tokens',
  '最大输出 Token': 'Maximum output tokens',
  '默认输入 600,000 Token、输出 128,000 Token。输出是额度上限，模型可以提前结束；实际请求仍须符合服务支持的模型容量。': 'Defaults are 600,000 input tokens and 128,000 output tokens. Output is an upper bound and requests must still fit provider capacity.',
  '目录规划最大输出 Token': 'Maximum outline-planning output tokens',
  '留空则沿用最大输出 Token': 'Leave blank to use the maximum output tokens',
  '仅用于目录规划和目录修正。': 'Used only for outline planning and revision.',
  '当前生成模型支持图片输入': 'Generation model accepts image input',
  '密钥仅保存在服务端，不写入浏览器存储。可选服务测试通过后可启用，保存后对新任务生效。': 'Keys are stored only on the server, not in browser storage. Optional services can be enabled after testing and apply to new tasks.',
  '保存配置': 'Save settings',
  '尚未测试': 'Not tested',
  '测试通过后可启用': 'Can be enabled after testing',
  '测试通过': 'Test passed',
  '生成已停止，点击右侧继续': 'Generation is paused. Select resume on the right.',
  '文档正在生成，可随时切换会话查看其他任务': 'The document is generating. You can switch conversations at any time.',
  '继续询问资料或文档内容…': 'Continue asking about the sources or document…',
  '告诉我你想生成什么文档，以及用途和要求…': 'Describe the document, its purpose and requirements…',
  '继续生成': 'Resume generation',
  '正在停止': 'Stopping',
  '停止生成': 'Stop generation',
  '停止回答': 'Stop response',
  '请求失败，请稍后重试。': 'Request failed. Please try again.',
  '请求失败，请检查输入或服务配置。': 'Request failed. Check the input or service configuration.',
  '请求失败，请检查材料或模型配置。': 'Request failed. Check the sources or model configuration.',
  '后台未返回事件流。': 'The server did not return an event stream.',
  '上传已取消，请重试。': 'Upload cancelled. Please retry.',
  '请等待当前处理结束，再调整资料。': 'Wait for the current task to finish before changing sources.',
  '每个会话最多上传 200 份资料。': 'Each conversation supports at most 200 sources.',
  '资料已上传并保存，可继续描述文档需求。': 'Sources are uploaded and saved. You can continue describing the document.',
  '请等待这份资料上传完成后再移除。': 'Wait for this source to finish uploading before removing it.',
  '已设为业务资料，请重新生成目录。': 'Set as a business source. Regenerate the outline.',
  '已设为文档模板，将按其中的章节和格式生成。请重新生成目录。': 'Set as a document template. Regenerate the outline to apply its sections and formatting.',
  '已设为风格示例，只参考写法和排版，不作为业务事实。请重新生成目录。': 'Set as a style reference for writing and layout only. Regenerate the outline.',
  '根据资料和对话规划目录，确认后生成正文。': 'Plan the outline from sources and conversation, then generate the body after confirmation.',
  '对话名称需要填写 1～200 个字符。': 'Conversation names must contain 1–200 characters.',
  '这个会话仍在处理，请先停止处理后再删除。': 'This conversation is still processing. Stop it before deleting.',
  '资料正在更新，请等待完成后再删除会话。': 'Sources are updating. Wait before deleting the conversation.',
  '对话已不存在，列表已更新': 'The conversation no longer exists; the list was refreshed.',
  '对话及其独有文件已删除': 'The conversation and its exclusive files were deleted',
  '对话已删除，文件已保留': 'The conversation was deleted; files were retained',
  '共用文件已保留': 'shared files were retained',
  '风格分析过程 · 已保存结果': 'Style analysis · results saved',
  '风格分析未完成 · 查看过程': 'Style analysis incomplete · view process',
  '处理过程 · 未完成': 'Process · incomplete',
  '风格分析连接已结束 · 查看过程': 'Style analysis connection ended · view process',
  '处理过程 · 连接已结束': 'Process · connection ended',
  '处理连接已结束': 'Processing connection ended',
  '请先恢复当前会话，或新建会话后再发送。': 'Restore the current conversation or create a new one before sending.',
  '请根据已上传资料和我们的讨论，规划完整文档目录供我确认。': 'Plan a complete document outline from the uploaded sources and our discussion for confirmation.',
  '请将单条消息控制在 600,000 字符以内。': 'Keep each message within 600,000 characters.',
  '资料正在更新，请完成后再发送。': 'Sources are updating. Wait before sending.',
  '请重试或移除上传失败的资料后再发送。': 'Retry or remove failed uploads before sending.',
  '请先上传至少一份资料，再生成目录。': 'Upload at least one source before generating an outline.',
  '资料状态已改变，请等待上传完成后再发送。': 'Source status changed. Wait for uploads to finish before sending.',
  '搜索服务': 'Search provider',
  '测试搜索服务': 'Test search',
  '启用网络搜索': 'Enable web search',
  '实际耗时': 'Elapsed time',
  '正在加载...': 'Loading…',
  '正在加载目录版式…': 'Loading outline layout…',
  '页面图片加载失败，已显示目录结构。': 'Page images failed to load; showing the outline structure.',
  '请结合资料研究记录确认章节结构，确认后开始生成正文。': 'Review the source research record and confirm the section structure before body generation.',
  '根据最新要求重新生成目录': 'Regenerate the outline from the latest requirements',
  '请操作最新的文档目录。': 'Use the latest document outline.',
  '修改目录': 'Revise outline',
  '请填写目录修改建议。': 'Enter outline revision instructions.',
  '文档已继续处理，最新版本会另行提供下载。': 'Document processing continued; the latest version will be available separately.',
  '已跳过本次补充，当前文档可直接下载，之后仍可重新补充。': 'Confirmation was skipped. The current document is downloadable and can be refined later.',
  '请填写 1 到 10000 之间的整数作为本轮调用上限。': 'Enter an integer from 1 to 10,000 as this round’s call limit.',
  '补答前草稿.docx': 'draft-before-confirmation.docx',
  '重新检查文档。': 'Recheck the document.',
  '风格分析已中断，已保存完成的步骤。': 'Style analysis was interrupted; completed steps were saved.',
  '此回答未完整结束': 'This response did not finish completely',
  '请确认文档目录。': 'Confirm the document outline.',
  '暂时无法恢复会话': 'Unable to restore the conversation',
  '历史记录仍保留，请刷新或从左侧重试。': 'History remains saved. Refresh or retry from the left sidebar.',
  '工作台导航': 'Workspace navigation',
  'Folio 首页': 'Folio home',
  '历史会话': 'Conversation history',
  '调整历史会话宽度': 'Resize conversation history',
  '关闭导航': 'Close navigation',
  '打开导航': 'Open navigation',
  '会话暂时未能加载，历史内容仍保存在后台。': 'The conversation could not be loaded; history remains saved on the server.',
  '重新加载': 'Reload',
  '消息输入区': 'Message input',
  '调整文档预览宽度': 'Resize document preview',
  '调整目录与页面预览高度': 'Resize outline and page preview',
  '拖动调整高度，双击恢复默认': 'Drag to resize; double-click to restore defaults',
  '目录 {directory}%，页面预览 {page}%': 'Outline {directory}%, page preview {page}%',
  '关闭提示': 'Close notification',
  '重命名失败，请稍后重试。': 'Rename failed. Try again later.',
  '删除失败，请稍后重试。': 'Delete failed. Try again later.',
  '会话操作': 'Conversation actions',
  '具体原因': 'Specific reason',
  '结束标记': 'Finish reason',
  '未知': 'Unknown',
  '未收到': 'Not received',
  '请求编号': 'Request ID',
  '阶段': 'Stage',
  'HTTP 状态': 'HTTP status',
  '秒': 's',
  '错误代码': 'Error code',
  '接口诊断': 'API diagnostics',
  '结构化输出 Schema 无效': 'Invalid structured-output schema',
  'Schema 约束': 'Schema constraint',
  '所有对象字段都必须声明为必填': 'All object fields must be required',
  '意图识别': 'Intent classification',
  '资料上传': 'Source upload',
  '风格分析': 'Style analysis',
  '目录规划': 'Outline planning',
  '目录修改': 'Outline revision',
  '启动文档任务': 'Start document task',
  '正文生成': 'Body generation',
  '对话回复': 'Chat response',
  '已保存 · 留空保持不变': 'Saved · leave blank to keep',
  '已保存，填写可替换': 'Saved; enter a replacement',
  '输入 API Key': 'Enter API key',
  '选填 Jev API Key': 'Optional Jev API key',
  '选填 Tavily API Key': 'Optional Tavily API key',
}

// Server metadata covers dynamic system messages while leaving source content untouched.
const runtimeTranslations = reactive(new Map<string, string>())
const runtimeOriginals = reactive(new Map<string, string>())
const catalogOriginals = new Map(Object.entries(catalog).map(([original, translated]) => [translated, original]))

export function registerRuntimeTranslations(payload: unknown): void {
  if (!payload || typeof payload !== 'object') return
  const translations = (payload as { _translations?: unknown })._translations
  if (!translations || typeof translations !== 'object' || Array.isArray(translations)) return
  for (const [original, translated] of Object.entries(translations)) {
    if (!original || typeof translated !== 'string' || !translated) continue
    runtimeTranslations.set(original, translated)
    runtimeOriginals.set(translated, original)
  }
}

export function setLanguage(value: Language) {
  language.value = value
  try { localStorage.setItem(STORAGE_KEY, value) } catch { /* Local persistence is optional. */ }
  updateDocumentLanguage()
}

function updateDocumentLanguage() {
  if (typeof document === 'undefined') return
  document.documentElement.lang = language.value
  document.title = tr('Folio · 智能文档工作台')
  document.querySelector('meta[name="description"]')?.setAttribute('content', tr('Folio：从资料到成文的智能文档工作台。'))
}
updateDocumentLanguage()

export function tr(source: string): string {
  return language.value === 'en' ? catalog[source] || source : source
}

export function trf(source: string, values: Record<string, string | number>): string {
  const translated = tr(source)
  return translated.replace(/\{(\w+)\}/g, (_, key: string) => String(values[key] ?? `{${key}}`))
}

export function formatElapsed(seconds?: number): string {
  const total = Math.max(0, Math.floor(seconds || 0))
  if (language.value === 'en') {
    return total < 60 ? `${total}s` : `${Math.floor(total / 60)}m ${total % 60}s`
  }
  return total < 60 ? `${total} 秒` : `${Math.floor(total / 60)} 分 ${total % 60} 秒`
}

export function formatTokens(value?: number): string {
  return Math.max(0, Math.floor(value || 0)).toLocaleString(language.value)
}

export function acceptLanguageHeaders(extra: HeadersInit = {}): HeadersInit {
  return { ...Object.fromEntries(new Headers(extra).entries()), 'Accept-Language': language.value }
}

const runtimePatterns: Array<[RegExp, (...groups: string[]) => string]> = [

  [/^模型配置未读取：(.+)$/, error => `Model settings could not be loaded: ${localizeRuntimeText(error)}`],
  [/^当前后台不支持以下配置项：(.+)。请更新或重启 Folio 后台，再刷新页面重试。$/, labels => `The server does not support these settings: ${labels.split('、').map(tr).join(', ')}. Update or restart the Folio server, then refresh and try again.`],
  [/^另有 (\d+) 份文件未加入。$/, count => `${count} additional file(s) were not added.`],
  [/^暂时无法恢复会话：(.+)。历史记录仍保留，请刷新或从左侧重试。$/, error => `Unable to restore the conversation: ${localizeRuntimeText(error)}. History remains saved. Refresh or retry from the sidebar.`],
  [/^第 (\d+) 页$/, page => `Page ${page}`],
  [/^第 (\d+)[—～–-](\d+) 页$/, (first, last) => `Pages ${first}–${last}`],
  [/^第 (\d+) 张幻灯片$/, page => `Slide ${page}`],
  [/^第 (\d+)—(\d+) 个文本块$/, (first, last) => `Text blocks ${first}–${last}`],
  [/^已补充 (\d+) 项说明，继续检查文档。$/, count => `Added ${count} answer(s) and continuing document checks.`],
  [/^本轮最多 (\d+) 次模型调用。$/, count => `This round allows at most ${count} model calls.`],

  [/^正在理解你的请求，判断需要执行的操作。$/, () => 'Understanding your request and determining the next action.'],
  [/^《(.+?)》的(.+?)未完成：(.+?)已尝试 (\d+) 次。已保存 (\d+)\/(\d+) 个正文章节，已有内容和检索进度保留。$/, (location, action, reason, attempts, saved, total) => `${localizeRuntimeText(action)} did not complete for “${location}”: ${localizeRuntimeText(reason)} Attempted ${attempts} time(s). Saved ${saved}/${total} body sections; existing content and retrieval progress were retained.`],
  [/^《(.+?)》的(.+?)未完成：(.+?)已保存 (\d+)\/(\d+) 个正文章节，已有内容和检索进度保留。$/, (location, action, reason, saved, total) => `${localizeRuntimeText(action)} did not complete for “${location}”: ${localizeRuntimeText(reason)} Saved ${saved}/${total} body sections; existing content and retrieval progress were retained.`],
  [/^已保存 (\d+)\/(\d+) 个正文章节，已有内容和检索进度保留。$/, (saved, total) => `Saved ${saved}/${total} body sections; existing content and retrieval progress were retained.`],
  [/^正在将补充内容整合到《(.+?)》（第 (\d+)\/(\d+) 项(?:，资料已分批核对)?）。$/, (section, index, total) => `Integrating supplemental content into “${section}” (item ${index}/${total}).`],
  [/^正在将补充内容整合到《(.+?)》。$/, section => `Integrating supplemental content into “${section}”.`],
  [/^《(.+?)》第 (\d+)\/(\d+) 项：正在筛选相关约束和必要原文。$/, (section, index, total) => `“${section}” item ${index}/${total}: selecting relevant constraints and required source text.`],
  [/^《(.+?)》第 (\d+)\/(\d+) 项已保存为待确认记录。$/, (section, index, total) => `“${section}” item ${index}/${total} was saved as a pending-confirmation record.`],
  [/^《(.+?)》补充内容已整合 (\d+)\/(\d+) 项。$/, (section, done, total) => `Supplemental content integrated into “${section}”: ${done}/${total} item(s).`],
  [/^“(.+?)”局部检查完成：(\d+) 项需处理的问题。$/, (section, count) => `Focused review of “${section}” completed with ${count} issue(s) to handle.`],
  [/^《(.+?)》(?:的)?(.+?)第 (\d+) 次未通过：(.+?)随后已发起重试。$/, (location, action, attempt, reason) => `${localizeRuntimeText(action)} attempt ${attempt} did not pass for “${location}”: ${localizeRuntimeText(reason)} A retry was started.`],
  [/^《(.+?)》(?:的)?(.+?)第 (\d+) 次未通过：(.+?)本次请求不再自动重试，后续由当前阶段处理。$/, (location, action, attempt, reason) => `${localizeRuntimeText(action)} attempt ${attempt} did not pass for “${location}”: ${localizeRuntimeText(reason)} This request will not retry automatically; the current stage will handle it.`],
  [/^《(.+?)》正在进行第 (\d+) 次(.+?)，修正上次未完成的结果。$/, (location, attempt, action) => `Retrying ${localizeRuntimeText(action)} for “${location}” (attempt ${attempt}) to correct the incomplete result.`],
  [/^目录规划未完成，未启动正文生成。(.+)$/, message => `Outline planning did not finish, so body generation was not started. ${localizeRuntimeText(message)}`],
  [/^正在浏览资料清单，研究相关原文后生成目录。$/, () => 'Reviewing the source list and studying the relevant originals before planning the outline.'],
  [/^正在准备资料清单和结构索引，随后研究相关原文再规划目录。$/, () => 'Preparing the source list and structure index, then researching relevant originals before planning the outline.'],
  [/^正在准备资料目录与写作要求。$/, () => 'Preparing the source directory and writing requirements.'],
  [/^正在整理上传资料的文件清单和内部结构，准备选择需要阅读的内容。$/, () => 'Organising the uploaded files and their internal structure before selecting content to read.'],
  [/^正在整理本次对话与可用资料，准备回答。$/, () => 'Reviewing this conversation and the available sources to prepare a response.'],
  [/^正在核对资料研究结果：检查目录要求是否引用了已读原文、资料来源是否一致。$/, () => 'Checking outline research results: verifying that outline requirements cite read source text and that source provenance is consistent.'],
  [/^正在等待模型返回完整目录（第 (\d+) 轮），尚未生成正文。$/, round => `Waiting for the model to return the complete outline (round ${round}); body generation has not started.`],
  [/^正在等待模型修正出错字段（第 (\d+) 轮），保留其余目录。$/, round => `Waiting for the model to repair invalid outline fields (round ${round}) while preserving the rest of the outline.`],
  [/^正在按目录编号理解修改建议（第 (\d+) 轮），仅返回需要执行的编辑。$/, round => `Applying the outline revision by node number (round ${round}); only required edits will be returned.`],
  [/^模型已返回目录，正在核对标题层级、节点用途和重复章节。$/, () => 'The model returned an outline. Checking heading levels, node purposes and duplicate sections.'],
  [/^正在对照已读取的资料要求，检查目录是否有明确的写作安排。$/, () => 'Comparing the outline with requirements from the sources that were read and checking that each requirement has a clear writing location.'],
  [/^本轮目录输出达到长度上限，准备第 (\d+) 轮：缩短写作说明、保留完整材料和目录，并检查可用输出空间。$/, round => `This outline response reached its output limit. Preparing round ${round} with shorter writing notes, complete source context and the full outline while checking available output capacity.`],
  [/^本轮目录未通过检查，准备第 (\d+) 轮修正。$/, round => `This outline did not pass validation. Preparing correction round ${round}.`],
  [/^目录校验通过，共 (\d+) 个节点，正在排版 Word 预览。$/, count => `Outline validation passed with ${count} node(s). Rendering the Word preview.`],
  [/^已整理 (\d+) 个目录节点，仍有待解决事项，正在排版初稿预览。$/, count => `Prepared ${count} outline node(s), but some items remain unresolved. Rendering the draft preview.`],
  [/^Word 目录预览已生成，正在保存可确认版本。$/, () => 'The Word outline preview is ready. Saving the version available for confirmation.'],
  [/^Word 目录初稿已生成，正在保存研究成果和未通过检查的具体原因。$/, () => 'The Word outline draft is ready. Saving the research results and the specific failed checks.'],
  [/^正在读取(.+?)的结构，准备分批分析全部可提取文本。$/, names => `Reading the structure of ${names} before analysing all extractable text in batches.`],
  [/^目录未通过结构校验，未启动正文生成。请调整要求或重试。$/, () => 'The outline failed structural validation, so body generation was not started. Adjust the requirements or try again.'],
  [/^本次修改未成功，已有目录仍保留。(.+)$/, message => `The revision was not applied; the existing outline was preserved. ${localizeRuntimeText(message)}`],
  [/^目录初稿已生成，检查尚未完成；请查看研究记录中的具体原因。$/, () => 'An outline draft was generated, but checks are incomplete. Review the source research record for details.'],
  [/^任务“(.+)”统计已更新：([\d.]+) 秒，([\d,]+) Token。$/, (title, seconds, tokens) => `Task “${title}” usage updated: ${seconds}s, ${tokens} tokens.`],
  [/^开始业务任务“(.+)”：依赖已满足。$/, title => `Starting business task “${title}”: dependencies are satisfied.`],
  [/^业务任务“(.+)”整体检查完成：(\d+) 项需处理的问题。$/, (title, count) => `Business task “${title}” review complete: ${count} issue(s) to handle.`],
  [/^正在检索业务任务“(.+)”（第 (\d+) 次），补充共同业务依据。$/, (title, count) => `Researching business task “${title}” (round ${count}) for shared evidence.`],
  [/^“(.+)”开始第 (\d+)\/3 轮本地资料研究。$/, (title, count) => `Task “${title}” started local source research round ${count}/3.`],
  [/^“(.+)”研究资料：(.+)$/, (title, action) => `Task “${title}” research: ${action}`],
  [/^“(.+)”已生成 (\d+) 项模型建议，补全材料缺口。$/, (title, count) => `Task “${title}” generated ${count} model proposal(s) for source gaps.`],
  [/^已建立业务执行计划：(\d+) 个任务，分别负责对应的文档内容节点。$/, count => `Business plan created with ${count} task(s).`],
  [/^任务“(.+)”负责：(.+)$/, (title, sections) => `Task “${title}” owns: ${sections}.`],
  [/^文档草稿已生成，还有 (\d+) 项需要确认。$/, count => `A draft is ready with ${count} item(s) requiring confirmation.`],
  [/^已自动补全资料缺口，正文包含 (\d+) 处标注为「模型建议」的内容。$/, count => `Source gaps were completed automatically; the body contains ${count} item(s) marked as model proposals.`],
  [/^已保存 (\d+)\/(\d+) 节正文。$/, (saved, total) => `${saved}/${total} sections saved.`],
  [/^目录预览 · (\d+) 页$/, count => `Outline preview · ${count} pages`],
  [/^(\d+) 个目录节点$/, count => `${count} outline nodes`],
  [/^已补充 (\d+) 项说明，继续检查文档。本轮最多 (\d+) 次模型调用。$/, (answers, calls) => `Added ${answers} answer(s) and continuing checks. This round allows at most ${calls} model calls.`],
  [/^本次补充 (\d+) 项$/, count => `${count} answer(s) this round`],
  [/^本步已用时 (.+)$/, elapsed => `Current step: ${elapsed}`],
  [/^已用时 (.+)$/, elapsed => `Elapsed: ${elapsed}`],
  [/^用时 (.+)$/, elapsed => `Time: ${elapsed}`],
  [/^(\d+) 份资料上传失败，可在列表中重试或移除。$/, count => `${count} source(s) failed to upload. Retry or remove them from the list.`],
  [/^(\d+) 个节点$/, count => `${count} nodes`],
  [/^(\d+) 页$/, count => `${count} pages`],
  [/^正在(继续)?浏览资料文件清单，查找与本次文档有关的文件(?:。本步目的：(.+))?。?$/, (more, purpose) => `${more ? 'Continuing to scan' : 'Scanning'} the source list for files relevant to this document.${purpose ? ` Purpose: ${purpose.replace(/[。.]+$/, '')}.` : ''}`],
  [/^正在(上传资料|《.+?》)中查找(?:“(.+)”的相关内容|相关要求的位置)。$/, (target, query) => query ? `Searching ${target} for “${query}”.` : `Searching ${target} for relevant requirements.`],
  [/^正在归纳已读原文，保存有出处的研究笔记。$/, () => 'Summarising read source text and saving source-backed research notes.'],
  [/^正在(继续)?查看(已经读取的原文依据|此前的资料阅读记录|完整编制要求|模板的完整约束|已保存的研究笔记|资料阅读历史索引|任务上下文)。$/, (more, target) => `${more ? 'Continuing to inspect' : 'Inspecting'} ${target}.`],
  [/^正在联网查找(?:“(.+)”|与目录有关的公开资料)(?:。目录用途：(.+))?。$/, (query, purpose) => `${query ? `Searching public sources for “${query}”` : 'Searching public sources for outline-relevant information'}.${purpose ? ` Outline purpose: ${purpose}` : ''}`],
  [/^联网搜索暂不可用，未取得(.+)的公开资料；已保留记录。$/, target => `Web search was unavailable; no public sources were obtained for ${target}. The attempt was recorded.`],
  [/^对(.+)的联网搜索已中断，已保留记录。$/, target => `Web search for ${target} was interrupted. The attempt was recorded.`],
  [/^联网搜索完成：(.+)，取得 (\d+) 个公开来源.*$/, (target, count) => `Web search completed for ${target} with ${count} public source(s).`],
  [/^本批研究笔记已保存，正在安排后续资料阅读。$/, () => 'The research notes for this batch were saved. Planning the next source-reading step.'],
  [/^已取得(已经读取的原文依据|此前的资料阅读记录|完整编制要求|模板的完整约束|已保存的研究笔记|资料阅读历史索引|任务上下文)的 (\d+) 字内容.*$/, (target, count) => `Read ${count} characters from ${target}.`],
  [/^本次未能完成对(.+)的读取，正在调整阅读方式。$/, target => `Unable to read ${target} this time. Adjusting the reading approach.`],
  [/^资料清单暂未读取成功，正在调整读取方式。$/, () => 'The source list could not be read. Adjusting the reading approach.'],
  [/^AI 返回的操作格式不完整，正在重新请求下一步资料阅读安排。$/, () => 'The model returned an incomplete action. Requesting the next source-reading step again.'],
  [/^资料研究结论格式不完整，正在重新整理目录要求和原文引用。$/, () => 'The research conclusion was incomplete. Reorganising outline requirements and source citations.'],
  [/^研究笔记尚未通过原文引用核对，正在修正归纳内容。$/, () => 'Research notes did not pass source-citation verification. Correcting the summary.'],
  [/^正在(继续)?查看(.+?)的内部结构，定位需要阅读的章节或页面(?:。本步目的：(.+))?。?$/, (more, name, purpose) => `${more ? 'Continuing to inspect' : 'Inspecting'} the structure of ${name} to locate relevant sections or pages.${purpose ? ` Purpose: ${purpose.replace(/[。.]+$/, '')}.` : ''}`],
  [/^正在(继续)?读取(.+?)，核对目录需要覆盖的要求(?:。本步目的：(.+))?。?$/, (more, target, purpose) => `${more ? 'Continuing to read' : 'Reading'} ${target} to verify outline coverage.${purpose ? ` Purpose: ${purpose.replace(/[。.]+$/, '')}.` : ''}`],
  [/^正在(继续)?查看(.+)。$/, (more, target) => `${more ? 'Continuing to inspect' : 'Inspecting'} ${target}.`],
  [/^已取得(.+?)原文，本批 (\d+) 字符.*$/, (target, count) => `Read ${count} characters from ${target}; checking requirements for the outline.`],
  [/^已获取(.+)的 (\d+) (个章节标题|条内部结构信息).*$/, (name, count, unit) => `Inspected ${count} ${unit.includes('章节') ? 'section headings' : 'structure items'} in ${name}.`],
  [/^已找到 (\d+) 条相关片段的位置.*$/, count => `Located ${count} relevant passage(s); selecting ranges to read in context.`],
  [/^已取得本页 (\d+) 份文件信息.*$/, count => `Loaded information for ${count} source(s) on this page.`],
  [/^正在分析 (\d+) 份资料的文件清单.*$/, count => `Analysing the source list for ${count} file(s).`],
  [/^正在并行处理 (\d+) 个资料读取任务.*$/, count => `Processing ${count} source-reading task(s) in parallel.`],
  [/^目录资料研究完成：实际读取 (\d+) 份资料，提取 (\d+) 项有依据的目录要求。$/, (files, reqs) => `Outline research complete: read ${files} source(s) and extracted ${reqs} supported outline requirement(s).`],
  [/^第 (\d+) 轮资料研究检查发现：(.+)；(.+)。$/, (round, detail, next) => `Outline research check ${round}: ${detail}. ${next}.`],
  [/^正在生成 Word 文件。$/, () => 'Generating the Word document.'],
  [/^正在规划文档章节。$/, () => 'Planning document sections.'],
  [/^正在按文档内容划分执行任务，安排内容节点和任务依赖。$/, () => 'Grouping content into business tasks and resolving dependencies.'],
  [/^开始按任务检索，整理共享术语和事实并生成正文。$/, () => 'Researching by task, consolidating shared facts, and generating the body.'],
  [/^正在检查章节之间的术语与规则。$/, () => 'Checking terminology and rules across sections.'],
  [/^正在生成并检查界面原型和流程图。$/, () => 'Generating and checking interface mockups and flowcharts.'],
  [/^正在检查和调整文档排版，保留全部正文内容。$/, () => 'Checking and adjusting document layout while preserving all content.'],
  [/^有 (\d+) 份资料上传失败，可在资料库中重试。$/, count => `${count} source(s) failed to upload. Retry from Sources.`],
  [/^无法移除资料：(.+)$/, error => `Unable to remove source: ${localizeRuntimeText(error)}`],
  [/^文件用途未保存：(.+)$/, error => `File purpose was not saved: ${localizeRuntimeText(error)}`],
  [/^无法切换会话：(.+)。当前内容已保留。$/, error => `Unable to switch conversations: ${localizeRuntimeText(error)}. Current content was retained.`],
  [/^无法(重命名|更新置顶状态)：(.+)$/, (action, error) => `Unable to ${action === '重命名' ? 'rename' : 'update pin status'}: ${localizeRuntimeText(error)}`],
  [/^无法删除会话：(.+)$/, error => `Unable to delete conversation: ${localizeRuntimeText(error)}`],
  [/^请先恢复连接并保存草稿：(.+)$/, error => `Restore the connection and save the draft first: ${localizeRuntimeText(error)}`],
  [/^修改建议：(.+)$/, advice => `Revision request: ${advice}`],
  [/^待确认问题未能加载：(.+)$/, error => `Unable to load confirmation questions: ${localizeRuntimeText(error)}`],
  [/^未能继续处理，填写内容已保留：(.+)$/, error => `Unable to continue; answers were retained: ${localizeRuntimeText(error)}`],
  [/^未能继续生成，已保存的进度仍保留：(.+)$/, error => `Unable to continue generation; saved progress was retained: ${localizeRuntimeText(error)}`],
  [/^(继续|暂停)失败：(.+)$/, (action, error) => `${action === '继续' ? 'Resume' : 'Pause'} failed: ${localizeRuntimeText(error)}`],
  [/^(.+?)：(请使用 Word、Excel、PDF、PPTX、文本或 PNG\/JPEG\/TIFF\/WebP 图片。|文件不能为空。)$/, (filename, detail) => `${filename}: ${tr(detail)}`],
  [/^(接口方式|接口地址|生成模型|免密访问|判断模型|判断服务|网络搜索|最大输出长度|目录规划输出长度|温度|最大输入 Token|模型上下文窗口|本地 token 估算编码|思考模式|结构化输出|图片输入|配置项|LLM API Key|Jev API Key|Tavily API Key)：(.+)$/, (label, detail) => `${tr(label)}: ${localizeRuntimeText(detail)}`],
]

// Keep composed client-side errors in their original form until rendering.
export function runtimeSourceText(source: string): string {
  return runtimeOriginals.get(source) || catalogOriginals.get(source) || source
}

export function localizeRuntimeText(source?: string): string {
  if (!source) return ''
  if (language.value === 'zh-CN') {
    return runtimeSourceText(source)
  }
  // Historical streams may come from a worker that predates the full-sentence catalog.
  // Capture titles separately so user text is never recursively translated.
  const review = source.match(/^跨章节检查第 (\d+)\/(\d+) 组：《(.*?)》(?:、《(.*?)》)?(?:等 (\d+) 节)?。$/)
  if (review) {
    const [, index, total, first, second, count] = review
    return `Cross-section review, group ${index}/${total}: “${first}”${second === undefined ? '' : `, “${second}”`}${count ? ` and others (${count} sections total)` : ''}.`
  }
  const translated = runtimeTranslations.get(source)
  if (translated) return translated
  if (catalog[source]) return catalog[source]!
  if (source.includes('\n')) return source.split('\n').map(localizeRuntimeText).join('\n')
  for (const [pattern, replacer] of runtimePatterns) {
    const match = source.match(pattern)
    if (match) return englishRuntimeText(replacer(...match.slice(1)))
  }
  return catalog[source] || source
}

const modelTestCodes: Record<string, [string, string]> = {
  native_tools_verified: [
    '连接测试通过，已验证模型支持原生工具调用。',
    'Connection verified with native tool support.',
  ],
  native_tools_required: [
    '未收到有效的原生工具调用；当前接口或模型不满足资料研究要求。',
    'No valid native tool call was received; this endpoint or model is not suitable for source research.',
  ],
  model_test_failed: [
    '模型连接测试失败，请检查配置或稍后重试。',
    'The model connection test failed. Check the configuration or try again later.',
  ],
  decision_verified: [
    '决策模型测试通过，已收到有效的结构化判断。',
    'Decision service verified with a valid structured result.',
  ],
  decision_rejected: [
    '决策模型未返回有效判断，请检查配置。',
    'The decision service did not return a valid result. Check its configuration.',
  ],
  decision_timeout: [
    '决策服务响应超时，请稍后重试。',
    'The decision service timed out. Try again later.',
  ],
  decision_test_failed: [
    '决策服务测试失败，请检查配置或稍后重试。',
    'The decision service test failed. Check the configuration or try again later.',
  ],
  search_verified: [
    '联网搜索测试通过，已收到 Tavily 检索响应。',
    'Web search verified with a valid Tavily response.',
  ],
  search_auth_failed: [
    'Tavily 认证失败，请检查 API Key。',
    'Tavily authentication failed. Check the API key.',
  ],
  search_request_failed: [
    'Tavily 测试失败，请检查服务额度或稍后重试。',
    'Tavily search failed. Check the service quota or try again later.',
  ],
  search_invalid_response: [
    'Tavily 未返回有效的检索结果。',
    'Tavily did not return a valid search result.',
  ],
  search_timeout: [
    'Tavily 响应超时，请稍后重试。',
    'Tavily timed out. Try again later.',
  ],
  search_connection_failed: [
    '无法连接 Tavily，请检查 API Key 和网络。',
    'Unable to connect to Tavily. Check the API key and network.',
  ],
}

export function localizeModelTestCode(code?: string): string {
  if (!code) return ''
  const labels = modelTestCodes[code]
  return labels ? labels[language.value === 'en' ? 1 : 0] : ''
}

function englishRuntimeText(text: string): string {
  const quoted: string[] = []
  const protectedText = text.replace(/([《“])([^》”]*)([》”])/g, (_, open: string, content: string, close: string) => {
    const index = quoted.push(content) - 1
    return `${open}\uE000${index}\uE001${close}`
  })
  return protectedText
    .replace(/《([^》]+)》的“([^”]+)”/g, '“$2” in “$1”')
    .replace(/《([^》]+)》的工作表“([^”]+)”/g, 'worksheet “$2” in “$1”')
    .replace(/《([^》]+)》的正文原文/g, 'the body of “$1”')
    .replace(/《([^》]+)》的正文/g, 'the body of “$1”')
    .replace(/《([^》]+)》的第 (\d+) 张幻灯片/g, 'slide $2 of “$1”')
    .replace(/《([^》]+)》的第 (\d+) 页/g, 'page $2 of “$1”')
    .replace(/《([^》]+)》/g, '“$1”')
    .replace(/的第 (\d+) 张幻灯片/g, ' slide $1')
    .replace(/的第 (\d+) 页/g, ' page $1')
    .replace(/的工作表“([^”]+)”/g, ' worksheet “$1”')
    .replace(/第 (\d+)—(\d+) 页/g, 'pages $1–$2')
    .replace(/涉及 (\d+) 页/g, 'covering $1 pages')
    .replace(/第 (\d+) 段 \/ 共 (\d+) 段/g, 'segment $1 of $2')
    .replace(/已经读取的原文依据/g, 'previously read source evidence')
    .replace(/此前的资料阅读记录/g, 'the previous source reading record')
    .replace(/完整编制要求/g, 'the complete authoring requirements')
    .replace(/模板的完整约束/g, 'the complete template constraints')
    .replace(/已保存的研究笔记/g, 'saved research notes')
    .replace(/资料阅读历史索引/g, 'the source reading history index')
    .replace(/任务上下文/g, 'the task context')
    .replace(/资料筛选或内部结构尚未检查完整/g, 'source selection or structure has not been fully checked')
    .replace(/关键原文仍需补查/g, 'key source text still needs to be checked')
    .replace(/目录要求与原文依据尚需核对/g, 'outline requirements and source evidence still need to be verified')
    .replace(/还有影响目录范围的资料缺口/g, 'source gaps still affect the outline scope')
    .replace(/正在继续补查/g, 'continuing the follow-up check')
    .replace(/将保留缺口，目录依据尚未确认完整/g, 'the gaps will be retained because outline evidence is not yet complete')
    .replace(/(?<=Reading |read |inspect |from |for )所选资料(?=[ .;,]|$)/g, 'the selected source')
    .replace(/(?<=Searching |from |in )上传资料(?=[ .;,]|$)/g, 'uploaded sources')
    .replace(/本步目的：/g, 'Purpose: ')
    .replace(/目录用途：/g, 'Outline purpose: ')
    .replace(/([。！？])/g, (punctuation) => ({ '。': '.', '！': '!', '？': '?' }[punctuation] || punctuation))
    .replace(/：/g, ': ')
    .replace(/；/g, '; ')
    .replace(/，/g, ', ')
    .replace(/\s+([.;:])/g, '$1')
    .trim()
    .replace(/\uE000(\d+)\uE001/g, (_, index: string) => quoted[Number(index)]!)
}

export function localizeStep(source?: string): string {
  const text = localizeRuntimeText(source)
  if (language.value !== 'en') return text
  const localized = text
    .replace(/(\d+) 个节点/g, '$1 nodes')
    .replace(/第 (\d+) 页/g, 'page $1')
    .replace(/本步已用时/g, 'Current step')
    .replace(/已用时/g, 'Elapsed')
    .replace(/用时/g, 'Time')
    .replace(/(\d+) 份资料/g, '$1 sources')
    .replace(/模型已连接，等待结果/g, 'Model connected, waiting for results')
    .replace(/正在等待模型响应/g, 'Waiting for the model')
    .replace(/模型正在分析资料/g, 'Model is analysing sources')
    .replace(/正在接收模型结果/g, 'Receiving model results')
  return localized
}
