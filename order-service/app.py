"""
Order Service 

Deliberately thin: validates the request, persists it as PENDING in
Postgres, publishes an order.created event, and returns immediately.
It does NOT call CMS/WMS/ROS itself - that is the Saga Worker's job,
running as its own separate consumer process (see saga-worker/worker.py).

This is what actually satisfies challenge 3 from the brief - "the system
should not block the client portal while waiting for the ROS to optimise
a route or for the WMS to confirm a package is ready" - since the client
gets a response before any of that happens.

Also handles the driver-app side of real-time tracking: marking a package
delivered/failed, which is the scenario's own example of a status change
that should reach the client portal immediately.


Fixes applied in this pass:
  - GET /orders/<id> now requires auth (previously anyone could look up
    or enumerate any order)
  - CORS is scoped to ALLOWED_ORIGINS from the environment, not "*"
  - RabbitMQ credentials come from the environment, not hardcoded

Still deliberately thin: it never calls CMS/WMS/ROS itself. That stays
the Saga Worker's job, running as its own separate process.


"""
import json
import os
import time
import uuid

import pika
from dotenv import load_dotenv
from flask import Flask, g, request, jsonify
from flask_cors import CORS

from auth import generate_token, require_auth, require_role
from db import authenticate_user, get_connection

load_dotenv()

app = Flask(__name__)


allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
CORS(app, origins=allowed_origins or None)  # None here means Flask-CORS default (same-origin only), not "*"

RABBITMQ_HOST = os.environ.get("RABBITMQ_HOST", "localhost")
RABBITMQ_USER = os.environ["RABBITMQ_USER"]
RABBITMQ_PASSWORD = os.environ["RABBITMQ_PASSWORD"]

def publish_event(routing_key, payload):
    # Guards against the same brief RabbitMQ-not-quite-ready window as the
    # other services, in case an order is submitted immediately after
    # startup - short backoff so a real outage still fails fast for the
    # caller instead of hanging the request.
    last_error = None
    connection = None
    for attempt in range(5):
        try:
            connection = pika.BlockingConnection(pika.ConnectionParameters(
                host=RABBITMQ_HOST, credentials=pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)
            ))
            break
        except pika.exceptions.AMQPConnectionError as e:
            last_error = e
            time.sleep(1)
    if connection is None:
        raise last_error
    channel = connection.channel()
    channel.exchange_declare(exchange="swifttrack", exchange_type="topic")
    channel.basic_publish(exchange="swifttrack", routing_key=routing_key, body=json.dumps(payload))
    connection.close()


@app.route("/login", methods=["POST"])
def login():
    # Demo credentials are validated against password hashes in PostgreSQL.
    creds = request.get_json(force=True)
    user = authenticate_user(creds.get("username", ""), creds.get("password", ""))
    if not user:
        return jsonify({"error": "Invalid username or password"}), 401
    return jsonify({"token": generate_token(user["username"], user["role"]), "role": user["role"]})


@app.route("/orders", methods=["POST"])
@require_auth
@require_role("client")
def submit_order():
    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        return jsonify({"error": "Idempotency-Key header is required"}), 400

    body = request.get_json(force=True)
    client_name = body.get("clientName")
    addresses = body.get("addresses", [])

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT order_id FROM idempotency_keys WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            existing = cur.fetchone()

        if existing:
            order_id = existing[0]
            with conn.cursor() as cur:
                cur.execute("SELECT status, client_username FROM orders WHERE order_id = %s", (order_id,))
                row = cur.fetchone()
            if row and row[1] != g.current_user["sub"]:
                return jsonify({"error": "Idempotency key belongs to another client"}), 409
            return jsonify({"orderId": order_id, "status": row[0] if row else "UNKNOWN"}), 202

        if not client_name or not addresses:
            return jsonify({"error": "clientName and addresses are required"}), 400

        order_id = body.get("orderId") or f"ORD-{uuid.uuid4().hex[:8]}"

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders (order_id, client_name, client_username, addresses, status)
                VALUES (%s, %s, %s, %s, 'PENDING')
                ON CONFLICT (order_id) DO NOTHING
                """,
                (order_id, client_name, g.current_user["sub"], json.dumps(addresses)),
            )
            cur.execute(
                "INSERT INTO idempotency_keys (idempotency_key, order_id) VALUES (%s, %s)",
                (idempotency_key, order_id),
            )
        conn.commit()
    finally:
        conn.close()

    publish_event("order.created", {
        "orderId": order_id, "clientName": client_name, "clientUsername": g.current_user["sub"], "addresses": addresses,
    })

    return jsonify({"orderId": order_id, "status": "PENDING"}), 202


@app.route("/orders", methods=["GET"])
@require_auth
def list_orders():
    # Seeds the client portal / driver app with history on login - without
    # this, the UI only ever shows orders/deliveries that happened to
    # arrive over the WebSocket during the current session (see the
    # "orders disappear after logout" gap). Clients get their own orders
    # only; drivers get the operational manifest (all orders), matching
    # the same scoping already used for the live WebSocket rooms.
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            select = (
                "SELECT order_id, client_name, client_username, addresses, status, cms_order_id, "
                "wms_package_id, ros_route_id, failed_step, failure_reason, delivery_status, delivery_reason "
                "FROM orders "
            )
            if g.current_user["role"] == "client":
                cur.execute(select + "WHERE client_username = %s ORDER BY created_at DESC", (g.current_user["sub"],))
            else:
                cur.execute(select + "ORDER BY created_at DESC")
            rows = cur.fetchall()
    finally:
        conn.close()

    keys = ["orderId", "clientName", "clientUsername", "addresses", "status", "cmsOrderId",
            "wmsPackageId", "rosRouteId", "failedStep", "failureReason", "deliveryStatus", "deliveryReason"]
    return jsonify([dict(zip(keys, row)) for row in rows])


@app.route("/orders/<order_id>", methods=["GET"])
@require_auth
def get_order(order_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT order_id, client_username, status, cms_order_id, wms_package_id, ros_route_id, "
                "failed_step, failure_reason, delivery_status, delivery_reason "
                "FROM orders WHERE order_id = %s",
                (order_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return jsonify({"error": "not found"}), 404

    if g.current_user["role"] == "client" and row[1] != g.current_user["sub"]:
        return jsonify({"error": "You are not authorized to view this order"}), 403

    keys = ["orderId", "clientUsername", "status", "cmsOrderId", "wmsPackageId", "rosRouteId",
            "failedStep", "failureReason", "deliveryStatus", "deliveryReason"]
    return jsonify(dict(zip(keys, row)))


@app.route("/deliveries/<order_id>/status", methods=["POST"])
@require_auth
@require_role("driver")
def update_delivery_status(order_id):
    # Stands in for the driver app marking a package delivered/failed.
    body = request.get_json(force=True)
    status = body.get("status")
    reason = body.get("reason")

    if status not in ("DELIVERED", "FAILED"):
        return jsonify({"error": "status must be DELIVERED or FAILED"}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET delivery_status = %s, delivery_reason = %s, "
                "delivered_at = now(), updated_at = now() WHERE order_id = %s RETURNING client_username",
                (status, reason, order_id),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    if not row:
        return jsonify({"error": "order not found"}), 404

    publish_event("delivery.updated", {"orderId": order_id, "clientUsername": row[0], "status": status, "reason": reason})

    return jsonify({"orderId": order_id, "deliveryStatus": status})


@app.route("/health", methods=["GET"])
def health():
    return {"status": "UP", "system": "order-service"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
