"""Start-up health check of the simulated edge model (Greengrass component lifecycle "Startup").

The "model" is a JSON file with a decision threshold. The self-test scores fixed reference samples;
a threshold outside the healthy range misclassifies them and the script exits non-zero, so the
component errors, becomes BROKEN after repeated failures, and the deployment fails and rolls back.
"""

import json
import sys

REFERENCE = [(0.9, True), (0.75, True), (0.3, False), (0.1, False)]


def main(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        model = json.load(f)
    threshold = model.get("threshold")
    # Tuple form on purpose: the Greengrass image ships an older Python 3 (found in testing).
    if not isinstance(threshold, (int, float)):  # noqa: UP038
        print("selftest: model file malformed")
        return 2
    wrong = sum(1 for score, label in REFERENCE if (score >= threshold) != label)
    print(f"selftest: version={model.get('version')} misclassified={wrong}")
    return 0 if wrong == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
