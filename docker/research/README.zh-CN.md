# 研究执行镜像

> English version: [English version](README.md)

在仓库根目录构建：

```sh
docker build --target base -t folio-research-base:v2 -f docker/research/Dockerfile .
docker build --target data -t folio-research-data:v2 -f docker/research/Dockerfile .
docker build --target office -t folio-research-office:v2 -f docker/research/Dockerfile .
docker build --target ocr -t folio-research-ocr:v2 -f docker/research/Dockerfile .
```

执行器不会自动拉取镜像，也不会回退到宿主机执行模型生成的代码。运维人员可通过 `profile_images` 将四个固定环境映射到经过审核的镜像。每次执行前，镜像标签都会解析为不可变的镜像 ID。

用户代码在无网络、只读根文件系统和非 root 用户下运行，移除 Linux capabilities，启用 `no-new-privileges`，并限制 CPU、内存、进程数、执行时间、日志和产物大小。只有任务临时目录和本次产物目录可写；源文件、上下文和批准的依赖均为只读，不挂载 Docker socket。

依赖请求只能指定规范化包名和精确版本。运维允许清单须包含 wheel 的 SHA-256 及所有传递依赖，默认仅允许 `networkx==3.4.2`。安装器从配置的 HTTPS `trusted_index_url` 下载二进制 wheel，校验哈希后通过 `--no-index --no-deps` 安装。用户 Python 不进入可联网的安装器。缓存键包含镜像摘要和完整依赖锁；任意 URL、索引、Git 仓库、本地路径、源码包及未批准版本均拒绝。

持久状态保存在 `task_root/.research`，不挂载到执行容器。原件位于 `/sources`，上下文位于 `/context`，临时文件位于 `/workspace/scratch`，新产物位于 `/workspace/artifacts`（环境变量 `FOLIO_OUTPUT_DIR`、`FOLIO_ARTIFACTS`）。需要登记的文件应写入产物目录。成功结果记录输出哈希。已完成并验证的旧产物以只读方式挂载到 `$FOLIO_PREVIOUS_ARTIFACTS/<execution_id>`；其哈希进入 `input_artifacts`，上游内容变化会使下游来源证明失效。

OCR 和 Office 转换采用相同隔离机制，记录源文件哈希和镜像摘要。固定操作包括 `ocr`、`office_pdf`、`office_recalculate`、`office_ocr`；最后一种在一次执行中完成 Office 转 PDF 和逐页 OCR。OCR 结果包含实际页数、位置及关键数字须核对原件的提示。整份 OCR 上限为 100 页或帧；指定 `pages` 时每次最多选择 20 个原始页码，长文档可渐进读取而不重新编号。`office_ocr` 还保存 `source-preview.pdf`，后续查看前验证转换脚本及产物哈希。LibreOffice 重算保留兼容性提示。

运维配置为 `<data-dir>/settings/research.json` 中的平铺 JSON 对象，也可通过 `FOLIO_RESEARCH_CONFIG` 指定文件。例如：

```json
{
  "max_concurrent": 2,
  "memory_mb": 512,
  "pids_limit": 64,
  "profile_images": {
    "base": "folio-research-base:v2",
    "data": "folio-research-data:v2",
    "office": "folio-research-office:v2",
    "ocr": "folio-research-ocr:v2"
  }
}
```

下表为独立执行器默认值，实际还受作业 `ResearchConfig` 约束；业务研究默认每次执行最多 120 秒，累计最多 600 秒。

| 配置项 | 默认值 |
| --- | --- |
| `profile_images` | 上述四个本地镜像标签 |
| `docker_binary` | `docker` |
| `max_concurrent` | 每个进程事件循环最多 2 次并发执行 |
| `cpu_limit`, `memory_mb`, `pids_limit` | 1 CPU、512 MiB、64 个进程 |
| `timeout_seconds`, `max_timeout_seconds` | 60、300 秒 |
| `max_output_bytes` | stdout 与 stderr 合计 128 KiB |
| `max_artifact_bytes`, `max_total_artifact_bytes` | 单文件 32 MiB，总计 128 MiB |
| `max_artifacts`, `max_scratch_bytes` | 100 个文件，临时空间 256 MiB |
| `trusted_index_url`, `dependency_network` | `https://pypi.org/simple`, `bridge` （仅安装器） |
| `dependency_cache_root` | 任务内 `.research/dependency_cache` |
| `dependency_allowlist` | 默认 NetworkX 精确 wheel 锁 |

允许清单条目格式为 `{"package-name":{"1.2.3":{"sha256":["<64 位小写十六进制字符>"],"dependencies":{"another-package":"4.5.6"}}}}`。每个传递依赖都须有独立的精确版本及 wheel 哈希。可联网安装器不接受模型指定索引，只运行固定下载和安装脚本。执行容器始终使用 `--network none`。

构建镜像后运行隔离和转换检查：

```sh
.venv/bin/python -m pytest -q tests/test_research_sandbox.py
FOLIO_TEST_DEPENDENCY_INSTALL=1 .venv/bin/python -m pytest -q tests/test_research_sandbox.py
```

第二条命令还会访问可信包索引，检查依赖安装及缓存复用。未安装的镜像环境会被跳过；跳过不能证明该机器上的隔离或转换检查已通过。

原始页面查看使用宿主机 Poppler（`pdftoppm`）进行有界 PDF 渲染，使用 Pillow 处理图片。研究模型须显式启用 `supports_image_input` 才能执行 `fs.view`、独立 OCR 核验及视觉提取。核验绑定原文件、页码、文本及渲染图哈希；OCR 成功本身不代表内容已经验证。
