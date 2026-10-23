# Folio 工作空间迁移

> English version: [English version](workspace-migration.md)

Folio 的 Python 包和命令名统一为 `folio`，默认工作空间为 `.folio`，主数据库为 `folio.sqlite3`。项目目录或数据目录变化后，数据库中的绝对路径不会自行更新。[`scripts/relocate_workspace.py`](../scripts/relocate_workspace.py) 用于离线迁移已知的活跃路径，并将指定的主数据库文件改名为 `folio.sqlite3`。

脚本默认 **dry-run，只报告计划变更**。它不移动项目目录，不启动服务，也不调用模型。历史对话仍保存在数据库中，浏览器存储键改名则属于独立的界面状态变化。

## 迁移范围

脚本只替换与原项目目录或原数据目录完全匹配的路径及其子路径，优先匹配较长的目录前缀。更新范围为：

- 主数据库中作业的资料路径、参考和风格文件路径、模板路径、产物路径，以及已保存业务任务的研究目录和笔记路径。
- 上传记录的文件路径、研究账本 `research_sessions` 中的 `workspace_root`。
- `jobs/*/work-records.json` 的研究路径，以及 `directory-drafts/*/inspection.json` 的文档和 PDF 路径。

上传原件、资料结构索引、已签名产物、历史模型请求、事件和审计日志不做全局字符串替换。历史记录里出现原路径是保留当时执行事实，不意味着其中所有路径都可作为迁移后的入口使用。

**发现 `artifact_records/*.json` 时，脚本会直接拒绝，包括 dry-run。** 这些派生证据有内容与来源链签名，需要专门的验签、路径迁移和重新签名流程；当前脚本没有实现该流程。不能删除签名记录、篡改原件或重算来源哈希来绕过限制。仅复制签名密钥也不足以使修改后的记录继续有效。

## 停服、备份和移动目录

1. 先在界面暂停正在运行的任务，等待状态变为“已暂停”，再停止使用该工作空间的所有服务和 Worker。脚本检查到并发修改会拒绝，但它不负责自动停服。
2. 将**整个数据目录**复制到项目之外的备份位置，保留上传原件、作业、数据库及其伴随文件、模型配置和研究配置。另行备份宿主签名密钥。不要只复制主数据库文件。
3. 记录移动前的项目绝对路径、数据绝对路径及数据库文件名，再移动项目和数据目录。下面的路径只是示例，应换成实际位置；若数据位于项目内，移动项目后还需按需调整数据子目录名。
4. 在移动后的目录运行 dry-run，核对报告后才使用 `--apply`。若脚本拒绝，保持服务停止并检查原因；迁移未完成前不要在新位置启动一个空工作空间。

```bash
# 填写移动前后的真实路径；不会通过这些变量自动执行目录移动。
original_root='/srv/projects/original-project'
folio_root='/srv/projects/folio'
original_data='/srv/projects/original-project/.workspace'
folio_data='/srv/projects/folio/.folio'
original_database='workspace.sqlite3'

cd "$folio_root"
python3 scripts/relocate_workspace.py \
  --data-dir "$folio_data" \
  --from-root "$original_root" --to-root "$folio_root" \
  --from-data "$original_data" --to-data "$folio_data" \
  --database-name "$original_database"
```

上述命令使用 Python 标准库，不依赖移动后的虚拟环境是否已经修复。`--data-dir` 必须是当前实际存在的数据目录。脚本不会根据 `--from-root` 或 `--from-data` 去复制原资料。

确认报告中的变更数据库、记录数量和 JSON 文件符合预期后，再执行相同参数并加上：

```bash
python3 scripts/relocate_workspace.py \
  --data-dir "$folio_data" \
  --from-root "$original_root" --to-root "$folio_root" \
  --from-data "$original_data" --to-data "$folio_data" \
  --database-name "$original_database" \
  --backup-dir '/srv/backups/folio-active-state-migration-01' \
  --apply
```

`--backup-dir` 必须是**尚不存在的新目录**，并且位于当前、原始、目标数据目录之外。脚本在写入前使用 SQLite 备份接口备份会读取或修改的数据库，包含已提交的 WAL 内容，并备份将修改的 JSON 文件；这份活跃状态备份不能替代第二步的完整数据备份。备份目录权限为 `0700`，数据库、JSON 备份及迁移报告权限为 `0600`。

写入使用逐数据库事务、原值核对和 JSON 临时文件替换；后续写入失败时会尝试从备份恢复活跃状态。主数据库在成功写入和 WAL checkpoint 后才改名。若原数据库与目标 `folio.sqlite3` 同时存在，脚本拒绝覆盖，需先确认它们分别属于哪个工作空间。

## 参数和报告

| 参数 | 含义 |
| --- | --- |
| `--data-dir` | 当前实际存在、需要迁移的数据目录，必填 |
| `--from-root` / `--to-root` | 原项目目录与新项目目录，均必填 |
| `--from-data` / `--to-data` | 原数据目录与新数据目录，均必填；数据路径匹配优先于项目路径 |
| `--database-name` | 当前主数据库文件名，不接受路径；默认 `folio.sqlite3` |
| `--backup-dir` | 需要写入变更时必须提供的新建外部备份目录 |
| `--apply` | 实际写入；省略则仅预览 |

JSON 报告包含 `mode`、目标 `database`、是否改名、各数据库变更数、待修改 JSON 文件和 `changed`。`historical_audits_and_sources_changed=false` 表示脚本没有重写历史审计与原资料，不代表所有旧任务都已通过续跑验收。

成功后可再次用同样参数 dry-run。原文件已改名时脚本会读取 `folio.sqlite3`；路径已迁移完毕应返回 `changed=false`。只读预览和重复检查不重置任务预算或模型调用次数。

## 宿主环境也要更新

迁移脚本不修改环境变量、服务配置、浏览器存储、Office 安装或 Docker 镜像。启动新服务前逐项确认实际路径：

| 配置 | 当前名称和位置 |
| --- | --- |
| 主模型配置 | 数据目录下 `settings/model.json`，或 `FOLIO_MODEL_CONFIG` 指向的文件 |
| 模型环境变量示例 | `FOLIO_MODEL_BASE_URL`、`FOLIO_MODEL`、`FOLIO_MODEL_API_KEY`；配置文件中的变量引用必须对应新名称 |
| 研究配置 | 数据目录下 `settings/research.json`，或 `FOLIO_RESEARCH_CONFIG` |
| Office | `FOLIO_OFFICE_PYTHON` 与 `FOLIO_SOFFICE` 必须成对配置，分别指向可导入 `uno` 的 Python 和 LibreOffice 可执行文件 |
| 本机 Office 配置 | 未显式配置时会检查 `~/.local/share/folio/office/runtime.json`，其内部路径也必须存在；之后才尝试系统安装 |
| 派生证据签名密钥 | 默认 `~/.local/state/folio/artifact-signing.key`，可用 `FOLIO_ARTIFACT_KEY_PATH` 指定既有密钥文件 |
| Chromium 与 Mermaid | 自定义路径分别使用 `FOLIO_CHROMIUM_EXECUTABLE`、`FOLIO_MERMAID_JS` |

签名密钥是 32 字节的独立宿主文件，须保留原字节和限制性权限；不要提交到代码仓库，也不要让新安装生成的另一把密钥替代旧证据所用密钥。现有签名记录仍受上一节的迁移限制。

如果复制本机 Office 安装目录，不能只更新 `runtime.json`：`fundamentalrc`、`unorc`、`sofficerc` 和绝对符号链接可能仍指向原安装路径。应核对并更新这些定位配置，保留其他服务正在使用的原安装，再运行 `uv run pytest tests/test_pagination.py -q` 验证实际分页；新旧 UNO 组件混用可能导致 LibreOffice 无法启动。

研究镜像默认使用 `folio-research-base:v2`、`folio-research-data:v2`、`folio-research-office:v2` 和 `folio-research-ocr:v2`。按[研究环境部署说明](../docker/research/README.zh-CN.md)构建，或在 `profile_images` 中显式配置本机已有且经核验的镜像。程序不会自动拉取，也不会因为找不到镜像而在宿主执行模型代码。镜像内容变化会改变不可变镜像摘要，不能承诺继续复用依赖原摘要的派生证据。

## 启动后核对

目录移动后，旧虚拟环境可能仍含原路径的启动脚本和可编辑安装记录。将原环境另行归档后，在新项目根目录重新执行：

```bash
uv sync --locked
npm --prefix frontend ci
npm --prefix frontend run build
uv run folio --data-dir "$folio_data" serve --port 8765
```

打开 `http://127.0.0.1:8765/`，先核对历史会话、附件和既有文档下载，再逐个检查需要继续的任务。移除文档类型选择器不会清除后台保存的写作指南；新会话默认没有额外指南。

浏览器使用 `folio.` 前缀保存会话入口、未同步草稿和面板尺寸。脚本不迁移这些浏览器数据；已同步消息仍应从服务端历史列表恢复，尚未同步的本机草稿应在移动前另存。资料版本、签名、运行环境或检查点不匹配的任务仍会拒绝续跑，路径更新成功不能替代这些核验。

如需回退，先停服，保留失败现场，再用完整备份恢复原目录及匹配的运行环境；不要同时在两处启动服务写入同一工作空间。
