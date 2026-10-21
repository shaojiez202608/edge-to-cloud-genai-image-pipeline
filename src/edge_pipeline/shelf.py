"""The "shelf": an e-commerce-ready image (subject cropped and placed on a plain square canvas) and
a manifest file in S3 that lists the processed items. The manifest is updated with conditional
writes (S3 If-Match / If-None-Match), so concurrent finalizers cannot overwrite each other.

The background tidy-up is deterministic image processing in code (crop + plain canvas); the model only
proposes the subject box, which has been validated before it is used here.
"""

from __future__ import annotations

import io
import json
import time

from PIL import Image

from . import naming
from .store import Conflict, ObjectStore

MARGIN = 0.08
PAD = 0.05
MAX_ATTEMPTS = 6


def render_shelf_image(jpeg: bytes, box: dict, side: int) -> bytes:
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = img.size
    x0 = max(0.0, box["x"] - MARGIN * box["w"]) * w
    y0 = max(0.0, box["y"] - MARGIN * box["h"]) * h
    x1 = min(1.0, box["x"] + box["w"] * (1 + MARGIN)) * w
    y1 = min(1.0, box["y"] + box["h"] * (1 + MARGIN)) * h
    if x1 - x0 < 8 or y1 - y0 < 8:
        x0, y0, x1, y1 = 0, 0, w, h
    crop = img.crop((int(x0), int(y0), int(x1), int(y1)))
    inner = int(side * (1 - 2 * PAD))
    crop.thumbnail((inner, inner), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (side, side), (255, 255, 255))
    canvas.paste(crop, ((side - crop.width) // 2, (side - crop.height) // 2))
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=90)
    return out.getvalue()


def update_manifest(store: ObjectStore, entry: dict, max_items: int) -> int:
    """Insert or replace one entry (keyed by thing_name + image_id). Returns the attempts used."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw, etag = store.get_versioned(naming.SHELF_MANIFEST_KEY)
        doc = json.loads(raw) if raw else {"schema_version": 1, "items": []}
        key = (entry["thing_name"], entry["image_id"])
        items = [i for i in doc.get("items", []) if (i.get("thing_name"), i.get("image_id")) != key]
        items.append(entry)
        items.sort(key=lambda i: i.get("updated_ms", 0), reverse=True)
        doc = {"schema_version": 1, "updated_ms": int(time.time() * 1000), "items": items[:max_items]}
        try:
            store.put_if(naming.SHELF_MANIFEST_KEY, json.dumps(doc, ensure_ascii=False, indent=1).encode(),
                         "application/json", etag)
            return attempt
        except Conflict:
            time.sleep(0.05 * attempt)
    raise Conflict(naming.SHELF_MANIFEST_KEY)
