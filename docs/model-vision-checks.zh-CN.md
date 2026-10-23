# 模型视觉能力检查

> English version: [English version](model-vision-checks.md)

`scripts/probe_jev_vision.py` 使用生成的四色方块图检查判断模型的图片输入和结构化回答能力。它是独立工具，不修改工作台设置、文档任务或任务调用计数；测试调用需要单独计入费用。

## 运行检查

在项目根目录运行，默认读取 `.folio/settings/model.json` 中的判断模型配置：

```bash
.venv/bin/python scripts/probe_jev_vision.py
```

默认运行 `image_original`，发送 **1 次 HTTP 请求**，一次回答四道颜色位置题。所有请求均为非流式，不自动重试，也不使用本地响应缓存。终端打印结果目录、状态、正确题数和耗时。

只生成输入和请求、不联网：

```bash
.venv/bin/python scripts/probe_jev_vision.py --case all --prepare-only
```

`--prepare-only` 仍需要接口地址，但不需要 API Key。没有工作台配置时，可以提供占位地址检查请求格式：

```bash
.venv/bin/python scripts/probe_jev_vision.py \
  --endpoint https://example.invalid/v1/chat/completions \
  --model example-model --case all --prepare-only
```

## 配置和接口协议

脚本读取 `decision_base_url`、`decision_api_key` 和 `decision_model`。常规 Base URL 后会追加 `/chat/completions`；以 `/systemone` 结尾的地址直接使用原生协议。

| 参数 | 用途 |
| --- | --- |
| `--config PATH` | 指定配置文件，默认 `.folio/settings/model.json` |
| `--endpoint URL` | 覆盖完整 POST 地址，不再追加路径 |
| `--model MODEL` | 覆盖判断模型名 |
| `--key-env NAME` | 使用指定环境变量覆盖配置中的密钥，默认 `JEV_TEST_API_KEY` |
| `--timeout SECONDS` | HTTP 超时，默认 900 秒，必须大于 0 |
| `--output PATH` | 指定新的结果目录；目录已存在时拒绝覆盖 |
| `--language en\|zh-CN` | 终端语言，默认 `en` |

OpenAI 兼容接口通过用户消息中的 `image_url` data URL 接收图片，必须返回一次 `submit_decisions` 工具调用。每道选择题的答案包含 `type`、`choice`、`confidence` 和所有选项的 `probabilities`，概率总和须接近 1。支持普通文本聊天并不保证支持这种图片与工具调用组合。

对于路径以 `/systemone` 结尾的接口，脚本发送原生 `state`、`questions` 和 `assets`，直接解析 `answers`；图片请求还带有 `TypeSafe-Danger-Zone-Images: true` 请求头。请求结构由共享的[判断客户端](../src/folio/decision_client.py)生成。

## 对照案例

```bash
.venv/bin/python scripts/probe_jev_vision.py --case all
```

`all` 顺序运行以下四个案例，共 **4 次请求**。也可以单独选择，例如 `--case image_explicit`。

| 案例 | 输入与问题 | 检查内容 |
| --- | --- | --- |
| `image_original` | 320×320 四色方块图，询问各颜色所在象限 | 基本图片输入和位置识别 |
| `image_explicit` | 同一张图，问题明确引用 `image` 字段 | 提问中的图片引用是否影响结果 |
| `image_swapped` | 保持问题和资源编号不变，交换图片中方块的位置 | 回答是否随图片内容改变 |
| `text_control` | 仅提供颜色与位置的文字对应关系，不发图片 | 接口与选择题协议是否正常 |

图片案例的标准答案只保存在本地 `expected.json`，不随请求发送。纯文本对照会在请求中给出对应关系，因此其通过不能证明图片识别能力。

## 双向提问

```bash
.venv/bin/python scripts/probe_jev_vision.py --case image_bidirectional
```

对同一张图顺序发送 **2 次请求**：

1. `color_positions`：问每种颜色在哪个象限。
2. `position_colors`：问每个象限是什么颜色。

两次请求都明确引用 `image` 字段，每组四题分别核对。第二次请求不携带第一次的答案，两个方向的标准答案都只保存在本地。该组合不交换图片，也不能与 `--rotate-options` 同用。

## 轮换选项顺序

```bash
.venv/bin/python scripts/probe_jev_vision.py --rotate-options
```

对同一个案例发送 **4 次请求**，只轮换各题 `criteria` 的排列顺序；图片、问题、资源编号、模型和选项文字保持一致。四轮分别以四个象限选项开头，保存及发送的 JSON 保留选项顺序。

默认输出子目录为 `image_original_order_0` 至 `image_original_order_3`。`result.json` 记录完整 `option_order`、轮换编号 `option_offset` 和选择首项的题数 `first_option_answers`，正确题数仍按图片内容计算。

可以搭配单个案例，例如 `--case image_explicit` 或 `--case text_control`；不能搭配 `all` 或 `image_bidirectional`。加 `--prepare-only` 可离线查看四份请求。轮换用于观察选项顺序是否影响答案，不能单独证明接口正确处理了图片。

## 结果文件

每次运行新建 `.folio/operations/jev-vision-local/<北京时间>-<编号>/`，或使用 `--output` 指定的目录。每个案例子目录包含：

| 文件 | 内容 |
| --- | --- |
| `input.png` | 实际发送的图片；纯文本案例没有此文件 |
| `request.json` | 实际发送的完整请求正文，包含图片 Base64 |
| `request-meta.json` | 接口地址和请求头；Authorization 密钥被替换为占位符 |
| `expected.json` | 标准答案 |
| `response.txt` | 服务器返回的原始正文，包含错误响应 |
| `response.json` | 响应可解析为 JSON 时保存的易读版本 |
| `response-meta.json` | HTTP 状态、响应类型和请求编号 |
| `result.json` | 状态、正确题数、实际答案、置信度、选项顺序和耗时等结果 |

根目录的 `summary.json` 汇总全部案例。仅准备文件时不产生响应文件；连接失败或超时时，错误类型记录在 `result.json`。

## 理解检查结果

| 状态 | 含义 |
| --- | --- |
| `prepared` | 已准备文件，未发送请求 |
| `passed` | 响应符合协议，四道题全部正确 |
| `wrong_answers` | 响应符合协议，但答案未全部正确 |
| `invalid_response` | HTTP 200，但返回不满足预期的结构化回答协议 |
| `http_error` | 收到非 HTTP 200 响应 |
| `network_error` | 连接、超时或其他 HTTP 客户端错误 |

HTTP 200 本身不代表检查通过。如果文字案例通过而图片案例失败，只能说明当前配置下的图片调用未通过这些检查。结合保存的输入、请求和响应判断原因；一次失败不足以断定模型不支持图片，简单色块题通过也不能证明复杂文档理解能力。

退出码：`0` 表示所选案例全部通过或只准备文件；`1` 表示至少一个案例未通过；`2` 表示命令参数或配置校验失败。
