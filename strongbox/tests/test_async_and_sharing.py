"""Tests for create_download_url, delete_file, process_upload, isolation."""

from __future__ import annotations

import json
import uuid

import pytest

from conftest import OTHER_ID, USER_ID

FILE_ID = "c9a6b1d4-2e3f-4a5b-8c9d-0e1f2a3b4c5d"


def body(response):
    return json.loads(response["body"])


def obj_key(sub, fid):
    return f"u/{sub}/{fid}"


# ----------------------------------------------------------------------
# create_download_url
# ----------------------------------------------------------------------
class TestCreateDownloadUrl:
    def test_happy_path_presigns_get_with_attachment_name(self, aws, api_event, seed_available):
        fid = seed_available(name="report.pdf")

        res = aws.handlers.create_download_url(api_event(file_id=fid), None)
        payload = body(res)

        assert res["statusCode"] == 200
        assert payload["downloadUrl"].startswith(f"https://{aws.handlers.FILES_BUCKET}.s3")
        assert payload["expiresIn"] == aws.handlers.DOWNLOAD_URL_TTL == 300

    def test_presigned_get_is_signed_and_carries_attachment_disposition(self, aws, api_event,
                                                                        seed_available):
        # moto's in-process mock cannot answer real HTTP, so the signature
        # itself is verified against the live stack by scripts/smoke_test.py.
        # Here we assert the handler signed the right request shape.
        from urllib.parse import parse_qs, urlsplit

        fid = seed_available(name="report.pdf")
        payload = body(aws.handlers.create_download_url(api_event(file_id=fid), None))

        query = parse_qs(urlsplit(payload["downloadUrl"]).query)
        assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
        assert query["X-Amz-Expires"] == ["300"]
        assert query["response-content-disposition"] == ['attachment; filename="report.pdf"']
        assert "X-Amz-Signature" in query

    def test_unknown_file_id_404(self, aws, api_event):
        fid = str(uuid.uuid4())

        res = aws.handlers.create_download_url(api_event(file_id=fid), None)

        assert res["statusCode"] == 404

    def test_pending_upload_cannot_be_downloaded(self, aws, api_event, seed_pending):
        fid = seed_pending()

        res = aws.handlers.create_download_url(api_event(file_id=fid), None)

        assert res["statusCode"] == 409


# ----------------------------------------------------------------------
# delete_file
# ----------------------------------------------------------------------
class TestDeleteFile:
    def test_happy_path_removes_object_and_row(self, aws, api_event, seed_available):
        fid = seed_available()

        res = aws.handlers.delete_file(api_event(method="DELETE", file_id=fid), None)

        assert res["statusCode"] == 200
        assert body(res) == {"deleted": fid}
        assert aws.s3.list_objects_v2(Bucket=aws.handlers.FILES_BUCKET,
                                      Prefix=obj_key(USER_ID, fid))["KeyCount"] == 0
        assert aws.table.get_item(Key={"userId": USER_ID, "fileId": fid}).get("Item") is None

    def test_unknown_file_id_404(self, aws, api_event):
        fid = str(uuid.uuid4())

        res = aws.handlers.delete_file(api_event(method="DELETE", file_id=fid), None)

        assert res["statusCode"] == 404

    def test_only_own_row_is_removed(self, aws, api_event, seed_available):
        fid = seed_available()
        foreign = seed_available(sub=OTHER_ID)

        res = aws.handlers.delete_file(api_event(method="DELETE", file_id=foreign), None)

        assert res["statusCode"] == 404
        assert aws.table.get_item(Key={"userId": USER_ID, "fileId": fid}).get("Item") is not None


# ----------------------------------------------------------------------
# process_upload (async leg)
# ----------------------------------------------------------------------
class TestProcessUpload:
    def test_marks_row_available_with_real_size(self, aws, s3_event, seed_pending):
        import time

        fid = seed_pending(size=3000, created_at=int(time.time()) - 10)

        aws.handlers.process_upload(s3_event(obj_key(USER_ID, fid), size=4567), None)

        row = aws.table.get_item(Key={"userId": USER_ID, "fileId": fid})["Item"]
        assert row["status"] == "AVAILABLE"
        assert row["sizeBytes"] == 4567
        assert row["uploadedAt"] > row["createdAt"]
        assert "declaredBytes" not in row

    def test_publishes_notification_with_event_attribute(self, aws, sns_spy, s3_event,
                                                        seed_pending):
        fid = seed_pending(name="holiday.jpg")

        aws.handlers.process_upload(s3_event(obj_key(USER_ID, fid), size=8192), None)

        sns_spy.publish.assert_called_once()
        kwargs = sns_spy.publish.call_args.kwargs
        assert kwargs["TopicArn"] == aws.topic_arn
        assert "holiday.jpg" in kwargs["Message"]
        assert kwargs["MessageAttributes"]["event"]["StringValue"] == "upload.completed"

    def test_ignores_key_outside_u_prefix(self, aws, sns_spy, s3_event, seed_pending):
        fid = seed_pending()

        result = aws.handlers.process_upload(
            s3_event("backups/someone-else/whatever", size=10), None)

        assert result == {"processed": 1}
        row = aws.table.get_item(Key={"userId": USER_ID, "fileId": fid})["Item"]
        assert row["status"] == "PENDING"
        sns_spy.publish.assert_not_called()

    def test_tolerates_missing_row_without_raising(self, aws, sns_spy, s3_event):
        ghost = str(uuid.uuid4())

        result = aws.handlers.process_upload(s3_event(obj_key(USER_ID, ghost), size=10), None)

        assert result == {"processed": 1}
        sns_spy.publish.assert_not_called()

    def test_continues_after_one_bad_record(self, aws, sns_spy, s3_event, seed_pending):
        good = seed_pending()
        ghost = str(uuid.uuid4())

        result = aws.handlers.process_upload({
            "Records": [
                {"s3": {"object": {"key": obj_key(USER_ID, ghost), "size": 1}}},
                {"s3": {"object": {"key": obj_key(USER_ID, good), "size": 42}}},
            ]
        }, None)

        assert result == {"processed": 2}
        row = aws.table.get_item(Key={"userId": USER_ID, "fileId": good})["Item"]
        assert row["status"] == "AVAILABLE" and row["sizeBytes"] == 42


@pytest.mark.parametrize("aws", [False], indirect=True)
def test_no_sns_client_and_upload_still_completes(aws, s3_event, seed_pending):
        assert aws.handlers._sns is None
        fid = seed_pending()

        result = aws.handlers.process_upload(s3_event(obj_key(USER_ID, fid), size=99), None)

        assert result == {"processed": 1}
        row = aws.table.get_item(Key={"userId": USER_ID, "fileId": fid})["Item"]
        assert row["status"] == "AVAILABLE"
