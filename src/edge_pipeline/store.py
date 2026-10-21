"""Storage adapters (S3 objects, DynamoDB items, MQTT notifications) used by the handlers.
Local mode swaps them for the in-process stand-ins in ``edge_pipeline.local.fakes``.

Item table layout (partition key ``thing_name``, sort key ``sk``):
  img#<image_id>         one per issued upload: declared type/size/hash, status, timestamps
  quota#<YYYYMMDDHH>     per-device upload counter for one hour (expires with ``ttl``)
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol


class Conflict(Exception):
    """Conditional write lost a race (manifest ETag changed, item already claimed)."""


class ObjectStore(Protocol):
    def get(self, key: str) -> bytes: ...
    def get_versioned(self, key: str) -> tuple[bytes | None, str | None]: ...
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def put_if(self, key: str, data: bytes, content_type: str, etag: str | None) -> None: ...
    def presigned_post(self, key: str, content_type: str, size: int, expiry_s: int) -> dict: ...


class ItemTable(Protocol):
    def put_issued(self, thing: str, image_id: str, declared: dict, expires_ms: int) -> None: ...
    def claim(self, thing: str, image_id: str, now_ms: int) -> dict | None: ...
    def set_status(self, thing: str, image_id: str, status: str, fields: dict) -> None: ...
    def count_upload(self, thing: str, hour: str, limit: int, ttl_s: int) -> bool: ...


class Notifier(Protocol):
    def publish(self, topic: str, payload: dict) -> None: ...


# --------------------------------------------------------------------------- AWS implementations


class S3Store:
    def __init__(self, client: Any, bucket: str):
        self.s3, self.bucket = client, bucket

    def get(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def get_versioned(self, key: str) -> tuple[bytes | None, str | None]:
        from botocore.exceptions import ClientError

        try:
            r = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None, None
            raise
        return r["Body"].read(), r["ETag"]

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def put_if(self, key: str, data: bytes, content_type: str, etag: str | None) -> None:
        """Conditional write: If-Match the ETag read before, or If-None-Match '*' for a new object."""
        from botocore.exceptions import ClientError

        cond = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, **cond)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("PreconditionFailed", "ConditionalRequestConflict", "412", "409"):
                raise Conflict(key) from None
            raise

    def presigned_post(self, key: str, content_type: str, size: int, expiry_s: int) -> dict:
        # The POST policy pins the exact key, the content type and the declared size (min = max), so the
        # device cannot upload anything else with this grant.
        return self.s3.generate_presigned_post(
            Bucket=self.bucket, Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[{"Content-Type": content_type}, ["content-length-range", size, size]],
            ExpiresIn=expiry_s,
        )


class DynamoItems:
    def __init__(self, client: Any, table: str):
        self.ddb, self.table = client, table

    def put_issued(self, thing: str, image_id: str, declared: dict, expires_ms: int) -> None:
        self.ddb.put_item(
            TableName=self.table,
            Item={"thing_name": {"S": thing}, "sk": {"S": f"img#{image_id}"}, "status": {"S": "url_issued"},
                  "declared": {"S": json.dumps(declared, sort_keys=True)},
                  "issued_ms": {"N": str(int(time.time() * 1000))}, "url_expires_ms": {"N": str(expires_ms)},
                  "ttl": {"N": str(int(time.time()) + 30 * 86400)}},
            ConditionExpression="attribute_not_exists(sk)",
        )

    def claim(self, thing: str, image_id: str, now_ms: int) -> dict | None:
        """url_issued -> queued, exactly once. Returns the declared metadata or None (unknown id, other
        device's id, replay, or already claimed)."""
        from botocore.exceptions import ClientError

        try:
            r = self.ddb.update_item(
                TableName=self.table,
                Key={"thing_name": {"S": thing}, "sk": {"S": f"img#{image_id}"}},
                UpdateExpression="SET #s = :q, queued_ms = :now",
                ConditionExpression="#s = :issued",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":q": {"S": "queued"}, ":issued": {"S": "url_issued"},
                                           ":now": {"N": str(now_ms)}},
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise
        return json.loads(r["Attributes"]["declared"]["S"])

    def set_status(self, thing: str, image_id: str, status: str, fields: dict) -> None:
        names = {"#s": "status"}
        values = {":s": {"S": status}, ":t": {"N": str(int(time.time() * 1000))}}
        sets = ["#s = :s", "updated_ms = :t"]
        for i, (k, v) in enumerate(sorted(fields.items())):
            names[f"#f{i}"] = k
            values[f":f{i}"] = {"S": json.dumps(v, sort_keys=True)} if not isinstance(v, str) else {"S": v}
            sets.append(f"#f{i} = :f{i}")
        self.ddb.update_item(TableName=self.table,
                             Key={"thing_name": {"S": thing}, "sk": {"S": f"img#{image_id}"}},
                             UpdateExpression="SET " + ", ".join(sets),
                             ExpressionAttributeNames=names, ExpressionAttributeValues=values)

    def count_upload(self, thing: str, hour: str, limit: int, ttl_s: int) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.ddb.update_item(
                TableName=self.table,
                Key={"thing_name": {"S": thing}, "sk": {"S": f"quota#{hour}"}},
                UpdateExpression="ADD n :one SET #ttl = if_not_exists(#ttl, :ttl)",
                ConditionExpression="attribute_not_exists(n) OR n < :limit",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":one": {"N": "1"}, ":limit": {"N": str(limit)},
                                           ":ttl": {"N": str(int(time.time()) + ttl_s)}},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True


class IotNotifier:
    def __init__(self, client: Any):
        self.iot = client

    def publish(self, topic: str, payload: dict) -> None:
        self.iot.publish(topic=topic, qos=1, payload=json.dumps(payload).encode())
