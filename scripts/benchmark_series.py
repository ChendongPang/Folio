"""Run ten independent jobs serially; stop on operational failures for diagnosis."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from benchmark_job import code_digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source_job")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "source_job": args.source_job,
        "code_sha256": code_digest(),
        "planned_runs": args.count,
        "runs": [],
        "status": "running",
    }
    path = args.output / "series.json"

    def save():
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    save()
    for index in range(1, args.count + 1):
        if code_digest() != manifest["code_sha256"]:
            manifest["status"] = "stopped_code_changed"
            break
        destination = args.output / f"run-{index:02}"
        with (args.output / f"run-{index:02}.log").open("w") as log:
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/benchmark_job.py",
                    args.source_job,
                    "--output",
                    str(destination),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        result = destination / "result.json"
        report = json.loads(result.read_text()) if result.exists() else {}
        manifest["runs"].append(
            {"index": index, "exit_code": completed.returncode, "report": str(result), **report}
        )
        save()
        print(
            json.dumps(
                {
                    k: report.get(k)
                    for k in (
                        "job_id",
                        "elapsed_seconds",
                        "status",
                        "calls",
                        "saved_nodes",
                        "operational_pass",
                        "passed",
                        "error",
                    )
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if completed.returncode or not report.get("operational_pass"):
            manifest["status"] = "stopped_for_diagnosis"
            break
    else:
        manifest["status"] = "complete"
    manifest["operational_passes"] = sum(bool(r.get("operational_pass")) for r in manifest["runs"])
    manifest["full_passes"] = sum(bool(r.get("passed")) for r in manifest["runs"])
    save()


if __name__ == "__main__":
    main()
