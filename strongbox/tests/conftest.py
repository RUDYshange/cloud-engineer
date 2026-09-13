"""Shared fixtures for the Strongbox handler unit tests.

Moto must be active BEFORE handlers.py imports boto3 (the module builds its
clients at import time), so handlers is imported inside the `aws` fixture
with a fresh module each run. Never import handlers at the top of a test
module - always go through the fixture.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

REGION = "eu-west-1"
TABLE = "strongbox-files"
BUCKET = "strongbox-files-123456789012-eu-west-1"
QUOTA = 1073741824  # 1 GiB, matches the fixture env passed to handlers.py
USER_ID = "0b86f6f2-1a2b-4c3d-9e4f-5a6b7c8d9e0f"
OTHER_ID = "7f3c9d21-4b5e-4f6a-8c7d-2e1f0a9b8c7d"
USER_EMAIL = "user@example.com"

BASE_ENV = {
    "TABLE_NAME": TABLE,
    "FILES_BUCKET": BUCKET,
    "QUOTA_BYTES": str(QUOTA),
    "AWS_DEFAULT_REGION": REGION,
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
}


def _load_handlers():
    """Import handlers fresh so module-level clients bind to moto's mock state."""
    sys.modules.pop("handlers", None)
    return importlib.import_module("handlers")


@pytest.fixture()
def aws(request, monkeypatch):
    """Moto-backed DynamoDB/S3/SNS plus the handlers module.

    Parametrize indirectly to drop the SNS topic:
        @pytest.mark.parametrize("aws", [False], indirect=True)
    """
    with_topic = getattr(request, "param", True)

    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "fileId", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "userId", "KeyType": "HASH"},
                {"AttributeName": "fileId", "KeyType": "RANGE"},
            ],
        )

        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )

        sns = None
        topic_arn = ""
        if with_topic:
            sns = boto3.client("sns", region_name=REGION)
            topic_arn = sns.create_topic(Name="strongbox-events")["TopicArn"]
            monkeypatch.setenv("TOPIC_ARN", topic_arn)
        else:
            monkeypatch.delenv("TOPIC_ARN", raising=False)

        yield SimpleNamespace(
            handlers=_load_handlers(),
            table=ddb.Table(TABLE),
            s3=s3,
            sns=sns,
            topic_arn=topic_arn,
        )

    sys.modules.pop("handlers", None)


@pytest.fixture()
def sns_spy(aws, monkeypatch):
    """Replace the handler module's SNS client so publishes can be asserted."""
    from unittest.mock import MagicMock

    spy = MagicMock()
    monkeypatch.setattr(aws.handlers, "_sns", spy)
    return spy


@pytest.fixture()
def api_event():
    """Build an API Gateway proxy event carrying a verified Cognito JWT."""

    def make(method="GET", body=None, file_id=None, sub=USER_ID, email=USER_EMAIL, **extra):
        event = {
            "requestContext": {
                "http": {"method": method},
                "authorizer": {"jwt": {"claims": {"sub": sub, "email": email}}},
            },
            "pathParameters": {"fileId": file_id} if file_id is not None else {},
            "body": json.dumps(body) if body is not None else None,
        }
        event.update(extra)
        return event

    return make


@pytest.fixture()
def s3_event():
    """Build an s3:ObjectCreated notification event."""

    def make(key, size=10):
        return {"Records": [{"s3": {"object": {"key": key, "size": size}}}]}

    return make


@pytest.fixture()
def seed_pending(aws):
    """Insert a PENDING metadata row straight into DynamoDB."""

    def make(sub=USER_ID, file_id=None, name="pending.bin", size=3000, created_at=None):
        fid = file_id or str(uuid.uuid4())
        aws.table.put_item(Item={
            "userId": sub,
            "fileId": fid,
            "name": name,
            "contentType": "application/octet-stream",
            "sizeBytes": 0,
            "declaredBytes": size,
            "status": "PENDING",
            "createdAt": created_at if created_at is not None else int(time.time()),
        })
        return fid

    return make


@pytest.fixture()
def seed_available(aws):
    """Insert a PENDING row, put the object, fire the async handler.

    Leaves the vault in exactly the state a finished upload produces.
    """

    def make(sub=USER_ID, name="notes.pdf", size=2048, content_type="application/pdf", created_at=None):
        fid = str(uuid.uuid4())
        key = f"u/{sub}/{fid}"
        aws.table.put_item(Item={
            "userId": sub,
            "fileId": fid,
            "name": name,
            "contentType": content_type,
            "sizeBytes": 0,
            "declaredBytes": size,
            "status": "PENDING",
            "createdAt": created_at if created_at is not None else int(time.time()) - 5,
        })
        aws.s3.put_object(Bucket=BUCKET, Key=key, Body=b"x" * size, ContentType=content_type)
        aws.handlers.process_upload(
            {"Records": [{"s3": {"object": {"key": key, "size": size}}}]}, None,
        )
        return fid

    return make
