# SwiftTrack — Week 1-2 (corrected foundation)

This replaces the earlier draft. The previous version ran the entire
CMS -> WMS -> ROS sequence *inside* the order-submission HTTP request,
which meant the client portal blocked until all three backends had
responded — directly contradicting challenge 3 in the brief ("the system
should not block the client portal while waiting for the ROS to optimise
a route"). It also sent a hardcoded stub to CMS regardless of the real
order, and never touched Postgres despite it being provisioned.

This version fixes all three:

- **Order Service** (`order-service/`) is a thin API: validate, persist to
  Postgres as `PENDING`, publish an event, return `202` immediately.
- **Saga Worker** (`saga-worker/`) is a *separate, standalone consumer
  process* that picks up that event and does the actual CMS -> WMS -> ROS
  work — the client is never waiting on it.
- **CMS now receives real order data** (client name + addresses), built
  into real XML, and the mock actually parses it.
- **Delivery-status updates** are wired in from the start — the scenario's
  own example of real-time tracking ("driver marks a package delivered,
  client portal reflects it immediately") now has a real event path.

Saga compensation, the circuit breaker, idempotency, and auth are Week 3
work, layered on top of this now-correct foundation.

## 1. Start the infrastructure

```bash
docker compose up -d
```

- RabbitMQ: `localhost:5672` (UI at `localhost:15672`, login `swift` / `swift123`)
- PostgreSQL: `localhost:5432` (`swift` / `swift123`, db `swifttrack`) — the
  `orders` table is created automatically from `init.sql` on first start

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
python saga-worker/worker.py        # no HTTP port - a message consumer
python notification-service/app.py  # WebSocket on http://localhost:5003
```

Open `test-client/index.html` in a browser to watch order rows update live.

## 4. Submit an order and watch it process asynchronously

```bash
curl -i -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -d '{"clientName": "Kandy Traders", "addresses": ["123 Galle Rd", "45 Duplication Rd"]}'
```

Notice the response comes back **immediately** with `202` and `"status": "PENDING"`
— the CMS/WMS/ROS calls haven't happened yet. A moment later, watch:

- The Saga Worker's terminal log the CMS/WMS/ROS calls happening
- `test-client/index.html` flip that order's row from PENDING to CONFIRMED
- Or query it directly: `curl http://localhost:5000/orders/<orderId>`

## 5. Simulate a driver marking a package delivered

```bash
curl -X POST http://localhost:5000/deliveries/<orderId>/status \
  -H "Content-Type: application/json" \
  -d '{"status": "DELIVERED"}'
```

Watch the same row in the test client pick up the delivery status live —
this is the scenario's own real-time tracking example, working end to end.

## Week 2 acceptance checklist

- [ ] `POST /orders` returns `202` in well under a second, regardless of
      how long CMS/WMS/ROS take to respond
- [ ] The Saga Worker (a separate process) picks up the event and calls
      all three mocks, visible in its own terminal log
- [ ] Mock CMS's response reflects the real client name sent, not a
      hardcoded value
- [ ] `GET /orders/<id>` shows the order moving from PENDING to CONFIRMED
      (or FAILED)
- [ ] Marking a delivery status pushes a live update to the test client
- [ ] Postgres actually contains the order rows: `docker exec -it
      swifttrack-postgres psql -U swift -d swifttrack -c "SELECT * FROM orders;"`

## Who owns what this week

- **Saga Worker dev**: `saga-worker/worker.py` — the CMS/WMS/ROS calls and
  Postgres status updates
- **Order Service dev**: `order-service/app.py` — request validation,
  the 202-immediately contract, and the delivery-status endpoint
- **Adapter/mock devs**: `mock-cms`, `mock-ros`, `mock-wms` — make sure
  each one reflects its real protocol (XML parsing, REST JSON, TCP lines)
- **Frontend/notifications dev**: `notification-service` and
  `test-client/index.html`
- **Architecture/docs leads**: no code this week — start drafting using
  the corrected component names (Order Service vs. Saga Worker as two
  distinct services, not one)

## Coming in Week 3

- Saga compensation in the worker (undo CMS/WMS if ROS fails)
- Circuit breaker around the ROS call
- Idempotency handling in the Order Service
- JWT auth (API Gateway responsibility, folded into the Order Service for
  the prototype — to be stated explicitly in the documentation)
