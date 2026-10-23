# Model Vision Capability Checks

> 中文版: [中文版](model-vision-checks.zh-CN.md)

`scripts/probe_jev_vision.py` uses generated four-color block images to check a decision model's image input and structured responses. It is a standalone tool: it does not modify workbench settings, document jobs, or job call counts. Account for these test calls separately when tracking costs.

## Run a Check

Run from the project root. By default, the script reads the decision model configuration from `.folio/settings/model.json`:

```bash
.venv/bin/python scripts/probe_jev_vision.py
```

The default case, `image_original`, sends **1 HTTP request** containing four color-position questions. Requests are non-streaming, with no automatic retries or local response cache. The terminal prints the output directory, status, correct-answer count, and elapsed time.

To prepare inputs and requests without network access:

```bash
.venv/bin/python scripts/probe_jev_vision.py --case all --prepare-only
```

`--prepare-only` still requires an endpoint, but does not require an API Key. Without workbench configuration, supply a placeholder endpoint to inspect the request format:

```bash
.venv/bin/python scripts/probe_jev_vision.py \
  --endpoint https://example.invalid/v1/chat/completions \
  --model example-model --case all --prepare-only
```

## Configuration and Protocols

The script reads `decision_base_url`, `decision_api_key`, and `decision_model`. It appends `/chat/completions` to a regular Base URL. An endpoint ending in `/systemone` is used directly with the native protocol.

| Option | Purpose |
| --- | --- |
| `--config PATH` | Configuration file; defaults to `.folio/settings/model.json` |
| `--endpoint URL` | Override the complete POST URL; no path is appended |
| `--model MODEL` | Override the decision model name |
| `--key-env NAME` | Override the configured key with this environment variable; defaults to `JEV_TEST_API_KEY` |
| `--timeout SECONDS` | HTTP timeout; defaults to 900 seconds and must be positive |
| `--output PATH` | New output directory; an existing directory is never overwritten |
| `--language en\|zh-CN` | Console language; defaults to `en` |

An OpenAI-compatible endpoint receives images as `image_url` data URLs in the user message and must return one `submit_decisions` tool call. Each multiple-choice answer includes `type`, `choice`, `confidence`, and `probabilities` for all options, summing approximately to 1. Support for ordinary text chat does not establish support for this combination of images and tool calls.

For endpoints whose path ends in `/systemone`, the script sends native `state`, `questions`, and `assets`, and reads `answers` directly. Image requests also include the `TypeSafe-Danger-Zone-Images: true` header. Requests are built by the shared [decision client](../src/folio/decision_client.py).

## Control Cases

```bash
.venv/bin/python scripts/probe_jev_vision.py --case all
```

`all` runs these four cases sequentially, making **4 requests**. Cases can also be selected individually, for example with `--case image_explicit`.

| Case | Input and questions | What it checks |
| --- | --- | --- |
| `image_original` | A 320×320 four-color block image; asks which quadrant contains each color | Basic image input and position recognition |
| `image_explicit` | The same image, with questions explicitly referencing the `image` field | Whether explicit image references affect the result |
| `image_swapped` | Keeps questions and the resource ID unchanged while swapping block positions | Whether answers change with image content |
| `text_control` | Only a textual mapping of colors to positions, without an image | Whether the endpoint and multiple-choice protocol work |

Expected answers for image cases are saved only in local `expected.json` files, not sent with the request. The text control deliberately includes the mapping in the request, so passing it does not establish image-recognition capability.

## Questions in Both Directions

```bash
.venv/bin/python scripts/probe_jev_vision.py --case image_bidirectional
```

Sends **2 requests** sequentially using the same image:

1. `color_positions`: asks which quadrant contains each color.
2. `position_colors`: asks which color appears in each quadrant.

Both requests explicitly reference the `image` field, and each group of four questions is checked separately. The second request does not include the first response, and expected answers for both groups remain local. This combination does not swap the image and cannot be used with `--rotate-options`.

## Rotate Option Order

```bash
.venv/bin/python scripts/probe_jev_vision.py --rotate-options
```

Sends **4 requests** for one case, rotating only the order of each question's `criteria`. The image, questions, resource ID, model, and option text remain unchanged. Each quadrant option appears first in one round, and the saved and transmitted JSON preserves this order.

Default output subdirectories are `image_original_order_0` through `image_original_order_3`. `result.json` records the complete `option_order`, rotation index `option_offset`, and number of answers selecting the first option in `first_option_answers`. Correct-answer counts still use the image content.

Use this with a single case, such as `--case image_explicit` or `--case text_control`; it cannot be combined with `all` or `image_bidirectional`. Add `--prepare-only` to inspect all four requests offline. Rotation helps reveal option-order effects, but cannot establish by itself whether the endpoint processed an image correctly.

## Output Files

Each run creates `.folio/operations/jev-vision-local/<Beijing time>-<ID>/`, or the directory supplied with `--output`. Each case subdirectory contains:

| File | Content |
| --- | --- |
| `input.png` | The image actually sent; absent for text-only cases |
| `request.json` | The complete request body actually sent, including image Base64 |
| `request-meta.json` | Endpoint and headers; the Authorization key is replaced with a placeholder |
| `expected.json` | Expected answers |
| `response.txt` | Raw server response body, including error responses |
| `response.json` | Readable response when its body can be parsed as JSON |
| `response-meta.json` | HTTP status, content type, and request ID |
| `result.json` | Status, correct-answer count, actual answers, confidence, option order, elapsed time, and related results |

`summary.json` in the run directory aggregates all cases. Preparation-only runs do not create response files. Connection failures and timeouts record the error type in `result.json`.

## Interpret Results

| Status | Meaning |
| --- | --- |
| `prepared` | Files prepared; no request sent |
| `passed` | Valid response with all four answers correct |
| `wrong_answers` | Valid response with at least one incorrect answer |
| `invalid_response` | HTTP 200, but the response does not meet the structured-answer protocol |
| `http_error` | A non-200 HTTP response |
| `network_error` | Connection, timeout, or another HTTP client error |

HTTP 200 alone does not mean the check passed. If text cases pass while image cases fail, the configured image requests have not passed these checks. Inspect saved inputs, requests, and responses to understand why. One failure does not establish that a model lacks image support, and passing simple color-block questions does not establish complex document-understanding capability.

Exit codes: `0` means all selected cases passed or files were only prepared; `1` means at least one case failed; `2` means command-line arguments or configuration failed validation.
