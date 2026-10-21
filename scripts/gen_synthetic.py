"""Write the synthetic prompt-injection set (PNG files, git-ignored) and its manifest.

    uv run python scripts/gen_synthetic.py [--out sample-data/injection]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from edge_pipeline import synthetic  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sample-data/injection")
    args = ap.parse_args()
    out = Path(args.out)
    rows = synthetic.write_set(out)
    for r in rows:
        r.pop("sha256")  # Pillow-version dependent; the manifest stays stable across machines
        r.pop("bytes")
    (out / "manifest.json").write_text(json.dumps({"generator": "scripts/gen_synthetic.py", "cases": rows},
                                                  indent=1, ensure_ascii=False) + "\n")
    print(f"{len(rows)} images in {out}")


if __name__ == "__main__":
    main()
