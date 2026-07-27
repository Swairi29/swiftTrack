"""
Order Service - Week 2 (corrected foundation).

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
"""
import json
import uuid

import pika
from flask import Flask, request, jsonify

from auth import generate_token, require_auth
from db import get_connection

app = Flask(__name__)


def publish_event(routing_key, payload):
    connection = pika.BlockingConnection(pika.ConnectionParameters(
        host="localhost", credentials=pika.PlainCredentials("swift", "swift123")
    ))
    channel = connection.channel()
    channel.exchange_declare(exchange="swifttrack", exchange_type="topic")
    channel.basic_publish(exchange="swifttrack", routing_key=routing_key, body=json.dumps(payload))
    connection.close()


@app.route("/login", methods=["POST"])
def login():
    creds = request.get_json(force=True)
    username = creds.get("username", "demo-user")
    return jsonify({"token": generate_token(username)})


@app.route("/orders", methods=["POST"])
@require_auth
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
                cur.execute("SELECT status FROM orders WHERE order_id = %s", (order_id,))
                row = cur.fetchone()
            return jsonify({"orderId": order_id, "status": row[0] if row else "UNKNOWN"}), 202

        if not client_name or not addresses:
            return jsonify({"error": "clientName and addresses are required"}), 400

        order_id = body.get("orderId") or f"ORD-{uuid.uuid4().hex[:8]}"

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders (order_id, client_name, addresses, status)
                VALUES (%s, %s, %s, 'PENDING')
                ON CONFLICT (order_id) DO NOTHING
                """,
                (order_id, client_name, json.dumps(addresses)),
            )
            cur.execute(
                "INSERT INTO idempotency_keys (idempotency_key, order_id) VALUES (%s, %s)",
                (idempotency_key, order_id),
            )
        conn.commit()
    finally:
        conn.close()

    publish_event("order.created", {
        "orderId": order_id, "clientName": client_name, "addresses": addresses,
    })

    # 202 Accepted, not 200/201 - the order is accepted for processing,
    # not yet processed. The Saga Worker handles CMS/WMS/ROS asynchronously.
    return jsonify({"orderId": order_id, "status": "PENDING"}), 202


@app.route("/orders/<order_id>", methods=["GET"])
def get_order(order_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT order_id, status, cms_order_id, wms_package_id, ros_route_id, "
                "failed_step, failure_reason, delivery_status, delivery_reason "
                "FROM orders WHERE order_id = %s",
                (order_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return jsonify({"error": "not found"}), 404

    keys = ["orderId", "status", "cmsOrderId", "wmsPackageId", "rosRouteId",
            "failedStep", "failureReason", "deliveryStatus", "deliveryReason"]
    return jsonify(dict(zip(keys, row)))


@app.route("/deliveries/<order_id>/status", methods=["POST"])
@require_auth
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
                "delivered_at = now(), updated_at = now() WHERE order_id = %s",
                (status, reason, order_id),
            )
            updated = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    if not updated:
        return jsonify({"error": "order not found"}), 404

    publish_event("delivery.updated", {"orderId": order_id, "status": status, "reason": reason})

    return jsonify({"orderId": order_id, "deliveryStatus": status})


@app.route("/health", methods=["GET"])
def health():
    return {"status": "UP", "system": "order-service"}


if __name__ == "__main__":
    app.run(port=5000)
