"""Offline tests of the standalone diagnostic, with no workbench or provider access."""

import base64
import importlib.util
import io
import json
import sys
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from decision_wire import payload as decision_payload
from PIL import Image


def probe_payload(request):
    data = decision_payload(request)
    return {key: data[key] for key in ("model", "state", "questions", "assets") if key in data}


def wire_result(result):
    return {"choices": [{"message": {"tool_calls": [{"function": {
        "name": "submit_decisions", "arguments": json.dumps({"answers": result["answers"]})
    }}]}}], "model": result.get("model", "test"), "usage": result.get("usage", {})}


@pytest.fixture
def probe(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/probe_jev_vision.py"
    spec = importlib.util.spec_from_file_location("standalone_jev_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden_store(*args, **kwargs):
        pytest.fail("The standalone probe must not create a Store or touch document jobs")

    monkeypatch.setattr("folio.store.Store.__init__", forbidden_store)
    return module


def answer_body(probe, expected):
    return wire_result({
        "answers": {
            color: {
                "type": "choice",
                "choice": position,
                "confidence": 0.97,
                "probabilities": {name: float(name == position) for name in probe.POSITIONS},
            }
            for color, position in expected.items()
        },
        "model": "jev-test-version",
        "usage": {"input_tokens": 400, "output_tokens": 4},
    })

def test_swapped_image_changes_only_asset_bytes_and_keeps_answers_out_of_text(probe):
    original, expected, png = probe.build_case("image_original", "jev")
    swapped, swapped_expected, swapped_png = probe.build_case("image_swapped", "jev")
    assert expected != swapped_expected and png != swapped_png
    first, second = deepcopy(original), deepcopy(swapped)
    first["assets"][probe.IMAGE_ID].pop("data_base64")
    second["assets"][probe.IMAGE_ID].pop("data_base64")
    assert first == second
    for payload, answers, picture in (
        (original, expected, png),
        (swapped, swapped_expected, swapped_png),
    ):
        assert payload["state"] == {"image": probe.IMAGE_ID}
        assert list(payload["questions"]) == list(probe.COLORS)
        assert set(payload) == {"model", "state", "questions", "assets"}
        assert base64.b64decode(payload["assets"][probe.IMAGE_ID]["data_base64"]) == picture
        assert payload["assets"][probe.IMAGE_ID]["type"] == "image"
        assert payload["assets"][probe.IMAGE_ID]["format"] == "png"
        # Each choice's label lists possible positions, never the expected mapping.
        assert answers not in [payload["state"], payload["questions"]]
        with Image.open(io.BytesIO(picture)) as image:
            assert image.size == (320, 320)
            colors = {
                "red": (255, 0, 0),
                "green": (0, 128, 0),
                "blue": (0, 0, 255),
                "yellow": (255, 255, 0),
            }
            for color, position in answers.items():
                index = probe.POSITIONS.index(position)
                assert (
                    image.getpixel((80 + index % 2 * 160, 80 + index // 2 * 160)) == colors[color]
                )


@pytest.mark.parametrize(
    ("scenario", "status"),
    [
        ("correct", "passed"),
        ("wrong", "wrong_answers"),
        ("bad_gateway", "http_error"),
        ("invalid_json", "invalid_response"),
    ],
)
def test_one_native_request_keeps_exact_response_and_never_retries(
    probe, tmp_path, capsys, scenario, status
):
    folder = tmp_path / scenario
    _, expected, _ = probe.build_case("image_original", "jev")
    body = answer_body(probe, expected)
    if scenario == "wrong":
        body = answer_body(probe, dict.fromkeys(probe.COLORS, "top_left"))
    raw = json.dumps(body, indent=2).encode() + b"\n"
    code, content_type = 200, "application/json"
    if scenario == "bad_gateway":
        code, content_type, raw = 502, "text/html", b"<html>upstream unavailable</html>\xff"
    elif scenario == "invalid_json":
        content_type, raw = "text/plain", b"Not a JSON response\n"
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://router.test/auto/v1"
        assert request.headers["Authorization"] == "Bearer private-test-key"
        assert "TypeSafe-Danger-Zone-Images" not in request.headers
        assert request.headers["Content-Type"] == "application/json"
        assert request.content == (folder / "request.json").read_bytes()
        assert probe_payload(request)["model"] == "jev"
        return httpx.Response(
            code,
            content=raw,
            headers={"content-type": content_type, "x-request-id": "safe-request-id"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = probe.run_case(
            client,
            "https://router.test/auto/v1",
            "jev",
            "private-test-key",
            "image_original",
            folder,
        )
    assert result["status"] == status and result["http_requests"] == len(calls) == 1
    assert (folder / "response.txt").read_bytes() == raw
    assert json.loads((folder / "response-meta.json").read_text())["status_code"] == code
    if scenario in {"correct", "wrong"}:
        assert json.loads((folder / "response.json").read_text()) == body
        assert result["correct"] == (4 if scenario == "correct" else 1)
    else:
        assert not (folder / "response.json").exists()
    assert "private-test-key" not in (folder / "request-meta.json").read_text()
    assert "private-test-key" not in capsys.readouterr().out


def test_timeout_does_not_retry_or_invent_response_files(probe, tmp_path, capsys):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("private-test-key in provider exception", request=request)

    folder = tmp_path / "timeout"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = probe.run_case(
            client,
            "https://router.test/auto/v1",
            "jev",
            "private-test-key",
            "image_original",
            folder,
        )
    assert len(calls) == 1 and result["status"] == "network_error"
    assert result["error_type"] == "ReadTimeout"
    assert not list(folder.glob("response*"))
    assert "private-test-key" not in (folder / "result.json").read_text()
    assert "private-test-key" not in capsys.readouterr().out


@pytest.mark.parametrize("prepare_only", [False, True])
def test_cli_all_cases_stays_out_of_workbench_and_sends_each_once(
    probe, tmp_path, monkeypatch, prepare_only
):
    output = tmp_path / "diagnostic"
    monkeypatch.setenv("JEV_TEST_API_KEY", "private-test-key")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe_jev_vision.py",
            "--case",
            "all",
            "--model",
            "jev",
            "--endpoint",
            "https://router.test/auto/v1",
            "--config",
            str(tmp_path / "absent.json"),
            "--output",
            str(output),
            *(["--prepare-only"] if prepare_only else []),
        ],
    )
    calls = []

    def handler(request):
        calls.append(request)
        case = probe.CASES[len(calls) - 1]
        payload, expected, _ = probe.build_case(case, "jev")
        assert probe_payload(request) == payload
        assert "TypeSafe-Danger-Zone-Images" not in request.headers
        return httpx.Response(200, json=answer_body(probe, expected))

    original_client = httpx.Client
    monkeypatch.setattr(
        probe.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert probe.main() == 0
    assert len(calls) == (0 if prepare_only else 4)
    results = json.loads((output / "summary.json").read_text())
    assert [r["case"] for r in results] == list(probe.CASES)
    assert all(r["status"] == ("prepared" if prepare_only else "passed") for r in results)
    assert not list(tmp_path.rglob("*.sqlite*"))


@pytest.mark.parametrize("answer_strategy", ["first_option", "correct"])
def test_rotated_options_only_change_wire_order_and_distinguish_accuracy_from_bias(
    probe, tmp_path, monkeypatch, answer_strategy
):
    output = tmp_path / "rotations"
    monkeypatch.setenv("JEV_TEST_API_KEY", "private-test-key")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe_jev_vision.py", "--case", "image_original", "--rotate-options",
            "--model", "jev", "--endpoint", "https://router.test/auto/v1",
            "--config", str(tmp_path / "absent.json"), "--output", str(output),
        ],
    )
    baseline, expected, original_png = probe.build_case("image_original", "jev")
    calls, first_options = [], []

    def handler(request):
        offset = len(calls)
        calls.append(request)
        payload = probe_payload(request)
        order = list(probe.POSITIONS[offset:] + probe.POSITIONS[:offset])
        # Dict equality deliberately ignores order: all semantic data must stay unchanged.
        assert payload == baseline
        assert list(payload["questions"]) == list(probe.COLORS)
        for question in payload["questions"].values():
            assert list(question["criteria"]) == order
        assert base64.b64decode(
            payload["assets"][probe.IMAGE_ID]["data_base64"], validate=True
        ) == original_png
        first_options.append(order[0])
        # Restore only the option order; the entire serialized request must then match.
        for question in payload["questions"].values():
            question["criteria"] = {
                key: question["criteria"][key] for key in probe.POSITIONS
            }
        assert payload == probe_payload(calls[0])
        assert "TypeSafe-Danger-Zone-Images" not in request.headers
        answers = dict.fromkeys(probe.COLORS, order[0]) if answer_strategy == "first_option" else expected
        return httpx.Response(200, json=answer_body(probe, answers))

    original_client = httpx.Client
    monkeypatch.setattr(
        probe.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert probe.main() == (1 if answer_strategy == "first_option" else 0)
    assert len(calls) == len(set(request.content for request in calls)) == 4
    assert first_options == list(probe.POSITIONS)
    results = json.loads((output / "summary.json").read_text())
    assert len(results) == 4
    for offset, result in enumerate(results):
        assert result["option_offset"] == offset
        assert result["option_order"] == list(probe.POSITIONS[offset:] + probe.POSITIONS[:offset])
        assert result["expected"] == expected
        assert result["http_requests"] == 1
        assert result["correct"] == (1 if answer_strategy == "first_option" else 4)
        assert result["first_option_answers"] == (4 if answer_strategy == "first_option" else 1)
    assert len(list(output.rglob("request.json"))) == 4
    assert not list(tmp_path.rglob("*.sqlite*"))


def test_rotate_options_rejects_all_cases_before_sending_requests(probe, tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys, "argv",
        ["probe_jev_vision.py", "--case", "all", "--rotate-options", "--output", str(tmp_path / "unused")],
    )

    def forbidden_client(*args, **kwargs):
        pytest.fail("An invalid case combination must not create an HTTP client")

    monkeypatch.setattr(probe.httpx, "Client", forbidden_client)
    with pytest.raises(SystemExit) as error:
        probe.main()
    assert error.value.code == 2
    assert not list(tmp_path.rglob("request.json"))


@pytest.mark.parametrize("reverse_all_red", [False, True])
def test_bidirectional_uses_same_image_and_scores_position_question_ids(
    probe, tmp_path, monkeypatch, reverse_all_red
):
    output = tmp_path / "bidirectional"
    colors = ("red", "yellow", "green", "blue")
    positions = ("top_left", "top_right", "bottom_left", "bottom_right")
    expected = [dict(zip(colors, positions, strict=True)), dict(zip(positions, colors, strict=True))]
    monkeypatch.setenv("JEV_TEST_API_KEY", "private-test-key")
    monkeypatch.setattr(
        sys, "argv",
        [
            "probe_jev_vision.py", "--case", "image_bidirectional",
            "--model", "jev", "--endpoint", "https://router.test/auto/v1",
            "--config", str(tmp_path / "absent.json"), "--output", str(output),
        ],
    )
    payloads = []

    def handler(request):
        direction = len(payloads)
        payload = probe_payload(request)
        payloads.append(payload)
        assert list(payload["questions"]) == list(colors if direction == 0 else positions)
        assert set(payload) == {"model", "state", "questions", "assets"}
        assert payload["state"] == {"image": probe.IMAGE_ID}
        assert "TypeSafe-Danger-Zone-Images" not in request.headers
        options = positions if direction == 0 else colors
        answers = {}
        for question_id, question in payload["questions"].items():
            assert question["type"] == "choice"
            assert question["criteria"] == {key: key.replace("_", " ") for key in options}
            assert "`image`" in question["instructions"]
            assert question_id.replace("_", " ") in question["instructions"]
            # The prompt states the question, never its color-position answer.
            assert not any(key.replace("_", " ") in question["instructions"] for key in options)
            choice = "red" if direction == 1 and reverse_all_red else expected[direction][question_id]
            answers[question_id] = {
                "type": "choice", "choice": choice, "confidence": 0.97,
                "probabilities": {key: float(key == choice) for key in options},
            }
        return httpx.Response(200, json=wire_result({"answers": answers, "model": "jev-test-version"}))

    original_client = httpx.Client
    monkeypatch.setattr(
        probe.httpx, "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert probe.main() == (1 if reverse_all_red else 0)
    assert len(payloads) == 2
    assert {key: value for key, value in payloads[0].items() if key != "questions"} == {
        key: value for key, value in payloads[1].items() if key != "questions"
    }
    png = base64.b64decode(payloads[0]["assets"][probe.IMAGE_ID]["data_base64"], validate=True)
    with Image.open(io.BytesIO(png)) as image:
        assert image.size == (320, 320)
        assert [image.getpixel(point) for point in ((80, 80), (240, 80), (80, 240), (240, 240))] == [
            (255, 0, 0), (255, 255, 0), (0, 128, 0), (0, 0, 255),
        ]
    results = json.loads((output / "summary.json").read_text())
    assert [result["case"] for result in results] == ["color_positions", "position_colors"]
    assert [result["correct"] for result in results] == [4, 1 if reverse_all_red else 4]
    for index, result in enumerate(results):
        assert result["expected"] == expected[index]
        assert result["http_requests"] == 1
        assert result["total"] == 4
        assert list(result["actual"]) == list(expected[index])
        assert list(result["confidence"]) == list(expected[index])
    assert len(list(output.rglob("request.json"))) == 2
    assert not list(tmp_path.rglob("*.sqlite*"))
