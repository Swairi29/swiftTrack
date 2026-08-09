# SwiftTrack security & correctness patch — apply notes

**Verified against your actual uploaded repo.** Every file this patch touches
matched my reconstruction exactly, character-for-character - `order-service/`,
`saga-worker/`, `mock-cms/app.py`, `notification-service/app.py`,
`docker-compose.yml`, and `init.sql` were all checked line-by-line against the
real code. The only thing the real repo revealed that wasn't obvious before:
your `README.md` recommends opening `client-portal/index.html` and
`driver-app/index.html` directly as a `file://` URL, not necessarily through a
local server - see the CORS note below, since that changes what "scope CORS"
actually means in practice.

## What changed and why

| File | Change | Rubric item fixed |
|---|---|---|
| `saga-worker/worker.py` | Atomic claim (`UPDATE ... WHERE status='PENDING'`) before running the saga, instead of a read-then-check guard | **The crash-mid-saga duplicate-processing bug** - the most important fix in this patch |
| `saga-worker/worker.py` | Compensation failures now publish to a `compensation.dlq` queue instead of `except: pass` | Closes the explicit `# TODO` for dead-letter handling |
| `order-service/app.py` | `GET /orders/<id>` now requires `@require_auth` | Closes the unauthenticated order-lookup gap |
| `order-service/app.py`, `notification-service/app.py` | CORS scoped to `ALLOWED_ORIGINS` from environment, not `*` | Closes the wide-open CORS gap |
| `mock-cms/app.py`, `saga-worker/worker.py` | XML parsing via `defusedxml` instead of stdlib `xml.etree.ElementTree` | Closes the XXE risk |
| `order-service/db.py`, `saga-worker/db.py`, `order-service/auth.py`, `order-service/app.py`, `notification-service/app.py` | All credentials/secrets read from environment via `python-dotenv`, no hardcoded values | Closes the hardcoded-secrets gap |
| `.env.example` (new) | Template for the real `.env` file (already gitignored) | - |
| `requirements.txt` | Added `flask-cors`, `python-dotenv`, `defusedxml` | - |

## Important: CORS and the file:// workflow

Your README's primary instructions are to open the two UI files directly
(`file://...`), not necessarily through a local server. Browsers send a
literal `Origin: null` header for `file://` pages - this is a real string,
not a missing header - so `.env.example` now sets:

```
ALLOWED_ORIGINS=null,http://localhost:5500,http://127.0.0.1:5500
```

`null` covers the file:// workflow your README documents as primary;
the `:5500` entries cover the Live Server alternative it also mentions.
If everyone on the team always uses one or the other, you can trim this
list down - narrower is better than broader once you know which workflow
you've settled on.

## Required database migration

The atomic claim needs a `claimed_at` column that didn't exist before.
If your Postgres volume already has data in it, run this once against
the running database rather than recreating it from `init.sql`:

```sql
ALTER TABLE orders ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMP;
```

Via docker exec, for example:

```bash
docker exec -it swifttrack-postgres psql -U swift -d swifttrack \
  -c "ALTER TABLE orders ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMP;"
```

If you're comfortable dropping local data and starting fresh instead,
just add the same column to `init.sql`'s `CREATE TABLE orders (...)`
block and recreate the Postgres volume.

## Setup step this patch adds

Before running any service:

```bash
cp .env.example .env
# then edit .env and set a real JWT_SECRET_KEY, e.g.:
python -c "import secrets; print(secrets.token_hex(32))"
```

`.env` should already be covered by `.gitignore` - confirm that before
committing anything.

## How to verify the main fix (crash-mid-saga)

This is the one worth actually testing, not just reading:

1. Submit an order as normal.
2. While the Saga Worker is mid-processing (hard to catch by hand, but
   you can approximate it), or more reliably: manually set the order back
   to `PENDING` in Postgres and republish an `order.created` event with
   the same `orderId` - the Saga Worker should now either skip it (if
   still marked `PROCESSING` and not yet stale) or safely reclaim and
   reprocess it (if genuinely abandoned past the 2-minute staleness
   window) - but it should never run CMS/WMS/ROS a second time while a
   healthy claim is in effect.
3. Confirm in the mock CMS/WMS logs that a given `orderId` never
   produces two separate CMS orders or WMS packages.

## Not included in this patch (next phase)

Per the implementation plan: Dockerfiles for all five Python services,
retry/backoff on the WMS call, service-discovery documentation, and
per-client WebSocket room scoping. These are larger or lower-urgency
changes, tracked separately.
