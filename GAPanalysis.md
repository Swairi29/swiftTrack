# created this after UI polishing

# SwiftTrack — Gap Analysis vs Assignment 4 (verified against the actual repo)

This reviews the repo as uploaded, checked line-by-line against `swiftTrack-main`'s
own code — not just against the assignment PDF. A `ROADMAP.md` already exists in
the repo and, on independent verification, its claims check out: the `[x]`/`[ ]`
items match what the code actually does. This file is a **companion, not a
replacement** — it confirms what's accurate, and adds a few things the existing
roadmap missed.

## Confirmed accurate from the existing ROADMAP.md

Spot-checked directly against the code:

- `GET /orders/<order_id>` in `order-service/app.py` has no `@require_auth` — confirmed, any order ID can be looked up unauthenticated.
- `notification-service/app.py`'s `socketio.emit("update", ...)` broadcasts to every connected socket with no room/scoping — confirmed, and `client-portal/index.html`'s `applyEvent()` adds _every_ incoming order to its table with no filter by the logged-in client. This is a real cross-tenant leak, not just a theoretical one.
- `cors_allowed_origins="*"` in `notification-service/app.py` and `flask_cors.CORS(app)` (unscoped) in `order-service/app.py` — both confirmed present.
- `SECRET_KEY = "swifttrack-dev-secret"` in `order-service/auth.py`, and `swift`/`swift123` repeated across `db.py`, `worker.py`, `docker-compose.yml` — confirmed hardcoded, not environment-driven.
- `docker-compose.yml` only defines `rabbitmq` and `postgres` — `order-service`, `saga-worker`, all three mocks, and `notification-service` still run as bare `python x.py`, confirmed.
- No dead-letter handling: `compensate_cms()` / `compensate_wms()` in `saga-worker/worker.py` still swallow their own exceptions with a bare `except: pass`.
- No automated tests anywhere in the repo — confirmed, `grep`-ing for `pytest`/`unittest` returns nothing.

Nothing in the above needed correction — good, verified work by whoever wrote it.

## What the existing roadmap missed

### 1. Dual-write risk between Postgres and RabbitMQ (not mentioned anywhere)

`order-service/app.py`'s `submit_order()` commits the Postgres insert, _then_
separately calls `publish_event("order.created", ...)` afterwards, outside that
transaction. If the process crashes between the commit and the publish, an
order is stuck at `PENDING` forever with no event ever raised to trigger the
Saga Worker — silent data loss that contradicts the brief's "an order must
never be lost" requirement. This is the textbook case for a **transactional
outbox**: write the event to an outbox table in the same transaction as the
order, and have a separate relay process publish from that table. Given the
timeline, this is worth stating as an explicit, named limitation in the
security/architecture write-up rather than fixing outright — but it should be
named, since it's a real gap the roadmap's own "Prototype gaps" section didn't
catch.

### 2. Scaling the Saga Worker needs a quick safety check first

The roadmap suggests demonstrating scalability with `--scale saga-worker=3`
on the demo call (section 2). Worth flagging before that goes in the
screencast: `process_order()`'s idempotent-consumer guard is a **check-then-act**
(`SELECT status`, then later `UPDATE`) with no atomic claim on the row. Under
normal operation with `prefetch_count=1`, RabbitMQ won't hand the same message
to two consumers at once, so this is unlikely to misfire in a short demo — but
it hasn't actually been tested under multiple replicas. Two safe options:
either test the scaled demo once beforehand to confirm no double-processing
shows up, or replace the guard with an atomic claim (`UPDATE orders SET
status='PROCESSING' WHERE order_id=%s AND status='PENDING'`, checking
`rowcount`) before relying on `--scale` live on camera.

### 3. The documentation and screencast script already exist — they're just not in this repo yet

`ROADMAP.md` says "Nothing in the repo yet addresses this" for the
documentation deliverable. A full draft covering all five graded sections
(introduction, conceptual + implementation architecture with two alternatives,
patterns, prototype, security) already exists from earlier work on this
assignment, along with a timed screencast narration script. Two things worth
doing rather than starting from zero:

- Add the existing documentation draft into the repo (a `docs/` folder is a
  reasonable place) and update it for what's changed since it was written —
  it currently describes the plain test client, not the real client-portal
  and driver-app UIs now in the repo, which are a meaningfully stronger demo
  than what the doc assumes.
- Update the screencast narration the same way — it currently scripts the
  demo entirely through `curl`, but demoing through the actual client-portal
  and driver-app UIs (submitting an order in the browser, clicking "Mark
  delivered" in the driver app) will read as more polished on camera than a
  terminal.

### 4. Minor polish, not worth much time

- The client portal/driver app login form only collects a username (no
  password field), while the README's `curl` example still shows a
  `password` field in the request body. Harmless since auth is mocked either
  way, but worth making consistent so it doesn't look like an oversight.
- A `.claude/scheduled_tasks.lock` file is present in the uploaded content.
  It's already correctly listed in `.gitignore`, so it's likely just a local
  artifact that was never actually committed — worth a quick `git status`
  check to confirm it isn't tracked, rather than assuming.

## Suggested priority given the timeline

The existing roadmap's own "Suggested order of attack" is sound and doesn't
need changing. Slot the two new items in as follows: add the dual-write note
to the security/architecture write-up when documentation work starts (item 1
in the roadmap's order of attack), and do the scaling safety check
immediately before recording the screencast, not before — no reason to spend
time on it earlier than it's needed.
