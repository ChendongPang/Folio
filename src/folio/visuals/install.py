"""Install pinned local visual dependencies: python -m folio.visuals.install."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from folio.runtime.language import localize_text


def main() -> None:
    parser = argparse.ArgumentParser(description="Install local Mermaid and Chromium rendering dependencies")
    parser.add_argument("--with-deps", action="store_true", help="Also install Chromium system libraries on Linux (may require sudo)")
    parser.add_argument("--language", choices=("en", "zh-CN"), default="en")
    args = parser.parse_args()
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit(localize_text("找不到 npm。请先安装 Node.js 22+，然后重试。", args.language))
    subprocess.run(
        [npm, "ci", "--ignore-scripts", "--omit=dev", "--no-audit", "--no-fund"],
        cwd=Path(__file__).parent, check=True,
    )
    command = [sys.executable, "-m", "playwright", "install"]
    if args.with_deps:
        command.append("--with-deps")
    subprocess.run([*command, "chromium"], check=True)
    print(localize_text("本地配图依赖已安装；运行时不需要 CDN 或图片服务 API Key。", args.language))


if __name__ == "__main__":
    main()
