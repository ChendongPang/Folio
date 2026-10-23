import type { DisplayField } from './types';
import { language, tr } from './i18n';

export function protocolDiagnosticFields(diagnostics?: Record<string, unknown>): DisplayField[] {
  if (!diagnostics) return [];
  const fields: DisplayField[] = [];
  const protocolIssues: Record<string, string> = {
    tool_delta_invalid: '工具调用片段的结构不正确',
    tool_index_missing: '工具调用缺少有效的序号',
    multiple_tool_calls: '一次回复包含多个工具调用，当前仅支持一个',
    tool_type_unsupported: '模型返回了不支持的工具类型',
    tool_id_invalid: '工具调用编号格式不正确',
    tool_function_invalid: '工具函数信息的结构不正确',
    tool_fragment_invalid: '工具名称或参数片段的类型不正确',
    tool_finish_invalid: '工具调用的结束标记不正确',
    tool_id_missing: '工具调用缺少编号',
    tool_unknown: '模型调用了未提供的工具',
    tool_arguments_json: '工具参数不是合法的 JSON',
    tool_arguments_type: '工具参数必须是 JSON 对象',
    response_not_sse: '接口未返回预期的流式响应',
    stream_json_invalid: '流式响应片段不是合法的 JSON',
    stream_shape_invalid: '流式响应片段的结构不正确',
    upstream_stream_error: '上游接口在返回过程中报错',
    legacy_function_call: '接口返回了旧版函数调用格式',
    unexpected_tool_call: '本次请求未允许工具调用，但接口返回了工具调用',
    tool_after_finish: '结束标记之后仍收到工具调用',
    content_invalid: '模型返回的正文类型不正确',
    text_after_finish: '结束标记之后仍收到正文',
    incomplete_response: '模型响应未完整结束',
    malformed_response: '模型响应格式不正确',
  };
  const protocolIssuesEn: Record<string, string> = {
    tool_delta_invalid: 'Malformed tool-call delta',
    tool_index_missing: 'Tool call is missing a valid index',
    multiple_tool_calls: 'The response contains multiple tool calls; only one is supported',
    tool_type_unsupported: 'The model returned an unsupported tool type',
    tool_id_invalid: 'Invalid tool-call identifier',
    tool_function_invalid: 'Malformed tool-function metadata',
    tool_fragment_invalid: 'Invalid type in a tool name or argument fragment',
    tool_finish_invalid: 'Invalid tool-call finish marker',
    tool_id_missing: 'Tool call is missing an identifier',
    tool_unknown: 'The model called a tool that was not provided',
    tool_arguments_json: 'Tool arguments are not valid JSON',
    tool_arguments_type: 'Tool arguments must be a JSON object',
    response_not_sse: 'The endpoint did not return the expected event stream',
    stream_json_invalid: 'A stream frame is not valid JSON',
    stream_shape_invalid: 'A stream frame has an invalid shape',
    upstream_stream_error: 'The upstream endpoint failed while streaming',
    legacy_function_call: 'The endpoint returned a legacy function-call format',
    unexpected_tool_call: 'The endpoint returned a tool call when none was allowed',
    tool_after_finish: 'A tool call arrived after the finish marker',
    content_invalid: 'The model returned an invalid content type',
    text_after_finish: 'Text arrived after the finish marker',
    incomplete_response: 'The model response did not finish cleanly',
    malformed_response: 'The model response is malformed',
  };
  if (typeof diagnostics.protocol_issue === 'string' && Object.hasOwn(protocolIssues, diagnostics.protocol_issue)) {
    fields.push({ label: tr('具体原因'), value: (language.value === 'en' ? protocolIssuesEn : protocolIssues)[diagnostics.protocol_issue]! });
  }
  const finishReasons: Record<string, string> = {
    stop: 'stop',
    length: language.value === 'en' ? 'length (output limit reached)' : 'length（达到输出上限）',
    content_filter: 'content_filter',
    tool_calls: 'tool_calls',
    unknown: tr('未知'),
    missing: tr('未收到'),
  };
  if (typeof diagnostics.finish_reason === 'string' && Object.hasOwn(finishReasons, diagnostics.finish_reason)) fields.push({ label: tr('结束标记'), value: finishReasons[diagnostics.finish_reason]! });
  if (typeof diagnostics.research_request_id === 'string' && /^research_[0-9]{3,6}$/.test(diagnostics.research_request_id)) fields.push({ label: tr('请求编号'), value: diagnostics.research_request_id });
  return fields;
}
