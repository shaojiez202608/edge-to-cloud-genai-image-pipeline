"""Lightweight edge-box simulators (one asyncio task per device) for load tests, the end-to-end path
and the IoT Jobs rollout drill. The same device logic runs over the in-process broker (local mode)
or over AWS IoT Core with the AWS IoT Device SDK v2 for Python (MQTT 5, mutual TLS)."""
