# S3 source-video retention: recommended policy (not implemented)

Status: proposal. Nothing in the code or bucket applies this yet. It needs owner approval, and an S3 lifecycle
change needs its own review.

## Current behaviour
- Each job uploads its source video to the bucket root under a random UUID key (`<uuid>.mp4`).
- After the worker finishes, the source is deleted (`status: deleted`).
- If the worker fails, the source is kept for retry (`status: retained_after_failure`), and the key is recorded in
  the job folder's `s3_video.json`. Nothing removes it later, so these objects accumulate.
- Highlights are uploaded under `highlights/<uuid>/<lang>.mp4`, and their keys are recorded per job. They are
  never deleted, so History keeps working.

## Recommended policy
1. **Move temporary sources under a `sources/` prefix** for new uploads, so a lifecycle rule can target only them.
   Keep `highlights/` untouched.
2. **Add an S3 lifecycle expiration rule** scoped to the `sources/` prefix, with 7 days.
3. **Add abort-incomplete-multipart-upload** cleanup after 1 day for the whole bucket.
4. **Keep the job record truthful.** When a source expires, its record keeps its key and status, so a retry can
   tell that the source is gone and fail clearly.
5. **Do not apply this to highlights.** Retain them until the owner sets a separate policy with a test.

## Existing objects
- Root-level source keys from before this policy (for example `defacc2c-cd3c-4af2-891a-97449d215e33.mp4`, from the
  failed job `b7e0dd08`) are not covered by a prefix rule. Delete them one at a time, with owner approval, after
  checking that their job record says `retained_after_failure`.

## Cost note
A source is about 1–2 MB for the smoke clip. Pricing has not been verified, so no cost is stated here.
