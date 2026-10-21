import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("src", "src/functions", "infra", "scripts"):
    sys.path.insert(0, str(ROOT / p))
