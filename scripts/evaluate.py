"""Prompt-injection evaluation in local mode (mock models; no AWS).

    uv run python scripts/evaluate.py [--model naive|robust] [--negative-control] [--report FILE]

Exit code: 0 when every counted case has its expected outcome. With --negative-control (output
validation disabled) the exit code is 0 only if the evaluation DOES report failures.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guards  # noqa: E402

from edge_pipeline.evaluation import evaluate  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=["naive", "robust"], default="naive")
    ap.add_argument("--negative-control", action="store_true")
    ap.add_argument("--report")
    args = ap.parse_args()
    with contextlib.redirect_stdout(io.StringIO()):
        r = evaluate(args.model, args.negative_control)
    if args.report:
        guards.write_report(args.report, r)
    print(f"model=mock-{args.model} negative_control={args.negative_control}: {r['passed']}/{r['counted']} expected "
          f"outcomes; failed={r['failed']}; known gaps={[g['case_id'] for g in r['known_gaps']]}")
    if args.negative_control:
        ok = len(r["failed"]) > 0
        print("negative control " + ("reported failures (expected)" if ok else "DID NOT report failures: evaluation is broken"))
        sys.exit(0 if ok else 1)
    sys.exit(0 if not r["failed"] else 1)


if __name__ == "__main__":
    main()
