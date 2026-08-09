# SwiftTrack — Documentation Brief

**Purpose of this file**: a complete, accurate factual reference to the
codebase as it actually exists, organized around Assignment 4's grading
criteria (section 5 of the brief). Paste this file's content (plus the
source code zip) into a fresh Claude chat and ask it to draft the
Solution Documentation PDF from it — every claim below is verified
against the code, not guessed, so it's safe to build diagrams and
prose directly on top of it. This file itself is a working aid, not
part of the submission.

---

## 1. One-paragraph system summary

SwiftTrack is an event-driven middleware platform that integrates three
heterogeneous backend systems — a SOAP/XML Client Management System
(CMS), a REST/JSON Route Optimisation System (ROS), and a proprietary
TCP messaging Warehouse Management System (WMS) — behind a thin, always-
responsive Order Service. Orders are accepted, persisted, and
acknowledged in under a second regardless of backend latency; the actual
cross-system work is carried out asynchronously by a separate Saga
Worker process that orchestrates CMS → WMS → ROS, compensates (unwinds)
whatever already succeeded if a later step fails, and publishes progress
events over RabbitMQ. A Notification Service relays every event to
connected browsers over WebSocket, so a Client Portal and a Driver App
both see order and delivery status change live, with no polling.

---

## 2. Component inventory

| Component | File(s) | Tech | Port / Protocol | Responsibility |
|---|---|---|---|---|
| Order Service | `order-service/app.py`, `auth.py`, `db.py` | Flask, psycopg2, pika, PyJWT, flask-cors | HTTP :5000 | Validate + persist orders as `PENDING`, publish `order.created`, return `202` immediately. Issues JWTs (`/login`). Handles driver delivery-status updates. Deliberately does **not** call CMS/WMS/ROS itself. |
| Saga Worker | `saga-worker/worker.py`, `db.py` | pika (consumer), requests, pybreaker | No HTTP port — RabbitMQ consumer | Consumes `order.created`, runs CMS → WMS → ROS in sequence, compensates on failure, updates Postgres, publishes `order.completed`/`order.failed`. Runs as an independent OS process from the Order Service. |
| Mock CMS | `mock-cms/app.py` | Flask | HTTP :5001 | Stands in for the legacy SOAP/XML CMS. Parses real order XML, returns an XML `OrderResponse`. Has a `/cms/order/cancel` compensation endpoint. |
| Mock ROS | `mock-ros/app.py` | Flask | HTTP :5002 | Stands in for the cloud REST/JSON route optimiser. Returns a stub optimized route. Has a `/routes/toggle-failure` switch to simulate an outage for demoing the circuit breaker/compensation. |
| Mock WMS | `mock-wms/server.py` | raw `socket` server, threaded | TCP :6000 | Stands in for the proprietary TCP messaging WMS. Newline-delimited JSON, one line in → one line out. Handles both package registration and compensation (`cancelPackageId`). |
| Notification Service | `notification-service/app.py` | Flask-SocketIO | HTTP/WS :5003 | Subscribes to all `order.#` and `delivery.#` events on the broker and re-broadcasts them to every connected WebSocket client in real time. |
| Client Portal | `client-portal/index.html` | Static HTML/JS, Socket.IO client | Opened as a local file | Client-facing UI: login, submit an order, watch a live table of order/delivery status. |
| Driver App | `driver-app/index.html` | Static HTML/JS, Socket.IO client | Opened as a local file | Driver-facing UI: login, a live manifest of confirmed-but-undelivered orders, mark delivered/failed. |
| RabbitMQ | `docker-compose.yml` | RabbitMQ 3 (management image) | AMQP :5672, UI :15672 | Message broker — topic exchange named `swifttrack`. |
| Postgres | `docker-compose.yml`, `init.sql` | Postgres 16 | :5432 | System of record for orders and idempotency keys. |

---

## 3. End-to-end flows

### 3.1 Happy-path order submission
1. Client Portal `POST /login` → gets JWT.
2. Client Portal `POST /orders` with `Authorization: Bearer <jwt>` and a
   client-generated `Idempotency-Key` header.
3. Order Service checks `idempotency_keys` (no match), inserts the order
   as `PENDING` into `orders`, records the idempotency key, publishes
   `order.created` to the `swifttrack` exchange, and returns `202` with
   `{orderId, status: "PENDING"}` — **before any backend call has
   happened.**
4. Saga Worker (separate process, consuming the durable
   `saga-worker.order-created` queue) picks up `order.created`.
5. `run_saga()` calls CMS (builds real XML from the order, parses the
   XML `OrderId` back), then WMS (TCP JSON line, gets a `packageId`
   back), then ROS (REST JSON, gets a `routeId` + `estimatedMinutes`
   back).
6. On success: Postgres row updated to `CONFIRMED` with all three
   returned IDs; `order.completed` published.
7. Notification Service relays every one of these events over WebSocket;
   Client Portal and Driver App update live.

### 3.2 Failure + compensation path
1. Same as above through step 4.
2. Say CMS and WMS succeed but ROS fails (e.g. `/routes/toggle-failure`
   is on, or the circuit breaker is open).
3. `run_saga()` catches the ROS failure, calls `_compensate()`, which
   calls WMS's cancel operation (`{"cancelPackageId": ...}` over TCP)
   and then CMS's `/cms/order/cancel` (undo in **reverse order** of
   creation).
4. Postgres row updated to `FAILED` with `failed_step="ros"` and a
   `failure_reason`; `order.failed` published with the failed step
   included.
5. If ROS has failed 3 times consecutively, `pybreaker` trips the
   circuit: further calls fail fast with `CircuitBreakerError` for 20s
   instead of waiting on a hung/unavailable ROS.

### 3.3 Idempotent resubmission (client-side retry safety)
- A repeated `POST /orders` with the **same** `Idempotency-Key` short-
  circuits before any insert/publish: it looks up the existing
  `order_id` in `idempotency_keys` and returns that order's current
  status. No duplicate order, no duplicate `order.created` event. This
  survives an Order Service restart because it's backed by Postgres, not
  an in-memory cache.

### 3.4 Idempotent consumer (broker-side redelivery safety)
- RabbitMQ delivery is manual-ack (`ch.basic_ack` only after
  `process_order()` returns). If the Saga Worker crashes mid-order, the
  message is redelivered to another/restarted worker rather than lost.
  Before re-running the saga, `process_order()` checks the order's
  current status in Postgres and skips it if it has already moved past
  `PENDING` — prevents double-processing an already-completed order.

### 3.5 Real-time delivery status push
1. Driver App `POST /deliveries/<orderId>/status` (JWT-protected) with
   `DELIVERED` or `FAILED` (+ optional reason).
2. Order Service updates the row and publishes `delivery.updated`.
3. Client Portal's row for that order updates live — this is the
   scenario brief's own worked example of real-time tracking.

---

## 4. Message broker topology

- **Exchange**: `swifttrack`, type `topic`, declared by every publisher/
  consumer (idempotent declare).
- **Routing keys published**: `order.created`, `order.completed`,
  `order.failed`, `delivery.updated`.
- **Saga Worker's queue**: `saga-worker.order-created` — durable, bound
  to routing key `order.created` only. `prefetch_count=1` + manual ack:
  this is a **Competing Consumers** setup — running multiple Saga Worker
  processes against this same queue would load-balance orders across
  them with no code change, which is the scaling story for the
  architecture (not yet demonstrated with real replicas — see §10).
- **Notification Service's queue**: anonymous, exclusive, auto-delete —
  bound to wildcard patterns `order.#` and `delivery.#` (everything).
  Auto-ack, since a dropped UI notification isn't business-critical the
  way a lost order would be.

---

## 5. Database schema (`init.sql`)

**`orders`**
| Column | Notes |
|---|---|
| `order_id` (PK) | e.g. `ORD-a1b2c3d4` |
| `client_name`, `addresses` (JSONB) | raw submitted order data |
| `status` | `PENDING` → `CONFIRMED` or `FAILED` |
| `cms_order_id`, `wms_package_id`, `ros_route_id` | IDs returned by each backend once the saga succeeds |
| `failed_step`, `failure_reason` | which saga step failed and why, when `status = FAILED` |
| `delivery_status`, `delivery_reason`, `delivered_at` | driver-reported outcome, independent of order-processing status |
| `created_at`, `updated_at` | timestamps |

**`idempotency_keys`**
| Column | Notes |
|---|---|
| `idempotency_key` (PK) | client-supplied header value |
| `order_id` (FK → `orders`) | which order this key resolved to |
| `created_at` | |

---

## 6. External-system protocol adapters (concrete payload shapes)

Use these directly in the "heterogeneous systems integration" diagram/
discussion — they're the literal wire formats, not paraphrased.

**CMS (SOAP-style XML over HTTP)** — `saga-worker.call_cms()` →
`mock-cms POST /cms/order`
```xml
<!-- request -->
<Order><ClientName>Kandy Traders</ClientName>
  <Addresses><Address>123 Galle Rd</Address><Address>45 Duplication Rd</Address></Addresses>
</Order>

<!-- response -->
<OrderResponse><OrderId>CMS-48213</OrderId>
  <ClientName>Kandy Traders</ClientName><Status>ACCEPTED</Status></OrderResponse>
```
Compensation: `POST /cms/order/cancel` `{"orderId": "CMS-48213"}` →
`{"cancelled": true, "orderId": "CMS-48213"}`

**WMS (proprietary newline-delimited JSON over raw TCP)** —
`saga-worker.call_wms()` → `mock-wms` socket server on `:6000`
```
→ {"orderId": "ORD-a1b2c3d4", "packageCount": 2}\n
← {"packageId": "WMS-ORD-a1b2c3d4", "status": "RECEIVED", "packageCount": 2}\n
```
Compensation:
```
→ {"cancelPackageId": "WMS-ORD-a1b2c3d4"}\n
← {"packageId": "WMS-ORD-a1b2c3d4", "status": "CANCELLED"}\n
```

**ROS (REST/JSON)** — `saga-worker.call_ros()` →
`mock-ros POST /routes/optimize`
```json
// request
{"deliveryAddresses": ["123 Galle Rd", "45 Duplication Rd"]}
// response
{"routeId": "ROS-4821", "optimizedStops": ["123 Galle Rd", "45 Duplication Rd"], "estimatedMinutes": 24}
```
Failure simulation: `POST /routes/toggle-failure` flips a flag; while on,
`/routes/optimize` returns `503 {"error": "ROS temporarily unavailable"}`.

---

## 7. Architectural & integration patterns actually implemented

Use this list as the backbone of section 1c — every pattern named here
is real, cite the file/function, don't invent extras.

1. **Orchestrated Saga with compensation** — `saga-worker/worker.py:
   run_saga()`, `_compensate()`, `compensate_cms()`, `compensate_wms()`.
   Answers challenge 4 (transaction management/recovery) directly: a
   central place decides call order and unwind order.
2. **Circuit Breaker** — `ros_breaker = pybreaker.CircuitBreaker(fail_max=3,
   reset_timeout=20)` wrapping `call_ros()`. Answers part of challenge 5
   (resilience) — stops hammering a failing external dependency.
3. **Competing Consumers** — durable queue + `prefetch_count=1` + manual
   ack on the Saga Worker's queue. Enables horizontal scaling of saga
   processing (see §4) and, combined with manual ack, guarantees an
   accepted order is never silently dropped even if a worker crashes.
4. **Publish–Subscribe over a Topic Exchange** — the `swifttrack`
   exchange with hierarchical routing keys (`order.*`, `delivery.*`)
   lets the Notification Service (and, in principle, future consumers)
   subscribe broadly (`order.#`) without the publishers knowing who's
   listening.
5. **Idempotent Receiver, both ends**:
   - producer-facing: `Idempotency-Key` header + `idempotency_keys`
     table dedupes client-side retries at the Order Service.
   - consumer-facing: the Saga Worker checks current order status before
     reprocessing, guarding against RabbitMQ's at-least-once redelivery.
6. **Protocol/Data-Format Translation (Adapter)** — `call_cms`/`call_wms`/
   `call_ros` each translate the same internal event into a different
   wire protocol (XML, raw TCP JSON-lines, REST JSON) and translate the
   reply back into a uniform internal shape. This is the direct answer
   to challenge 1.
7. **Asynchronous Request-Acknowledgement (return address / decoupled
   response)** — the Order Service's `202 Accepted` is not the final
   answer; the real outcome arrives later via the event stream. This is
   the direct answer to challenge 3 ("should not block the client
   portal").
8. **API Gateway responsibility folded into a service (explicit
   simplification)** — JWT issuance/verification lives in
   `order-service/auth.py` rather than a separate gateway service. State
   this explicitly in the docs as a deliberate prototype-scope decision,
   not an oversight.

---

## 8. Security — implemented vs. explicitly out of scope

**Implemented**
- JWT (HS256, 2h expiry) required on `POST /orders` and
  `POST /deliveries/<id>/status` (`auth.py: require_auth`).
- `Idempotency-Key` required on order submission (defense against
  duplicate-order-from-retry, not strictly a security control but
  belongs in the same discussion).
- CORS enabled (`flask-cors`) on the Order Service so browser clients on
  a different origin can call it.

**Explicit gaps — list these in section 1e as accepted prototype risks,
each with what a production deployment would need:**
- `GET /orders/<order_id>` has no auth at all — any order is readable by
  ID. → would need the same JWT check, plus authorization (a client
  should only read their own orders).
- The WebSocket channel (`notification-service`) has no auth or
  per-client scoping — it broadcasts every order and delivery event to
  every connected socket, a cross-tenant data leak in a real multi-
  client deployment. → would need to join clients to a room keyed off
  their JWT `sub` claim.
- `/login` accepts any username with no password/credential check — it's
  a stand-in for a real identity provider, not an auth system.
- No role distinction between "client" and "driver" — the same JWT
  authorizes both the order-submission and delivery-status endpoints.
  → would need a role claim and per-endpoint authorization.
- Hardcoded credentials/secrets throughout (`swift`/`swift123` for both
  RabbitMQ and Postgres, `SECRET_KEY = "swifttrack-dev-secret"` in
  `auth.py`) — committed in source rather than environment variables or
  a secrets manager.
- No TLS anywhere — all traffic is plain HTTP/TCP on localhost.
- No rate limiting on `POST /orders`, despite the brief's own framing
  around high-volume promotional events (Black Friday/Avurudu) being a
  natural abuse vector too.

---

## 9. Two alternative architectures (required by section 5.1.b)

### Alternative A — Synchronous orchestration via an API Gateway (no broker)
Client Portal → API Gateway → **synchronously** calls CMS, then WMS,
then ROS in one request, returns the combined result.
- *Pros*: fewer moving parts, no broker to run/operate, easy to reason
  about for a small team.
- *Cons*: directly violates challenge 3 — the client blocks on the
  slowest backend (ROS). No buffering for traffic spikes. If the process
  crashes mid-call, the order can be lost entirely (violates "an order
  must never be lost"). This was literally the bug in the project's own
  Week 1 draft and is why it was rejected.

### Alternative B — Pure event choreography (no central Saga Worker)
Order Service publishes `order.created`; independent CMS-adapter, WMS-
adapter, and ROS-adapter services each react to events (not to a central
orchestrator), each publishing their own outcome; a separate aggregator
listens for all three outcomes to decide final order status.
- *Pros*: maximum decoupling — each adapter scales and fails
  independently; no single orchestrator process is a bottleneck.
- *Cons*: compensation logic gets scattered across services (each one
  needs to know what to undo and listen for siblings' failures), making
  "what happens when step 2 fails" much harder to audit than one
  `run_saga()` function. Harder to trace a single order's journey.
  More services to design/build/debug within a 4-week team assignment
  for no functional gain at this scale.

### Chosen architecture — Orchestrated Saga with a single Saga Worker
Rationale to state in the docs: centralizes compensation logic in one
auditable place (directly satisfies the "propose methods to recover"
requirement legibly), still achieves full client/backend decoupling via
the queue (satisfies challenge 3 the same as choreography would), and
can still scale horizontally as competing consumers on the same queue if
load requires it — most of Alternative B's scaling benefit without its
debugging cost, and none of Alternative A's blocking/data-loss risk.

---

## 10. Known limitations (state honestly, don't overclaim)

- App services (order-service, saga-worker, the three mocks,
  notification-service) are **not containerized** — only Postgres and
  RabbitMQ are in `docker-compose.yml`. The competing-consumers scaling
  story (§4, §7) is architecturally real but not demonstrated with
  actual multiple replicas.
- No automated test suite exists — verification so far is manual
  (`curl` walkthroughs in `README.md`).
- Proof-of-delivery capture (signature/photo) is **not implemented** —
  per the brief's own allowance, the real-time/notification piece can be
  "architecturally described but only minimally implemented," and this
  is the piece being left at that level.
- Dead-letter handling for compensation calls that themselves fail
  repeatedly is a marked `TODO` in `saga-worker/worker.py`
  (`compensate_cms`) — currently such failures are silently swallowed.
- Mobile-responsiveness of the Driver App hasn't been tested on an
  actual phone viewport, despite the brief framing it as a driver's
  mobile app.

---

## 11. Suggested diagram contents

**Conceptual architecture diagram**: Client Portal / Driver App → API
layer (Order Service) → Message Broker → Saga Worker → {CMS, WMS, ROS} →
Database; Notification Service subscribes to the broker and pushes to
both UIs over WebSocket. Show the `202`-immediately response path
separately from the async saga path to make challenge 3's resolution
visually obvious.

**Implementation architecture diagram**: same shape, labelled with real
tech — Flask + PyJWT + flask-cors (order-service, :5000), RabbitMQ topic
exchange `swifttrack` (:5672/:15672), Python/pika/pybreaker consumer
(saga-worker), PostgreSQL 16 (:5432, tables `orders` +
`idempotency_keys`), Flask-SocketIO (notification-service, :5003), three
mocks (Flask :5001 XML, Flask :5002 JSON + fail-toggle, raw TCP socket
server :6000 JSON-lines), static HTML/JS + Socket.IO client for both
UIs.

---

## 12. File map

```
docker-compose.yml         RabbitMQ + Postgres only
init.sql                   orders, idempotency_keys tables
requirements.txt           flask, requests, pika, psycopg2-binary,
                            flask-socketio, pyjwt, pybreaker, flask-cors

order-service/app.py       thin API: /login, /orders, /orders/<id>,
                            /deliveries/<id>/status, /health
order-service/auth.py      JWT issue + require_auth decorator
order-service/db.py        Postgres connection

saga-worker/worker.py      run_saga, compensation, circuit breaker,
                            idempotent-consumer guard, RabbitMQ consumer
saga-worker/db.py          Postgres connection (separate copy, same DB)

mock-cms/app.py            SOAP/XML stand-in, :5001
mock-ros/app.py            REST/JSON stand-in + fail-toggle, :5002
mock-wms/server.py         raw TCP, newline JSON, :6000

notification-service/app.py  RabbitMQ → WebSocket relay, :5003

client-portal/index.html   client UI: login, submit order, live table
driver-app/index.html      driver UI: login, manifest, mark delivered/failed
shared/style.css           shared styling for both UIs

README.md                  setup + demo walkthrough (incl. failure/
                            compensation/circuit-breaker demo steps)
ROADMAP.md                 what's left before submission (working doc,
                            not part of the deliverable)
```
