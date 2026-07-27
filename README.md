# SwiftTrack — Week 1-2-3 (corrected foundation + resilience)

## What's in this version

**Week 1-2 foundation (corrected):**

- **Order Service** is thin: validate, persist to Postgres as `PENDING`,
  publish an event, return `202` immediately — the client never blocks on
  CMS/WMS/ROS.
- **Saga Worker** is a separate, standalone process that consumes that
  event and does the actual backend work.
- CMS receives real order data (client name + addresses) as real XML.
- Delivery-status updates are wired in — the scenario's own real-time
  tracking example (driver marks delivered -> client portal updates).

**Week 3 additions (this update):**

- **Saga compensation** in the worker: if ROS fails after CMS/WMS already
  succeeded, it calls CMS's cancel endpoint and WMS's cancel message to
  unwind them, in reverse order.
- **Circuit breaker** around the ROS call (`pybreaker`) — 3 failures trips
  it, 20s cooldown before it tries again.
- **Idempotency, backed by Postgres** (not in-memory) — a repeated
  `Idempotency-Key` returns the same order and its current status, and
  this survives a service restart, unlike an in-memory store.
- **Idempotent-consumer guard** in the worker — if RabbitMQ redelivers an
  event (e.g. the worker crashed after processing but before acking), it
  checks the order's current status first and skips re-running the saga.
- **JWT auth** on `/orders` and `/deliveries/.../status` — folded into the
  Order Service for the prototype rather than a separate API Gateway
  service; this simplification should be stated explicitly in the report.

## 1. Start the infrastructure

```bash
docker compose up -d
```

## 2. Install dependencies

```bash
pip install -r requirements.txt
```

## 3. Run every service (separate terminals)

```bash
python mock-cms/app.py              # http://localhost:5001
python mock-ros/app.py              # http://localhost:5002
python mock-wms/server.py           # TCP on localhost:6000
python order-service/app.py         # http://localhost:5000
python saga-worker/worker.py        # message consumer, no HTTP port
python notification-service/app.py  # WebSocket on http://localhost:5003
```

Open `test-client/index.html` in a browser to watch orders update live.

## 4. Get a token and submit an order

```bash
TOKEN=$(curl -s -X POST http://localhost:5000/login \
  -H "Content-Type: application/json" \
  -d '{"username":"demo","password":"demo"}' | python -c "import sys,json;print(json.load(sys.stdin)['token'])")

curl -i -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: demo-key-1" \
  -d '{"clientName": "Kandy Traders", "addresses": ["123 Galle Rd", "45 Duplication Rd"]}'
```

Response comes back `202` immediately. Watch the Saga Worker's terminal and
`test-client/index.html` for the order moving to `CONFIRMED` shortly after.

## 5. Demo idempotency

Repeat the exact same curl command above with the same `Idempotency-Key`.
You'll get the same `orderId` back, and no second order.created event is
published (check the Saga Worker's terminal — nothing new happens).

## 6. Demo saga compensation + the circuit breaker

```bash
curl -X POST http://localhost:5002/routes/toggle-failure

curl -i -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: demo-key-2" \
  -d '{"clientName": "Colombo Mart", "addresses": ["10 Marine Drive"]}'
```

Watch the Saga Worker's terminal: CMS and WMS both succeed, ROS fails, and
you'll see the compensating calls fire against CMS's `/cancel` and WMS's
cancel handling. The order ends up `FAILED` with `failedStep: "ros"`.
Submit two or three more orders (new `Idempotency-Key` each time) while
failure mode is still on — the third attempt should trip the circuit
breaker, and the failure reason will say "circuit breaker open" instead of
a raw connection error. Toggle failure mode off again afterwards.

## 7. Demo the delivery-status real-time push

```bash
curl -X POST http://localhost:5000/deliveries/<orderId>/status \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"status": "DELIVERED"}'
```

## Week 3 acceptance checklist

- [ ] `/login` issues a JWT; `/orders` and `/deliveries/.../status` reject
      requests without one
- [ ] Forcing a ROS failure triggers compensating calls to CMS and WMS
      (visible in their terminal logs) and the order ends up `FAILED`
- [ ] Repeated ROS failures trip the circuit breaker — subsequent attempts
      fail fast with a clear reason, not a hang or raw exception
- [ ] Resubmitting with the same `Idempotency-Key` returns the same order,
      even after restarting the Order Service
- [ ] Restarting the Saga Worker mid-flight and letting RabbitMQ redeliver
      a message doesn't double-process an already-completed order
- [ ] `test-client/index.html` reflects all of the above live

## Who owns what this week

- **Saga Worker dev**: compensation logic + circuit breaker tuning, make
  sure the failure demo above runs cleanly
- **Order Service dev**: idempotency correctness, auth wiring
- **Adapter/mock devs**: CMS's `/cancel` and WMS's cancel handling — these
  are what compensation actually calls
- **Architecture/docs leads**: write up sections (b)/(c)/(e) using the
  real pattern names — Saga, Circuit Breaker, idempotent consumer, JWT —
  and be explicit that the API Gateway's auth responsibility is folded
  into the Order Service for this prototype

## Coming in Week 4

- Dead-letter handling for compensations that themselves fail repeatedly
- Wrap up documentation, record the screencast, rehearse the demo flow
  above so it runs smoothly in under 10 minutes
