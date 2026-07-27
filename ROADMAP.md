# SwiftTrack — Roadmap to submission

Deadline: **20 August 2026**. This tracks what's left, grouped by how it's
graded (Assignment 4, section 5) so work can be split across the team.
Check off items as they land; keep this file itself out of the final
source-code zip if it feels too "meta" for submission.

## 1. Documentation deliverable — not started, worth as much as the code

Nothing in the repo yet addresses this. It's the single biggest remaining
risk since it's ungraded by demo, only by the PDF.

- [ ] **Introduction** to the solution (1a)
- [ ] **Conceptual + implementation architecture diagrams** (1b) — draw
      what's actually built: Order Service, Saga Worker, RabbitMQ topic
      exchange, Postgres, the three mocks, Notification Service, WebSocket
      client
- [ ] **Two alternative architectures** with rationale for why the current
      one (event-driven orchestration via a Saga Worker) was chosen over
      them (1b) — e.g. a synchronous API-Gateway-orchestrates-everything
      design, and a pure choreography (no central Saga Worker, each service
      reacts to events) design
- [ ] **Architectural & integration patterns write-up** (1c): name and
      justify each pattern actually in the code —
      Orchestrated Saga + compensation (`saga-worker/worker.py`),
      Circuit Breaker (`pybreaker` around the ROS call),
      Competing Consumers (RabbitMQ queue + manual ack),
      Publish-Subscribe / topic exchange (`swifttrack` exchange, `order.#`
      / `delivery.#` routing keys),
      Idempotent Consumer (status check before reprocessing),
      Protocol/data translation adapters (XML for CMS, TCP/JSON for WMS)
- [ ] **Security considerations list** (1e) — write up both what's done
      (JWT, Idempotency-Key) and what's explicitly out of scope for the
      prototype (see section 3 below) — an honest list here is worth more
      than pretending there are no gaps
- [ ] Index numbers of contributing group members

## 2. Prototype gaps against the brief's six challenges

- [x] Heterogeneous integration (CMS/ROS/WMS protocol translation)
- [x] Real-time tracking/notifications (WebSocket broadcast)
- [x] High-volume async processing (202-immediately + queue)
- [x] Transaction management (Saga compensation in `run_saga()`)
- [ ] **Scalability** — app services aren't containerized. Only
      `postgres` and `rabbitmq` are in `docker-compose.yml`; order-service,
      saga-worker, the three mocks, and notification-service still run as
      bare `python x.py` processes. At minimum, add a `Dockerfile` per
      service and bring them into `docker-compose.yml` so the "scale to
      more instances" story is demonstrable, even if only with
      `--scale saga-worker=3` on the demo call.
- [ ] **Resilience** — no retry/backoff on WMS (a dropped TCP connection
      currently just fails the saga once), and no health-check-based
      restart policy in compose
- [ ] Dead-letter handling for compensation calls that themselves fail
      (already flagged as a `# TODO Week 4` in `saga-worker/worker.py`)

## 3. Security hardening (currently JWT-protected on two endpoints only)

- [ ] `GET /orders/<order_id>` has no auth — anyone can look up any order
      by guessing/enumerating IDs
- [ ] The WebSocket in `notification-service` has **no auth or scoping at
      all** — it broadcasts every client's order/delivery events to every
      connected socket. This is a real cross-tenant data leak in a
      multi-client-portal design and is worth fixing or explicitly
      disclosing in the security write-up (per-client rooms keyed off the
      JWT `sub` claim would close it)
- [ ] `cors_allowed_origins="*"` in `notification-service/app.py` — fine
      for local dev, call it out as a prototype simplification
- [ ] Hardcoded credentials (`swift`/`swift123`, `SECRET_KEY =
      "swifttrack-dev-secret"`) repeated across `db.py` / `worker.py` /
      `auth.py` — move to environment variables before anyone records the
      screencast with these visible on screen
- [ ] No TLS anywhere (fine to state as an accepted prototype limitation —
      real deployment would terminate TLS at a gateway/load balancer)
- [ ] No rate limiting on `/orders` — relevant given the brief's own
      "Black Friday / Avurudu" high-volume framing

## 4. UI polish (do this last, once the backend items above are stable)

`test-client/index.html` is a bare read-only test page, not the two
things the brief actually asks for:

- [ ] **Client Portal**: needs a real login form (hits `/login`, stores
      the JWT, sends it as `Authorization: Bearer ...`), an order
      submission form (currently only demoed via `curl`), and the existing
      live-status table wired to use the token
- [ ] **Driver Mobile App** — currently has *zero* UI. The backend only
      has `/deliveries/<order_id>/status`. The brief calls for: viewing
      the assigned manifest/route, marking delivered/failed with a reason,
      and capturing a signature or photo as proof of delivery. Per the
      brief this can be "architecturally described but only minimally
      implemented" for the real-time piece — but the manifest view +
      delivered/failed action should exist as at least a simple mobile-
      width web page, since it's a named functional requirement, not just
      part of the real-time-tracking allowance
- [ ] General polish once functional: consistent styling between the two
      UIs, loading/error states, a favicon, README screenshots for the
      documentation

## 5. Testing (currently none)

- [ ] No automated tests exist anywhere in the repo — only manual `curl`
      steps in the README. At minimum, a handful of `pytest` cases against
      `order-service` (auth required, idempotency dedupes, validation
      errors) and an integration test that runs a full order through the
      saga (success + one compensation path) would substantially
      de-risk the live demo in the screencast.

## Suggested order of attack (given ~3.5 weeks to 20 Aug)

1. Documentation skeleton (section 1) — start this now, in parallel with
   code, so diagrams reflect reality rather than being reverse-engineered
   at the end
2. Security fixes that are cheap (auth on `GET /orders/<id>`, env vars for
   secrets) — leave the WebSocket-scoping fix for whoever has bandwidth
3. Containerize the remaining services (section 2) so the scalability
   story is demoable
4. UI pass (section 4) — only after the above is stable, since UI work is
   the most visible in the screencast and easiest to redo last
5. Tests (section 5) alongside/after UI, focused on what you'll actually
   demo live
