# 测试与验收

> English: [Testing and acceptance](testing.md)

从仓库根目录运行以下命令；先完成 [README 中的环境准备](../README.zh-CN.md)。

## 本地检查

```bash
uv run pytest -q -r s
uv run ruff check src tests scripts
npm --prefix frontend run build
git diff --check
```

记录命令、代码版本、结果及跳过原因。专项复测只证明所选用例通过，不能覆盖原全量失败记录；有重叠的测试组不能相加成为独立用例总数。

部分集成测试依赖本机工具或测试资料：

| 范围 | 前提与验证边界 |
| --- | --- |
| 浏览器交互与配图渲染 | 需要 Playwright Chromium，可用 `uv run playwright install chromium` 安装。模拟服务响应验证界面与流程，不证明真实服务兼容性。 |
| Word 分页与页面渲染 | 需要 LibreOffice/UNO 和 Poppler；缺少工具时应查看跳过原因。 |
| 隔离研究、Office 转换与 OCR | 按[业务 Agent 研究](agent-research.zh-CN.md)说明构建 Docker 研究镜像。缺少 Docker 或镜像时，相应测试会跳过。 |
| 依赖安装 | `FOLIO_TEST_DEPENDENCY_INSTALL=1 uv run pytest -q tests/test_research_sandbox.py` 还会访问配置的受信软件包索引。 |
| 合成文档测试资料 | 公开、可重建的来源资料和预期结果位于 `examples/acceptance_cases/`。 |

可按修改范围运行专项检查：

```bash
uv run pytest -q tests/test_native_tool_runtime.py tests/test_optional_decision_routing.py
uv run pytest -q tests/test_directory_research_resume.py tests/test_directory_review.py
uv run pytest -q tests/test_research_sandbox.py tests/test_source_index.py
uv run pytest -q tests/test_document_run_audit.py tests/test_audited_replay.py
uv run pytest -q tests/test_acceptance_cases.py tests/test_acceptance_output_counting.py
```

受控 Runtime 和合成证据验证协议、调度、来源、取消与恢复契约，不能证明真实模型理解质量、OCR 准确率、视觉质量或全链路性能。

## 真实服务验收

连接探测与完整生成应分别测试。连接成功只证明基本兼容性，不能证明研究、写作、审阅和导出整条链路成功。对有代表性的文档检查：

1. 确认后的目录覆盖需求，每个必需章节都有唯一负责的业务任务。
2. 事实、计算、例外规则、来源版本及覆盖范围与原材料一致。读过清单或存在引用，不等于理解了全部材料。
3. OCR 和视觉证据与原页核对；派生计算保留输入与执行来源链。
4. 缺少依据的建议继续明确标记，未决问题继续保留。审阅未完成时，`needs_confirmation` 草稿仍可用于保存已完成成果。
5. DOCX 可打开，必需章节和配图齐全，且与保存的正文版本一致。在实际 Word/PDF 页面中检查字体、列表、表格边界、页码、图中文字和分页。

使用合成验收包时，预期答案只供验收者核对，不交给被测模型。关键词命中与篇幅检查有助于发现遗漏，但语义正确性和业务批准仍需人工核对。参见[资料与排版](source-materials-and-layout.zh-CN.md)及[配图说明](illustrations.zh-CN.md)。

## 可复现回放

[`scripts/replay_audited.py`](../scripts/replay_audited.py) 使用真实服务执行新一轮任务。应设置次数上限并保留失败、中断的运行名额；新系列使用新目录，源任务保持只读，每轮执行期间保持代码、源文件、确认目录和服务配置稳定。脚本建立独立 Store，不导入缓存模型结果。

单次系列可先创建 `.folio/benchmarks/acceptance/ledger.json`，将 `SOURCE_JOB_ID` 替换为实际源任务 ID。不要覆盖已有账本。

```json
{
  "source_job": "SOURCE_JOB_ID",
  "max_full_runs": 1,
  "max_optimization_rounds": 1,
  "optimization_rounds": [{"round": 1, "status": "ready"}],
  "full_runs": []
}
```

```bash
uv run python scripts/replay_audited.py SOURCE_JOB_ID \
  --root .folio \
  --output-root .folio/benchmarks/acceptance \
  --timeout 1200 --generation-timeout 900 --parallel 16
```

默认使用源任务已确认的目录。添加 `--from-uploads` 会先重新执行资料准备、意图识别、目录规划和预览，再进入生成；该模式调用 GUI 的服务函数并自动确认，不验证浏览器交互，也不包含用户阅读等待时间。只有起点、输入和限制一致的运行才适合比较。

检查新运行目录中的 `result.json`、`timing.json` 及实际产物。`elapsed_seconds` 和 `generation_to_export_seconds` 的统计范围不同。脚本的 `passed` 包含内置的 420 秒目标和自动产物检查，不是通用性能承诺，也不证明业务质量；应另行记录实际结果、未决问题、`review_passed` 和人工检查结果。导出前失败不算快速完成交付。

## 离线审计与耗时

[`scripts/audit_document_run.py`](../scripts/audit_document_run.py) 只读取已保存记录，不调用服务，也不恢复任务：

```bash
uv run python scripts/audit_document_run.py \
  --root .folio \
  --job JOB_ID \
  --output .folio/audits/JOB_ID \
  --language zh-CN
```

审计回放时，将 `--root` 指向该轮的 `data` 目录，并使用新任务 ID。输出包含 `report.html`、`report.json` 及阶段耗时、研究、来源、审阅、修订和缓存复用的 CSV。报告可能包含源材料原文，应放在忽略提交的本地目录；分享前删除不应公开的内容。

| 指标 | 解释 |
| --- | --- |
| 阶段墙钟时间 | 不重叠的阶段可相加；重叠任务或请求的历时不能相加。未执行阶段应记为未执行，不能记为成功且耗时零秒。 |
| Engine 尝试与 `jobs.calls` | 逻辑动作及重试，不等于供应商 HTTP 数，也不覆盖全部前置准备。 |
| 已记录服务操作 | 按操作 ID 配对开始与完成，保留失败、取消及未结束请求；内部重试可能产生多次操作。 |
| 辅助请求与检索 | 模型元信息和分词容量请求与生成分开计数；MCP 工具调用数不包含全部传输握手与心跳。 |
| 缓存命中 | 复用结果不构成新服务调用。 |
| Token 用量 | 只累计服务实际返回的用量；缺失值保持未知，汇总值不等于完整账单。 |

动作历时可能包含本地排队。先检查重复读取、重试、输出截断、容量检查和任务槽等待，再判断是否模型推理缓慢或需要提高并发。缺失埋点不能事后补造为精确耗时或 HTTP 次数。诊断与恢复方法见[故障排查](troubleshooting.zh-CN.md)。
