# 文档

[English](README.md) | 简体中文

安装和首次使用请从[项目 README](../README.zh-CN.md)开始。

## 使用与运维

| 指南 | 范围 |
| --- | --- |
| [用户模板](templates.zh-CN.md) | DOCX 约束、风格参考与支持的结构 |
| [文档配图](illustrations.zh-CN.md) | 流程图、界面示意图与渲染器配置 |
| [补答与续跑](confirmation-and-resume.zh-CN.md) | 待确认项、补充答复与恢复 |
| [工作空间迁移](workspace-migration.zh-CN.md) | 备份、已存路径与部署变更 |
| [故障排查](troubleshooting.zh-CN.md) | 模型错误、性能、来源与文档排版 |
| [研究执行环境](../docker/research/README.zh-CN.md) | 隔离 Python、Office 与 OCR 执行 |

## 技术架构

| 设计 | 范围 |
| --- | --- |
| [整体架构](architecture.zh-CN.md) | 组件及端到端职责 |
| [模型交互](model-interactions.zh-CN.md) | LLM 与判断模型协议、路由和预算 |
| [目录规划](outline-planning.zh-CN.md) | 资料研究、目录审阅与用户确认 |
| [Agent 研究](agent-research.zh-CN.md) | 来源导航、研究记忆、工具和证据 |
| [任务执行](task-execution.zh-CN.md) | 业务任务、依赖调度和审阅 |
| [执行引擎](execution-engine.zh-CN.md) | 运行状态、检查点和交付 |
| [上下文与容量](context-and-capacity.zh-CN.md) | 请求构造、分批和上下文限制 |
| [资料与版式](source-materials-and-layout.zh-CN.md) | 文件用途、事实边界和 Word 排版 |
| [前端工作台](web-workspace.zh-CN.md) | 界面状态、会话和 API |

## 测试

- [测试指南](testing.zh-CN.md)：回归层次、可复现评估和验收标准。
- [模型图片能力检查](model-vision-checks.zh-CN.md)：图片输入及模型能力对照检查。
- [合成示例](../examples/acceptance_cases/README.zh-CN.md)：可复现资料与预期结果。
