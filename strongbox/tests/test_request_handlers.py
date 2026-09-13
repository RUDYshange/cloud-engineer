"""Tests for list_files, create_upload_url, and the shared api wrapper."""

from __future__ import annotations

import json
import uuid

import pytest
from botocore.exceptions import ClientError

from conftest import USER_ID

FILE_ID = "c9a6b1d4-2e3f-4a5b-8c9d-0e1f2a3b4c5d"


def body(response):
    return json.loads(response["body"])


# ----------------------------------------------------------------------
# list_files
# ----------------------------------------------------------------------
class TestListFiles:
    def test_empty_vault(self, aws, api_event):
        res = aws.handlers.list_files(api_event(), None)

        assert res["statusCode"] == 200
        assert body(res) == {"files": [], "usage": {"usedBytes": 0, "quotaBytes": aws.handlers.QUOTA_BYTES}}

    def test_lists_only_caller_rows_newest_first(self, aws, api_event, seed_available):
        import time

        now = int(time.time())
        old_id = seed_available(name="older.pdf", created_at=now - 120)
        new_id = seed_available(name="newer.pdf", created_at=now)
        seed_available(sub="11111111-2222-4333-8444-555566667777", name="someone-elses.pdf")

        res = aws.handlers.list_files(api_event(), None)
        payload = body(res)

        assert [f["fileId"] for f in payload["files"]] == [new_id, old_id]
        assert all(f["name"] != "someone-elses.pdf" for f in payload["files"])

    def test_usage_sums_available_rows(self, aws, api_event, seed_available):
        seed_available(size=1024)
        seed_available(size=512)

        res = aws.handlers.list_files(api_event(), None)

        assert body(res)["usage"]["usedBytes"] == 1024 + 512

    def test_projection_hides_internal_fields(self, aws, api_event, seed_available):
        seed_available()

        res = aws.handlers.list_files(api_event(), None)

        assert set(body(res)["files"][0]) == {
            "fileId", "name", "status", "sizeBytes", "contentType", "createdAt", "uploadedAt",
        }

    def test_missing_identity_rejected(self, aws, api_event):
        event = api_event()
        event["requestContext"]["authorizer"]["jwt"]["claims"] = {"email": "x@example.com"}

        res = aws.handlers.list_files(event, None)

        assert res["statusCode"] == 401
        assert "error" in body(res)


# ----------------------------------------------------------------------
# create_upload_url
# ----------------------------------------------------------------------
class TestCreateUploadUrl:
    def test_happy_path_writes_pending_row_and_presigns_put(self, aws, api_event):
        event = api_event(method="POST", body={
            "name": "notes.pdf", "sizeBytes": 2048, "contentType": "application/pdf",
        })

        res = aws.handlers.create_upload_url(event, None)
        payload = body(res)

        assert res["statusCode"] == 201
        assert payload["expiresIn"] == aws.handlers.UPLOAD_URL_TTL == 900
        assert payload["uploadUrl"].startswith(f"https://{aws.handlers.FILES_BUCKET}.s3")
        assert f"/u/{USER_ID}" in payload["uploadUrl"]

        row = aws.table.get_item(
            Key={"userId": USER_ID, "fileId": payload["fileId"]})["Item"]
        assert row["status"] == "PENDING"
        assert row["declaredBytes"] == 2048
        assert row["sizeBytes"] == 0

    def test_presigned_put_is_sigv4_and_key_is_writable(self, aws, api_event):
        # In-process moto cannot serve real HTTP for the presigned URL; the
        # signed shape is asserted here and exercised live by smoke_test.py.
        event = api_event(method="POST", body={
            "name": "f.bin", "sizeBytes": 10, "contentType": "application/octet-stream",
        })

        payload = body(aws.handlers.create_upload_url(event, None))

        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(payload["uploadUrl"]).query)
        assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
        assert query["X-Amz-Expires"] == ["900"]
        # The ContentType is bound into the signature via signed headers, so
        # a browser PUT with a different Content-Type is rejected by S3.
        assert query["X-Amz-SignedHeaders"] == ["content-type;host"]
        assert "X-Amz-Signature" in query

        # The key the URL points at must be writable under the u/ prefix.
        put = aws.s3.put_object(
            Bucket=aws.handlers.FILES_BUCKET, Key=f"u/{USER_ID}/{payload['fileId']}",
            Body=b"0123456789", ContentType="application/octet-stream")
        assert put["ResponseMetadata"]["HTTPStatusCode"] == 200

    def test_strips_unsafe_characters_from_name(self, aws, api_event):
        # The allowlist keeps dots (so "notes.v2.pdf" survives) but strips
        # separators and query characters, defusing path traversal.
        event = api_event(method="POST", body={
            "name": "../../etc/passwd?.pdf", "sizeBytes": 10, "contentType": "application/pdf",
        })

        payload = body(aws.handlers.create_upload_url(event, None))

        assert "/" not in payload["name"] and "?" not in payload["name"]

    def test_long_name_is_truncated(self, aws, api_event):
        event = api_event(method="POST", body={
            "name": "x" * 500 + ".pdf", "sizeBytes": 10, "contentType": "application/pdf",
        })

        payload = body(aws.handlers.create_upload_url(event, None))

        assert len(payload["name"]) <= 200

    def test_missing_content_type_defaults(self, aws, api_event):
        event = api_event(method="POST", body={"name": "f.bin", "sizeBytes": 10})

        row = aws.table.get_item(
            Key={"userId": USER_ID, "fileId": body(
                aws.handlers.create_upload_url(event, None))["fileId"]})["Item"]

        assert row["contentType"] == "application/octet-stream"

    def test_rejects_missing_name(self, aws, api_event):
        res = aws.handlers.create_upload_url(
            api_event(method="POST", body={"sizeBytes": 10}), None)

        assert res["statusCode"] == 400

    def test_rejects_non_positive_size(self, aws, api_event):
        for size in (0, -5):
            res = aws.handlers.create_upload_url(
                api_event(method="POST", body={"name": "f.bin", "sizeBytes": size}), None)
            assert res["statusCode"] == 400

    def test_rejects_non_numeric_size(self, aws, api_event):
        res = aws.handlers.create_upload_url(
            api_event(method="POST", body={"name": "f.bin", "sizeBytes": "big"}), None)

        assert res["statusCode"] == 400

    def test_rejects_malformed_json(self, aws, api_event):
        event = api_event(method="POST")
        event["body"] = "{not json"

        res = aws.handlers.create_upload_url(event, None)

        assert res["statusCode"] == 400

    def test_rejects_file_over_per_file_cap(self, aws, api_event):
        too_big = aws.handlers.MAX_FILE_BYTES + 1

        res = aws.handlers.create_upload_url(
            api_event(method="POST", body={"name": "huge.bin", "sizeBytes": too_big}), None)

        assert res["statusCode"] == 413

    def test_rejects_upload_that_would_breach_quota(self, aws, api_event):
        # Seed the usage directly in DynamoDB - putting a real 1 GiB object
        # through moto just to fill the quota would be absurd.
        filler = str(uuid.uuid4())
        aws.table.put_item(Item={
            "userId": USER_ID, "fileId": filler, "name": "filler.bin",
            "contentType": "application/octet-stream",
            "sizeBytes": aws.handlers.QUOTA_BYTES - 500,
            "status": "AVAILABLE", "createdAt": 1,
        })
        event = api_event(method="POST", body={"name": "f.bin", "sizeBytes": 1000})

        res = aws.handlers.create_upload_url(event, None)

        assert res["statusCode"] == 409
        assert "Not enough space" in body(res)["error"]

    def test_pending_rows_count_toward_quota_at_declared_size(self, aws, api_event, seed_pending):
        # A PENDING row must still consume quota: 600 declared + 1000 new > 1500 cap.
        seed_pending(size=600)

        import unittest.mock as mock

        with mock.patch.object(aws.handlers, "QUOTA_BYTES", 1500):
            res = aws.handlers.create_upload_url(
                api_event(method="POST", body={"name": "f.bin", "sizeBytes": 1000}), None)

        assert res["statusCode"] == 409

    def test_accepts_upload_within_quota(self, aws, api_event, seed_available):
        seed_available(size=512)
        event = api_event(method="POST", body={"name": "f.bin", "sizeBytes": 512})

        res = aws.handlers.create_upload_url(event, None)

        assert res["statusCode"] == 201


# ----------------------------------------------------------------------
# shared api() wrapper
# ----------------------------------------------------------------------
class TestApiWrapper:
    def test_unexpected_exception_becomes_500(self, aws, api_event, monkeypatch):
        def boom(event, context):
            raise RuntimeError("boom")

        res = aws.handlers.api(boom)(api_event(), None)

        assert res["statusCode"] == 500

    def test_client_error_becomes_502(self, aws, api_event):
        def aws_fail(event, context):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceeded", "Message": "slow"}},
                "Query",
            )

        res = aws.handlers.api(aws_fail)(api_event(), None)

        assert res["statusCode"] == 502
        assert "ProvisionedThroughputExceeded" in body(res)["error"]

    def test_wrapper_preserves_handler_name(self, aws):
        assert aws.handlers.list_files.__name__ == "list_files"


# ----------------------------------------------------------------------
# _path_file_id validation
# ----------------------------------------------------------------------
class TestPathFileId:
    @pytest.mark.parametrize("bad", ["", "not-a-uuid", "u/../escape",
                                     "c9a6b1d4-2e3f-4a5b-8c9d-0e1f2a3b4c5X"])
    def test_invalid_file_ids_rejected(self, aws, api_event, bad):
        res = aws.handlers.create_download_url(api_event(file_id=bad), None)

        assert res["statusCode"] == 400

    def test_valid_uuid_passes_validation(self, aws, api_event):
        good = str(uuid.uuid4())
        res = aws.handlers.create_download_url(api_event(file_id=good), None)

        # uuid format is fine; the 404 comes from the missing row, not the id.
        assert res["statusCode"] == 404
