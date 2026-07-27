"""
Saga Worker - Week 2 (corrected foundation).

Runs as its OWN process, entirely separate from the Order Service. It
consumes order.created events from RabbitMQ and runs CMS -> WMS -> ROS in
sequence, updating the order's row in Postgres and publishing
order.completed / order.failed as it goes.

Intentionally straight-line for now: no compensation and no circuit
breaker yet. Those are Week 3 additions layered on top of this same
consumer, once the async foundation itself is confirmed working.

Manual ack (not auto_ack) is used deliberately: if this process crashes
mid-order, the message is redelivered rather than silently lost, which
matters for "an order must never be lost" from the brief.
"""
import json
import socket
import xml.etree.ElementTree as ET

import pika
import requests

from db import get_connection

CMS_URL = "http://localhost:5001/cms/order"
ROS_URL = "http://localhost:5002/routes/optimize"
WMS_HOST = "localhost"
WMS_PORT = 6000


def publish_event(routing_key, payload):
    connection = pika.BlockingConnection(pika.ConnectionParameters(
        host="localhost", credentials=pika.PlainCredentials("swift", "swift123")
    ))
    channel = connection.channel()
    channel.exchange_declare(exchange="swifttrack", exchange_type="topic")
    channel.basic_publish(exchange="swifttrack", routing_key=routing_key, body=json.dumps(payload))
    connection.close()


def update_order(order_id, **fields):
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [order_id]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE orders SET {set_clause}, updated_at = now() WHERE order_id = %s",
                values,
            )
        conn.commit()
    finally:
        conn.close()


def call_cms(client_name, addresses):
    # Real translation: build actual XML from the real order fields.
    address_xml = "".join(f"<Address>{a}</Address>" for a in addresses)
    order_xml = f"<Order><ClientName>{client_name}</ClientName><Addresses>{address_xml}</Addresses></Order>"
    resp = requests.post(CMS_URL, data=order_xml, headers={"Content-Type": "application/xml"}, timeout=5)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    return root.findtext("OrderId")


def call_wms(order_id, addresses):
    with socket.create_connection((WMS_HOST, WMS_PORT), timeout=5) as s:
        s.sendall((json.dumps({"orderId": order_id, "packageCount": len(addresses)}) + "\n").encode("utf-8"))
        return json.loads(s.recv(1024).decode("utf-8"))


def call_ros(addresses):
    resp = requests.post(ROS_URL, json={"deliveryAddresses": addresses}, timeout=5)
    resp.raise_for_status()
    return resp.json()


def process_order(event):
    order_id = event["orderId"]
    client_name = event["clientName"]
    addresses = event["addresses"]

    try:
        cms_order_id = call_cms(client_name, addresses)
        wms_resp = call_wms(order_id, addresses)
        ros_resp = call_ros(addresses)
    except (requests.RequestException, OSError, ET.ParseError) as e:
        update_order(order_id, status="FAILED", failure_reason=str(e)[:200])
        publish_event("order.failed", {"orderId": order_id, "reason": str(e)})
        return

    update_order(
        order_id, status="CONFIRMED", cms_order_id=cms_order_id,
        wms_package_id=wms_resp.get("packageId"), ros_route_id=ros_resp.get("routeId"),
    )
    publish_event("order.completed", {
        "orderId": order_id, "cmsOrderId": cms_order_id, "wms": wms_resp, "ros": ros_resp,
    })


def start_worker():
    connection = pika.BlockingConnection(pika.ConnectionParameters(
        host="localhost", credentials=pika.PlainCredentials("swift", "swift123")
    ))
    channel = connection.channel()
    channel.exchange_declare(exchange="swifttrack", exchange_type="topic")
    queue = channel.queue_declare(queue="saga-worker.order-created", durable=True)
    channel.queue_bind(exchange="swifttrack", queue=queue.method.queue, routing_key="order.created")

    def on_message(ch, method, properties, body):
        event = json.loads(body)
        process_order(event)
        ch.basic_ack(delivery_tag=method.delivery_tag)

    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=queue.method.queue, on_message_callback=on_message)
    print("Saga worker listening for order.created events...")
    channel.start_consuming()


if __name__ == "__main__":
    start_worker()
