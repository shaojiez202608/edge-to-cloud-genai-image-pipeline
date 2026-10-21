"""Content-free structured logs and CloudWatch Embedded Metric Format (EMF) metrics.

Logged fields are restricted to an allow-list of identifiers, enums, counts, codes and timings.
Image content, model text and declared metadata are never logged; values of unknown fields are
dropped, and string values are truncated.
"""

from __future__ import annotations

import json
import os
import sys
import time

NAMESPACE = os.environ.get("METRICS_NAMESPACE", "EdgeGenAIPipeline")
ALLOWED_FIELDS = {
    "event", "thing_name", "image_id", "request_id", "status", "decision", "codes", "error", "step",
    "duration_ms", "latency_ms", "count", "attempt", "categories", "input_tokens", "output_tokens",
    "batch_size", "failures", "mode",
}
_SINK = []  # tests can capture records here


def log(event: str, **fields) -> dict:
    rec = {"ts": int(time.time() * 1000), "event": event}
    for k, v in fields.items():
        if k not in ALLOWED_FIELDS:
            continue
        if isinstance(v, str):
            v = v[:80]
        elif isinstance(v, list):
            v = [str(x)[:48] for x in v[:20]]
        elif not isinstance(v, int | float | bool) and v is not None:
            v = str(type(v).__name__)
        rec[k] = v
    line = json.dumps(rec, ensure_ascii=True)
    _SINK.append(rec)
    if len(_SINK) > 1000:
        del _SINK[:500]
    print(line, file=sys.stdout, flush=True)
    return rec


def metric(name: str, value: float, unit: str = "Count", **dims: str) -> None:
    """One EMF record; dimensions are fixed low-cardinality values (never thing names)."""
    dims = {k: str(v)[:32] for k, v in dims.items()}
    rec = {
        "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [
            {"Namespace": NAMESPACE, "Dimensions": [sorted(dims)] if dims else [[]],
             "Metrics": [{"Name": name, "Unit": unit}]}]},
        name: value, **dims,
    }
    print(json.dumps(rec), file=sys.stdout, flush=True)
