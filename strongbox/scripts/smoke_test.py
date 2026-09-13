#!/usr/bin/env python3
"""End-to-end check against a deployed stack. Standard library only.

    python3 scripts/smoke_test.py you@example.com 'YourPassw0rd'

Reads web/config.js for the endpoint details, then walks the full path:
sign in, request an upload URL, PUT to S3, wait for the async handler to
mark the file AVAILABLE, download it back, compare bytes, delete it.

Run this before Demo Week. It catches a broken deploy in 30 seconds
instead of in front of the assessors.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = b"Strongbox smoke test. If you can read this, the round trip works.\n" * 64


def load_config() -> dict:
    path = os.path.join(ROOT, "web", "config.js")
    with open(path) as fh:
        text = fh.read()
    cfg = {}
    for key in ("region", "userPoolClientId", "apiBaseUrl"):
        match = re.search(key + r'\s*:\s*"([^"]+)"', text)
        if not match or match.group(1).startswith("REPLACE_"):
            sys.exit(f"config.js has no real value for {key}. Run scripts/deploy.sh first.")
        cfg[key] = match.group(1)
    return cfg


def post_json(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")
        sys.exit(f"  FAILED {url}\n  HTTP {err.code}: {detail}")


def api(cfg, path, token, method="GET", body=None):
    headers = {"Authorization": "Bearer " + token}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(cfg["apiBaseUrl"] + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")
        sys.exit(f"  FAILED {method} {path}\n  HTTP {err.code}: {detail}")


def step(n, text):
    print(f"[{n}] {text}", flush=True)


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    email, password = sys.argv[1], sys.argv[2]
    cfg = load_config()

    step(1, "Signing in to Cognito")
    auth = post_json(
        f"https://cognito-idp.{cfg['region']}.amazonaws.com/",
        {
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": cfg["userPoolClientId"],
            "AuthParameters": {"USERNAME": email, "PASSWORD": password},
        },
        {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        },
    )
    token = auth["AuthenticationResult"]["IdToken"]
    print("    got an id token")

    step(2, "Checking the API rejects an unauthenticated call")
    try:
        urllib.request.urlopen(cfg["apiBaseUrl"] + "/files", timeout=15)
        sys.exit("    FAILED: /files answered without a token. The authoriser is not attached.")
    except urllib.error.HTTPError as err:
        if err.code not in (401, 403):
            sys.exit(f"    FAILED: expected 401/403, got {err.code}")
        print(f"    rejected with {err.code}, correct")

    step(3, "Requesting a presigned upload URL")
    grant = api(cfg, "/files/upload-url", token, "POST", {
        "name": "smoke-test.txt",
        "sizeBytes": len(PAYLOAD),
        "contentType": "text/plain",
    })
    file_id = grant["fileId"]
    print(f"    fileId {file_id}")

    step(4, "Uploading straight to S3")
    put = urllib.request.Request(
        grant["uploadUrl"], data=PAYLOAD, method="PUT",
        headers={"Content-Type": "text/plain"},
    )
    try:
        with urllib.request.urlopen(put, timeout=60) as res:
            print(f"    S3 returned {res.status}")
    except urllib.error.HTTPError as err:
        sys.exit(f"    FAILED: S3 returned {err.code}\n{err.read().decode(errors='replace')}")

    step(5, "Waiting for the S3 event handler to mark it available")
    deadline = time.time() + 30
    record = None
    while time.time() < deadline:
        listing = api(cfg, "/files", token)
        record = next((f for f in listing["files"] if f["fileId"] == file_id), None)
        if record and record["status"] == "AVAILABLE":
            print(f"    available after {record['sizeBytes']} bytes recorded")
            break
        time.sleep(2)
    else:
        sys.exit("    FAILED: still PENDING after 30s. Check the process-upload logs.")

    if int(record["sizeBytes"]) != len(PAYLOAD):
        sys.exit(f"    FAILED: size mismatch, {record['sizeBytes']} != {len(PAYLOAD)}")

    step(6, "Downloading through a presigned GET")
    link = api(cfg, f"/files/{file_id}/download-url", token)
    with urllib.request.urlopen(link["downloadUrl"], timeout=60) as res:
        got = res.read()
    if got != PAYLOAD:
        sys.exit("    FAILED: downloaded bytes do not match what was uploaded")
    print(f"    {len(got)} bytes match")

    step(7, "Deleting")
    api(cfg, f"/files/{file_id}", token, "DELETE")
    listing = api(cfg, "/files", token)
    if any(f["fileId"] == file_id for f in listing["files"]):
        sys.exit("    FAILED: the row is still listed after delete")
    print("    gone")

    used = listing["usage"]["usedBytes"]
    quota = listing["usage"]["quotaBytes"]
    print(f"\nAll seven checks passed. Vault holds {used} of {quota} bytes.")


if __name__ == "__main__":
    main()
