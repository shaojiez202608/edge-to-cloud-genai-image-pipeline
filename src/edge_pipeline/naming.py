"""Names of things, topics and object keys. Device identity always comes from the authenticated
connection (the MQTT client id that IoT Core binds to the certificate's thing), never from a payload.

Topic layout (every device-writable topic is under the device's own name):

    edge/<thing>/telemetry/heartbeat   device -> cloud   heartbeat with a sequence number (QoS 1)
    edge/<thing>/upload/request        device -> cloud   ask for a presigned POST for one image
    edge/<thing>/upload/url            cloud  -> device  presigned POST (or a refusal code)
    edge/<thing>/images/uploaded       device -> cloud   metadata after the upload; starts processing
    edge/<thing>/results               cloud  -> device  processing outcome (status and codes only)
    $aws/things/<thing>/jobs/...       AWS IoT Jobs reserved topics (rollout drill)
"""

from __future__ import annotations

import re

TOPIC_ROOT = "edge"
HEARTBEAT = "telemetry/heartbeat"
UPLOAD_REQUEST = "upload/request"
UPLOAD_URL = "upload/url"
IMAGE_UPLOADED = "images/uploaded"
RESULTS = "results"

# Device kinds: "sim" = lightweight simulator (IoT Device SDK), "gg" = AWS IoT Greengrass core.
KINDS = ("sim", "gg")
_SERIAL = r"[a-z0-9]{1,12}"
IMAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png"}


def thing_name_re(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}-(?:{'|'.join(KINDS)})-{_SERIAL}$")


def thing_name(prefix: str, kind: str, serial: str) -> str:
    name = f"{prefix}-{kind}-{serial}"
    if not thing_name_re(prefix).match(name):
        raise ValueError("invalid thing name")
    return name


def device_topic(thing: str, suffix: str) -> str:
    return f"{TOPIC_ROOT}/{thing}/{suffix}"


def thing_from_topic(topic: str) -> str | None:
    parts = topic.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == TOPIC_ROOT else None


def upload_key(thing: str, image_id: str, content_type: str) -> str:
    return f"uploads/{thing}/{image_id}.{EXTENSIONS[content_type]}"


def work_key(thing: str, image_id: str) -> str:
    return f"work/{thing}/{image_id}.jpg"


def result_key(thing: str, image_id: str) -> str:
    return f"results/{thing}/{image_id}.json"


def shelf_image_key(thing: str, image_id: str) -> str:
    return f"shelf/images/{thing}/{image_id}.jpg"


def shelf_item_key(thing: str, image_id: str) -> str:
    return f"shelf/items/{thing}/{image_id}.json"


SHELF_MANIFEST_KEY = "shelf/manifest.json"
LOADTEST_PREFIX = "loadtest/"
