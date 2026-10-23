import json
import sys

import pytest

from folio.cli import main


@pytest.mark.parametrize(
    ("command", "renderer_name", "filename", "source"),
    [
        ("render-ui", "render_ui", "原型.html", "<h1>交易录入</h1>"),
        ("render-flowchart", "render_flowchart", "流程.mmd", "flowchart TD\n A[录入] --> B[审批]"),
    ],
)
def test_visual_command_renders_utf8_without_model_or_store(
    tmp_path, monkeypatch, capsys, command, renderer_name, filename, source
):
    from folio.visuals import render

    source_file = tmp_path / filename
    source_file.write_text(source, encoding="utf-8")
    output_dir = tmp_path / "配图"
    calls = []

    async def renderer(actual_source, actual_output_dir):
        calls.append((actual_source, actual_output_dir))
        return {
            "image_path": actual_output_dir / "image.png",
            "source_path": actual_output_dir / filename,
            "width": 1000,
            "height": 600,
        }

    def unexpected_store(*args, **kwargs):
        pytest.fail("Standalone render commands must not initialize the job store")

    monkeypatch.setattr(render, renderer_name, renderer)
    monkeypatch.setattr("folio.cli.Store", unexpected_store)
    monkeypatch.setattr(
        sys, "argv", ["folio", command, str(source_file), "--output-dir", str(output_dir)]
    )
    main()

    assert calls == [(source, output_dir)]
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["image_path"] == str(output_dir / "image.png")
    assert result["source_path"] == str(output_dir / filename)
    assert (result["width"], result["height"]) == (1000, 600)
    assert captured.err == ""


@pytest.mark.parametrize(
    "command,renderer_name", [("render-ui", "render_ui"), ("render-flowchart", "render_flowchart")]
)
def test_visual_renderer_failure_returns_nonzero_without_success_output(
    tmp_path, monkeypatch, capsys, command, renderer_name
):
    from folio.visuals import render

    source_file = tmp_path / "input.txt"
    source_file.write_text("input", encoding="utf-8")

    async def renderer(*args):
        raise RuntimeError("浏览器不可用，请安装 Chromium")

    monkeypatch.setattr(render, renderer_name, renderer)
    monkeypatch.setattr(
        sys, "argv", ["folio", command, str(source_file), "--output-dir", str(tmp_path / "out")]
    )
    with pytest.raises(SystemExit) as failure:
        main()

    assert failure.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "浏览器不可用，请安装 Chromium" in captured.err


@pytest.mark.parametrize("invalid_encoding", [False, True])
def test_visual_source_read_failure_returns_nonzero(
    tmp_path, monkeypatch, capsys, invalid_encoding
):
    source_file = tmp_path / "input.html"
    if invalid_encoding:
        source_file.write_bytes(b"\xff\xfe")
    monkeypatch.setattr(
        sys,
        "argv",
        ["folio", "render-ui", str(source_file), "--output-dir", str(tmp_path / "out")],
    )
    with pytest.raises(SystemExit) as failure:
        main()

    assert failure.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "UTF-8" in captured.err if invalid_encoding else "FileNotFoundError" in captured.err
