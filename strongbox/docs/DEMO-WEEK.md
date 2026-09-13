# Demo Week notes

The brief asks each group to cover five things: what was completed, what was
not finished, challenges faced, what you enjoyed, and suggestions for
improvement. This file is a skeleton — replace the bracketed parts with what
your team actually experienced. Assessors can tell the difference.

---

## Live demo running order (aim for 6 minutes)

1. **Open the CloudFront URL.** Point out it is HTTPS on a private S3 bucket —
   nothing is publicly readable, CloudFront reaches the bucket through Origin
   Access Control.
2. **Create an account.** Show the verification code arriving. This proves
   Cognito is doing real email verification, not a stub.
3. **Open dev tools, network tab, and upload a file.** This is the moment worth
   slowing down for. Two requests appear: a small `POST` to your API, then a
   `PUT` going straight to `s3.amazonaws.com`. Say out loud that the file bytes
   never touched Lambda.
4. **Watch the row flip from "waiting" to stored.** That transition is the S3
   event notification firing the second Lambda. Show the notification email
   landing.
5. **Download and delete.** Mention the download link expires in five minutes.
6. **Sign in as a second user.** Empty vault. This demonstrates tenant
   isolation rather than just claiming it.
7. **Close on the template.** Scroll `template.yaml` briefly. Say the whole
   stack can be destroyed and rebuilt from this one file.

Have a screen recording of this as a fallback. Live demos on conference wifi
fail often enough that a backup is not pessimism.

---

## What was completed

- Authenticated API on API Gateway with a Cognito JWT authoriser
- Lambda + DynamoDB backend with per-user data isolation
- Asynchronous processing on S3 object creation, with SNS notification
- Static web app on S3 behind CloudFront with Origin Access Control
- Entire stack as Infrastructure as Code in a single SAM template
- Per-user storage quota and per-file size cap
- IAM roles scoped per function to a single table and a single key prefix

## What was not finished

Be specific and honest here. Blank or vague answers cost marks; a clear gap
list with reasoning scores well. Genuine gaps in this build:

- CORS is open to `*` on both the API and the files bucket rather than pinned
  to the CloudFront domain
- Handler unit tests exist (`pytest` with moto mocks) but there is no
  integration test against a deployed stack — `scripts/smoke_test.py` covers
  that manually after each deploy
- No CI/CD pipeline; deployment is a shell script run by hand
- No file previews, thumbnails, folders, sharing, or versioning
- Cognito Hosted UI was not used, so there is no password reset flow in the app
- `[whatever your team ran out of time for]`

## Challenges

Candidates from this build that you can speak to honestly:

- **The circular dependency.** Wiring an S3 event to a Lambda while that same
  Lambda holds the bucket name in an environment variable makes CloudFormation
  refuse to deploy. The fix was to name the bucket explicitly and pass the name
  as a string rather than a `!Ref`. Explaining this well shows you understand
  how CloudFormation builds its dependency graph.
- **Presigned URL signatures.** They must be generated with SigV4 and the
  `Content-Type` sent by the browser has to match the one used when signing, or
  S3 returns 403 with no useful explanation.
- **CORS on the files bucket.** The browser PUTs directly to S3, so the bucket
  needs its own CORS rules. API Gateway CORS does not cover it.
- **The eventual gap.** After the PUT succeeds, the DynamoDB row is not yet
  `AVAILABLE`. Refreshing the list immediately shows stale state. The client
  waits briefly; a more robust answer is polling or a websocket.
- `[what actually cost your team the most time]`

## What we enjoyed

`[Write this in your own words — it is the one section that cannot be
generic. Something real: the moment auth worked end to end, seeing the first
notification email arrive, watching a teardown and rebuild reproduce the whole
system from one file.]`

## Suggestions for improvement

**To the application:**
- Pin CORS to the CloudFront domain
- Add a CI/CD pipeline (this is exactly the GitLab + CloudFormation pattern
  from the WeShare practical in Iteration 2)
- Move upload processing behind SQS so failures retry with a dead-letter queue
- Add CloudWatch alarms on Lambda errors and a dashboard for upload volume
- Server-side encryption with a customer-managed KMS key instead of SSE-S3

**To the course:**
`[Your team's honest feedback on the iterations, the workshops, and the
AWS account access.]`

---

## Likely questions

**Why not upload through Lambda?** The 6 MB payload limit, and cost. Streaming
a 100 MB file through Lambda means holding memory for the duration of the
transfer, which the user's connection speed controls, not you.

**How do you know one user cannot read another's files?** The `userId` comes
from the `sub` claim in a JWT that API Gateway verified against the Cognito
JWKS before Lambda was invoked. It is not read from the body or the path. The
Lambda constructs the S3 key from it, and the IAM policy only grants access
under the `u/` prefix.

**What happens if the browser dies mid-upload?** The row stays `PENDING`
forever and counts against quota. A scheduled EventBridge rule sweeping rows
older than a day is the fix; it is not built.

**Why DynamoDB rather than RDS?** Access pattern is a single query on
`userId`, no joins, spiky traffic. On-demand billing costs nothing when idle,
which suits a system that is dormant most of the time.

**What does this cost to run?** Effectively nothing under the free tier at
demo scale. The standing costs outside the free tier are S3 storage and
CloudFront data transfer — both proportional to actual use. Nothing here is
billed per hour, which is the point of the serverless model.
