"""Strongbox Lambda handlers.

Five entry points share this module:

    list_files           GET    /files
    create_upload_url    POST   /files/upload-url
    create_download_url  GET    /files/{fileId}/download-url
    delete_file          DELETE /files/{fileId}
    process_upload       S3 ObjectCreated -> async

Design notes for the report:
  * File bytes never pass through Lambda. The API hands the browser a
    presigned S3 URL and the browser talks to S3 directly. This sidesteps
    the 6 MB Lambda payload limit and keeps invocation time in
    milliseconds regardless of file size.
  * The object key is u/{userId}/{fileId}. userId comes from the verified
    Cognito JWT, never from the request body, so one user cannot construct
    a key inside another user's namespace.
  * DynamoDB rows are written PENDING by the API and flipped to AVAILABLE
    by the S3 event. A row stuck on PENDING means the browser never
    completed the PUT.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import unquote_plus

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLE_NAME = os.environ["TABLE_NAME"]
FILES_BUCKET = os.environ["FILES_BUCKET"]
QUOTA_BYTES = int(os.environ.get("QUOTA_BYTES", 2 * 1024**3))
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")

UPLOAD_URL_TTL = 900      # 15 minutes to start the PUT
DOWNLOAD_URL_TTL = 300    # 5 minutes to start the GET
MAX_FILE_BYTES = 100 * 1024**2

_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(TABLE_NAME)
# SigV4 is required for presigned URLs to validate in every region.
_s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
_sns = boto3.client("sns") if TOPIC_ARN else None

SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")
UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _respond(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, default=_encode),
    }


def _encode(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _caller(event: dict) -> tuple[str, str]:
    """Pull the user identity out of the JWT that API Gateway already verified."""
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
        return claims["sub"], claims.get("email", "")
    except KeyError as exc:
        raise ApiError(401, "No verified identity on this request.") from exc


def _path_file_id(event: dict) -> str:
    file_id = (event.get("pathParameters") or {}).get("fileId", "")
    if not UUID4.match(file_id):
        raise ApiError(400, "That file id is not valid.")
    return file_id


def _key(user_id: str, file_id: str) -> str:
    return f"u/{user_id}/{file_id}"


def api(fn):
    """Turn ApiError into a JSON response and anything else into a 500."""

    def wrapper(event, context):
        try:
            return fn(event, context)
        except ApiError as err:
            return _respond(err.status, {"error": err.message})
        except ClientError as err:
            log.exception("AWS call failed")
            code = err.response.get("Error", {}).get("Code", "Unknown")
            return _respond(502, {"error": f"Storage service rejected the request ({code})."})
        except Exception:  # noqa: BLE001
            log.exception("Unhandled error")
            return _respond(500, {"error": "Something broke on our side. Try again."})

    wrapper.__name__ = fn.__name__
    return wrapper


# ----------------------------------------------------------------------
# GET /files
# ----------------------------------------------------------------------
@api
def list_files(event, _context):
    user_id, _ = _caller(event)

    items: list[dict] = []
    kwargs = {
        "KeyConditionExpression": Key("userId").eq(user_id),
        "ProjectionExpression": "fileId, #n, #s, sizeBytes, contentType, createdAt, uploadedAt",
        "ExpressionAttributeNames": {"#n": "name", "#s": "status"},
    }
    while True:
        page = _table.query(**kwargs)
        items.extend(page.get("Items", []))
        token = page.get("LastEvaluatedKey")
        if not token:
            break
        kwargs["ExclusiveStartKey"] = token

    items.sort(key=lambda i: i.get("createdAt", 0), reverse=True)
    used = sum(int(i.get("sizeBytes", 0)) for i in items)

    return _respond(200, {
        "files": items,
        "usage": {"usedBytes": used, "quotaBytes": QUOTA_BYTES},
    })


# ----------------------------------------------------------------------
# POST /files/upload-url
# ----------------------------------------------------------------------
@api
def create_upload_url(event, _context):
    user_id, _ = _caller(event)

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError as exc:
        raise ApiError(400, "Request body must be JSON.") from exc

    raw_name = str(body.get("name", "")).strip()
    if not raw_name:
        raise ApiError(400, "Give the file a name.")
    name = SAFE_NAME.sub("_", raw_name)[:200]

    content_type = str(body.get("contentType") or "application/octet-stream")[:120]

    try:
        size = int(body.get("sizeBytes", 0))
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "sizeBytes must be a number.") from exc
    if size <= 0:
        raise ApiError(400, "That file is empty.")
    if size > MAX_FILE_BYTES:
        raise ApiError(413, f"Files are capped at {MAX_FILE_BYTES // 1024**2} MB.")

    used = _used_bytes(user_id)
    if used + size > QUOTA_BYTES:
        remaining = max(QUOTA_BYTES - used, 0)
        raise ApiError(
            409,
            f"Not enough space. {remaining // 1024**2} MB left of "
            f"{QUOTA_BYTES // 1024**2} MB.",
        )

    file_id = str(uuid.uuid4())
    now = int(time.time())

    _table.put_item(
        Item={
            "userId": user_id,
            "fileId": file_id,
            "name": name,
            "contentType": content_type,
            "sizeBytes": 0,
            "declaredBytes": size,
            "status": "PENDING",
            "createdAt": now,
        },
        ConditionExpression="attribute_not_exists(fileId)",
    )

    url = _s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": FILES_BUCKET,
            "Key": _key(user_id, file_id),
            "ContentType": content_type,
        },
        ExpiresIn=UPLOAD_URL_TTL,
    )

    return _respond(201, {
        "fileId": file_id,
        "name": name,
        "uploadUrl": url,
        "expiresIn": UPLOAD_URL_TTL,
    })


def _used_bytes(user_id: str) -> int:
    total = 0
    kwargs = {
        "KeyConditionExpression": Key("userId").eq(user_id),
        "ProjectionExpression": "sizeBytes, declaredBytes, #s",
        "ExpressionAttributeNames": {"#s": "status"},
    }
    while True:
        page = _table.query(**kwargs)
        for item in page.get("Items", []):
            # Count pending rows at their declared size so a burst of
            # parallel uploads cannot overshoot the quota.
            total += int(item.get("sizeBytes") or item.get("declaredBytes") or 0)
        token = page.get("LastEvaluatedKey")
        if not token:
            return total
        kwargs["ExclusiveStartKey"] = token


# ----------------------------------------------------------------------
# GET /files/{fileId}/download-url
# ----------------------------------------------------------------------
@api
def create_download_url(event, _context):
    user_id, _ = _caller(event)
    file_id = _path_file_id(event)

    record = _table.get_item(Key={"userId": user_id, "fileId": file_id}).get("Item")
    if not record:
        raise ApiError(404, "No such file in your vault.")
    if record.get("status") != "AVAILABLE":
        raise ApiError(409, "That upload never finished.")

    name = record.get("name", "download")
    url = _s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": FILES_BUCKET,
            "Key": _key(user_id, file_id),
            "ResponseContentDisposition": f'attachment; filename="{name}"',
        },
        ExpiresIn=DOWNLOAD_URL_TTL,
    )

    return _respond(200, {"downloadUrl": url, "expiresIn": DOWNLOAD_URL_TTL})


# ----------------------------------------------------------------------
# DELETE /files/{fileId}
# ----------------------------------------------------------------------
@api
def delete_file(event, _context):
    user_id, _ = _caller(event)
    file_id = _path_file_id(event)

    record = _table.get_item(Key={"userId": user_id, "fileId": file_id}).get("Item")
    if not record:
        raise ApiError(404, "No such file in your vault.")

    # Object first: an orphaned row is recoverable, an orphaned object is
    # invisible to the user and still costs money.
    _s3.delete_object(Bucket=FILES_BUCKET, Key=_key(user_id, file_id))
    _table.delete_item(Key={"userId": user_id, "fileId": file_id})

    return _respond(200, {"deleted": file_id})


# ----------------------------------------------------------------------
# S3 ObjectCreated -> async
# ----------------------------------------------------------------------
def process_upload(event, _context):
    """Mark the upload complete and announce it. Runs outside the request path."""
    for record in event.get("Records", []):
        key = unquote_plus(record["s3"]["object"]["key"])
        size = int(record["s3"]["object"].get("size", 0))

        parts = key.split("/")
        if len(parts) != 3 or parts[0] != "u":
            log.warning("Ignoring unexpected key: %s", key)
            continue
        _, user_id, file_id = parts

        try:
            updated = _table.update_item(
                Key={"userId": user_id, "fileId": file_id},
                UpdateExpression=(
                    "SET #s = :available, sizeBytes = :size, uploadedAt = :now "
                    "REMOVE declaredBytes"
                ),
                ConditionExpression="attribute_exists(fileId)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":available": "AVAILABLE",
                    ":size": size,
                    ":now": int(time.time()),
                },
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except ClientError as err:
            if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Object landed with no matching row - almost always a
                # delete that raced the notification. Nothing to do.
                log.warning("No row for %s, skipping", key)
                continue
            raise

        log.info("Upload complete: %s (%s bytes)", key, size)

        if _sns:
            _sns.publish(
                TopicArn=TOPIC_ARN,
                Subject="Strongbox: upload complete",
                Message=(
                    f"{updated.get('name', file_id)} is now stored in your vault.\n\n"
                    f"Size: {size / 1024:.1f} KB\n"
                    f"File id: {file_id}\n"
                ),
                MessageAttributes={
                    "event": {"DataType": "String", "StringValue": "upload.completed"},
                },
            )

    return {"processed": len(event.get("Records", []))}
