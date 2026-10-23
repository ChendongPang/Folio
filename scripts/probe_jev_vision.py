"""Standalone OpenAI-compatible decision image diagnostics: one HTTP request per case, no job/database writes."""

import argparse
import base64
import io
import json
import os
from datetime import datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from PIL import Image, ImageDraw

from folio.decision_client import _valid_answers, decision_target
from folio.model_settings import load_model_config
from folio.runtime.language import localize_text

ROOT = Path(__file__).resolve().parents[1]
CASES = ("image_original", "image_explicit", "image_swapped", "text_control")
BIDIRECTIONAL_CASES = ("color_positions", "position_colors")
COLORS = ("red", "green", "blue", "yellow")
POSITIONS = ("top_left", "top_right", "bottom_left", "bottom_right")
IMAGE_ID = "7d419e4522ee41469dd0dc78bbff5ef9"


def build_case(case, model, *, option_offset=0):
    # Reconstruct the spatial order from the earlier saved diagnostic, not a
    # purported byte-for-byte replay of its unsaved, randomly generated request.
    ordered = ("red", "yellow", "green", "blue")
    if case == "image_swapped":
        ordered = ("blue", "green", "yellow", "red")
    expected = {color: position for color, position in zip(ordered, POSITIONS, strict=True)}
    option_order = POSITIONS[option_offset:] + POSITIONS[:option_offset]
    questions = {
        color: {
            "type": "choice",
            "instructions": f"Which quadrant of the image contains the {color} square?",
            "criteria": {position: position.replace("_", " ") for position in option_order},
        }
        for color in COLORS
    }
    if case == "color_positions":
        questions = {
            color: {
                "type": "choice",
                "instructions": f"In `image`, where is the {color} square?",
                "criteria": {position: position.replace("_", " ") for position in POSITIONS},
            }
            for color in ordered
        }
    elif case == "position_colors":
        expected = {position: color for color, position in zip(ordered, POSITIONS, strict=True)}
        questions = {
            position: {
                "type": "choice",
                "instructions": f"In `image`, what color is the square at the {position.replace('_', ' ')}?",
                "criteria": {color: color for color in ordered},
            }
            for position in POSITIONS
        }
    payload = {"model": model, "state": {"image": IMAGE_ID}, "questions": questions}
    if case == "text_control":
        payload["state"] = {"positions": expected}
        for color in COLORS:
            questions[color]["instructions"] = (
                f"According to the text in `positions`, which quadrant contains the {color} square?"
            )
        return payload, expected, None
    if case == "image_explicit":
        for color in COLORS:
            questions[color]["instructions"] = (
                f"Look at the image referenced by `image`. Which quadrant contains the {color} square?"
            )
    picture = Image.new("RGB", (320, 320), "white")
    draw = ImageDraw.Draw(picture)
    for index, color in enumerate(ordered):
        left, top = index % 2 * 160, index // 2 * 160
        draw.rectangle((left + 10, top + 10, left + 150, top + 150), fill=color)
    output = io.BytesIO()
    picture.save(output, format="PNG")
    png = output.getvalue()
    payload["assets"] = {
        IMAGE_ID: {"type": "image", "format": "png", "data_base64": base64.b64encode(png).decode()}
    }
    return payload, expected, png


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def run_case(client, endpoint, model, key, case, folder, *, prepare_only=False, option_offset=0, language="en"):
    def tr(text):
        return localize_text(text, language)

    folder.mkdir(mode=0o700)
    payload, expected, png = build_case(case, model, option_offset=option_offset)
    questions = payload["questions"]
    question_names = tuple(payload["questions"])
    option_order = list(payload["questions"][question_names[0]]["criteria"])
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if png:
        (folder / "input.png").write_bytes(png)
    from folio.decision_client import decision_payload, is_systemone_endpoint

    native = is_systemone_endpoint(endpoint)
    payload = decision_payload(model, payload["state"], questions, payload.get("assets"), native=native)
    if native and payload.get("assets"):
        headers["TypeSafe-Danger-Zone-Images"] = "true"
    # Send exactly the UTF-8 bytes saved here, so request.json can also be replayed with curl.
    save_json(folder / "request.json", payload)
    save_json(folder / "expected.json", expected)
    save_json(folder / "request-meta.json", {
        "method": "POST", "url": endpoint,
        "headers": {k: "Bearer <not stored>" if k == "Authorization" else v for k, v in headers.items()},
    })
    result = {
        "case": case, "http_requests": 0, "expected": expected, "status": "prepared",
        "option_offset": option_offset, "option_order": option_order,
    }
    if prepare_only:
        save_json(folder / "result.json", result)
        return result
    label = f"{case} | 首项 {option_order[0]}"
    print(tr(f"[{label}] 正在发送 1 次请求，等待完整 JSON 返回……"), flush=True)
    started = monotonic()
    result["http_requests"] = 1
    try:
        response = client.post(endpoint, content=(folder / "request.json").read_bytes(), headers=headers)
        (folder / "response.txt").write_bytes(response.content)
        (folder / "response.txt").chmod(0o600)
        save_json(folder / "response-meta.json", {
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type"),
            "request_id": response.headers.get("x-request-id"),
        })
        result["http_status"] = response.status_code
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if parsed is not None:
            save_json(folder / "response.json", parsed)
        if not native:
            try:
                calls = parsed["choices"][0]["message"]["tool_calls"]
                parsed = json.loads(calls[0]["function"]["arguments"]) if len(calls) == 1 and calls[0]["function"]["name"] == "submit_decisions" else None
            except (TypeError, KeyError, IndexError, ValueError):
                parsed = None
        valid = _valid_answers(parsed, questions)
        if response.status_code != 200:
            result["status"] = "http_error"
        elif not valid:
            result["status"] = "invalid_response"
        else:
            answers = parsed["answers"]
            actual = {name: answers[name]["choice"] for name in question_names}
            correct = sum(actual[name] == expected[name] for name in question_names)
            result.update(
                status="passed" if correct == len(question_names) else "wrong_answers",
                correct=correct, total=len(question_names), actual=actual,
                confidence={name: answers[name]["confidence"] for name in question_names},
                model=parsed.get("model", model), usage=parsed.get("usage", {}),
                first_option_answers=sum(choice == option_order[0] for choice in actual.values()),
            )
    except httpx.HTTPError as exc:
        # Keep arbitrary exception text (which may contain credentials) off the console.
        result.update(status="network_error", error_type=type(exc).__name__)
    result["elapsed_seconds"] = round(monotonic() - started, 3)
    save_json(folder / "result.json", result)
    print(tr(f"[{label}] {result['status']}；正确 {result.get('correct', '—')}/4；选择首项 {result.get('first_option_answers', '—')}/4；{result['elapsed_seconds']} 秒"), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=(*CASES, "all", "image_bidirectional"), default="image_original")
    parser.add_argument("--rotate-options", action="store_true", help="Run one case four times, rotating only choice order")
    parser.add_argument("--config", type=Path, default=ROOT / ".folio/settings/model.json")
    parser.add_argument("--endpoint", help="Override the exact POST URL; no path is appended")
    parser.add_argument("--model", help="Override the configured OpenAI-compatible decision model ID")
    parser.add_argument("--key-env", default="JEV_TEST_API_KEY", help="Environment variable overriding the saved key")
    parser.add_argument("--timeout", type=float, default=900, help="HTTP timeout in seconds")
    parser.add_argument("--output", type=Path, help="New output directory (must not already exist)")
    parser.add_argument("--prepare-only", action="store_true", help="Save inputs without making any HTTP requests")
    parser.add_argument("--language", choices=("en", "zh-CN"), default="en", help="Console language")
    args = parser.parse_args()

    def tr(text):
        return localize_text(text, args.language)

    if args.rotate_options and args.case in {"all", "image_bidirectional"}:
        parser.error(tr("--rotate-options 仅用于单个案例，不可与 all 或 image_bidirectional 同用。"))
    config = load_model_config(args.config) if args.config.is_file() else {}
    endpoint, model, key = decision_target(config)
    endpoint, model, key = args.endpoint or endpoint, args.model or model, os.environ.get(args.key_env, key)
    if not endpoint:
        parser.error(tr("缺少接口地址：请配置工作台或使用 --endpoint。"))
    if not key and not args.prepare_only:
        parser.error(tr(f"缺少 API Key：请配置工作台或设置环境变量 {args.key_env}。"))
    if args.timeout <= 0:
        parser.error(tr("--timeout 必须大于 0。"))
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    output = args.output or ROOT / ".folio/operations/jev-vision-local" / (stamp + "-" + uuid4().hex[:6])
    if output.exists():
        parser.error(tr("输出目录已存在，请换一个目录以保留之前的结果。"))
    output.mkdir(parents=True, mode=0o700)
    selected = (
        BIDIRECTIONAL_CASES if args.case == "image_bidirectional"
        else CASES if args.case == "all"
        else (args.case,)
    )
    print(tr(f"结果目录：{output.resolve()}"), flush=True)
    with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
        results = [
            run_case(
                client, endpoint, model, key, case,
                output / (f"{case}_order_{offset}" if args.rotate_options else case),
                prepare_only=args.prepare_only, option_offset=offset, language=args.language,
            )
            for case in selected
            for offset in (range(4) if args.rotate_options else (0,))
        ]
    save_json(output / "summary.json", results)
    print(tr("仅准备文件，未联网。" if args.prepare_only else f"共发送 {len(results)} 次请求，无自动重试、无本地缓存。"), flush=True)
    return 0 if all(r["status"] in {"prepared", "passed"} for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
