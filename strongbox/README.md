WTC-K82CLCED
# Strongbox

A serverless self-storage application. Users sign up, upload files to a private
vault, download them again through short-lived links, and delete them. Every
piece of infrastructure is defined in one CloudFormation template — nothing is
clicked in the console.

Built for the Cloud Computing 2 capstone (Iteration 3).

---

## How it fits together

```
                   ┌──────────────┐
  browser ────────▶│  CloudFront  │──▶ S3 (web bucket, private, OAC only)
     │             └──────────────┘
     │
     │ 1. sign in                    ┌──────────────┐
     ├──────────────────────────────▶│   Cognito    │  returns id token (JWT)
     │                               └──────────────┘
     │
     │ 2. POST /files/upload-url  (Authorization: Bearer <JWT>)
     │             ┌──────────────┐        ┌──────────────┐
     ├────────────▶│ API Gateway  │───────▶│    Lambda    │──▶ DynamoDB (PENDING)
     │             │ HTTP API     │        │ create-upload│
     │             │ JWT authoriser        └──────┬───────┘
     │             └──────────────┘               │ presigned PUT url
     │◀────────────────────────────────────────────┘
     │
     │ 3. PUT file bytes  ─────────────────▶  S3 (files bucket)
     │                                              │
     │                                              │ ObjectCreated event
     │                                              ▼
     │                                    ┌──────────────────┐
     │                                    │ Lambda           │
     │                                    │ process-upload   │
     │                                    └────┬─────────┬───┘
     │                                         │         │
     │                        DynamoDB (AVAILABLE)      SNS ──▶ email
     │
     │ 4. GET /files  ──▶ Lambda ──▶ DynamoDB query on userId
```

**The upload path is the design decision worth defending in the demo.** File
bytes never enter Lambda. The API returns a presigned S3 URL and the browser
uploads directly. That avoids the 6 MB API Gateway payload ceiling, keeps
Lambda duration flat regardless of file size, and cuts compute cost to
near zero for the expensive part of the workload.

### Tenant isolation

The S3 key is `u/{userId}/{fileId}` and the DynamoDB partition key is `userId`.
Both come from the `sub` claim of the JWT that API Gateway already verified —
never from anything in the request body. A user physically cannot address a key
or a partition belonging to someone else, because they cannot forge the claim.

### Resources created

| Resource | Purpose |
|---|---|
| Cognito user pool + client | Sign up, email verification, sign in |
| HTTP API + JWT authoriser | Rejects unauthenticated calls before Lambda runs |
| 4 request Lambdas | list, upload-url, download-url, delete |
| 1 event Lambda | Reacts to `s3:ObjectCreated`, updates state, publishes SNS |
| DynamoDB table | File metadata, on-demand billing, encrypted, PITR on |
| S3 files bucket | Object storage, all public access blocked |
| S3 web bucket | Static site, readable only by CloudFront via OAC |
| CloudFront distribution | HTTPS, compression, edge caching |
| SNS topic + subscription | Upload notifications by email |

---

## Deploy

Prerequisites: AWS CLI v2, SAM CLI, Python 3.12, and an authenticated session
(`aws configure` or the AWS Academy credentials).

```bash
chmod +x scripts/*.sh
./scripts/deploy.sh your.email@example.com strongbox eu-west-1
```

The script builds, deploys, writes `web/config.js` from the stack outputs,
syncs the web app to S3 and invalidates the CloudFront cache. It prints the
site URL when it finishes.

**Confirm the SNS subscription email before demoing.** AWS will not deliver
notifications until you click the link.

Tear everything down when you are finished:

```bash
./scripts/teardown.sh strongbox eu-west-1
```

---

## API

All routes require `Authorization: Bearer <cognito id token>`.

| Method | Path | Returns |
|---|---|---|
| `GET` | `/files` | Files owned by the caller, plus quota usage |
| `POST` | `/files/upload-url` | `{ fileId, uploadUrl }` — presigned PUT, valid 15 min |
| `GET` | `/files/{fileId}/download-url` | `{ downloadUrl }` — presigned GET, valid 5 min |
| `DELETE` | `/files/{fileId}` | Removes the object and the metadata row |

`POST /files/upload-url` body:

```json
{ "name": "notes.pdf", "sizeBytes": 84213, "contentType": "application/pdf" }
```

### File states

`PENDING` — a row exists but the browser has not finished the PUT.
`AVAILABLE` — the S3 event fired and the row was updated with the real size.

A row stuck on `PENDING` is a failed or abandoned upload. This is deliberate:
it makes the asynchronous leg visible in the interface during the demo, and
it means the metadata row is written before the object rather than after.

---

## Parameters

| Parameter | Default | Notes |
|---|---|---|
| `ProjectName` | `strongbox` | Prefixes every resource name |
| `NotifyEmail` | — | Required. Receives upload notifications |
| `StorageQuotaBytes` | 2 GiB | Per-user cap, enforced before the URL is issued |

Per-file cap is 100 MB, set in `src/handlers.py`.

---

## Tests

Unit tests cover all five handlers with AWS calls mocked by moto:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt   # POSIX: .venv/bin/pip
.venv/Scripts/python -m pytest                      # POSIX: .venv/bin/python
```

They validate tenant isolation, the PENDING→AVAILABLE transition, quota
accounting, presigned-URL signing, and the error mapping in the shared
wrapper. The presigned URLs themselves are exercised against real AWS by
`scripts/smoke_test.py` after a deploy.

---

## Known limits

- CORS on the API and the files bucket allows any origin. For a production
  deployment, narrow both to the CloudFront domain. It is left open so the app
  still works if you test the frontend from `localhost`.
- `USER_PASSWORD_AUTH` sends the password to Cognito over TLS from the browser.
  Cognito Hosted UI with the authorization-code flow is the stronger option;
  this flow was chosen so the app stays three static files with no build step.
- Quota is enforced at URL-issue time. A user could request many URLs at once
  and slightly overshoot. Pending rows are counted at their declared size to
  narrow that window.
- No file versioning or recycle bin. Delete is immediate and final.
