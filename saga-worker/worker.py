"""
Saga Worker - Week 3 (compensation + circuit breaker).

Runs as its OWN process, entirely separate from the Order Service. It
consumes order.created events from RabbitMQ and runs CMS -> WMS -> ROS in
sequence, updating the order's row in Postgres and publishing
order.completed / order.failed as it goes.

If a step fails partway through, run_saga() unwinds whatever already
succeeded (WMS then CMS, reverse order) via their cancel operations -
this is the orchestrated-Saga answer to challenge 4 in the brief
("propose the methods the system would use to recover when one or more
steps in a transaction fail").

The ROS call is wrapped in a circuit breaker (pybreaker): after 3
consecutive failures it trips and fails fast for 20s instead of hammering
a struggling/unavailable ROS.

Manual ack (not auto_ack) is used deliberately: if this process crashes
mid-order, the message is redelivered rather than silently lost, which
matters for "an order must never be lost" from the brief. Because of that
redelivery, process_order() first checks the order's current status and
skips it if it has already moved past PENDING - the idempotent-consumer
side of that guarantee.
"""
import json
import socket
import xml.etree.ElementTree as ET

import pika
import pybreaker
import requests

from db import get_connection

CMS_URL = "http://localhost:5001/cms/order"
CMS_CANCEL_URL = "http://localhost:5001/cms/order/cancel"
ROS_URL = "http://localhost:5002/routes/optimize"
WMS_HOST = "localhost"
WMS_PORT = 6000

# After 3 consecutive ROS failures, stop calling ROS for 20s and fail fast.
ros_breaker = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=20)


class SagaFailedError(Exception):
    def __init__(self, step, reason):
        self.step = step
        self.reason = reason
        super().__init__(f"Saga failed at step '{step}': {reason}")


def get_order_status(order_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM orders WHERE order_id = %s", (order_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


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


def compensate_cms(cms_order_id):
    if not cms_order_id:
        return
    try:
        requests.post(CMS_CANCEL_URL, json={"orderId": cms_order_id}, timeout=5)
    except requests.RequestException:
        pass  # TODO Week 4: route persistently-failing compensations to a dead-letter queue


def call_wms(order_id, addresses):
    with socket.create_connection((WMS_HOST, WMS_PORT), timeout=5) as s:
        s.sendall((json.dumps({"orderId": order_id, "packageCount": len(addresses)}) + "\n").encode("utf-8"))
        return json.loads(s.recv(1024).decode("utf-8"))


def compensate_wms(package_id):
    if not package_id:
        return
    try:
        with socket.create_connection((WMS_HOST, WMS_PORT), timeout=5) as s:
            s.sendall((json.dumps({"cancelPackageId": package_id}) + "\n").encode("utf-8"))
            s.recv(1024)
    except OSError:
        pass


@ros_breaker
def call_ros(addresses):
    resp = requests.post(ROS_URL, json={"deliveryAddresses": addresses}, timeout=5)
    resp.raise_for_status()
    return resp.json()


def _compensate(completed):
    if "wms" in completed:
        compensate_wms(completed["wms"].get("packageId"))
    if "cms" in completed:
        compensate_cms(completed["cms"])


def run_saga(order_id, client_name, addresses):
    completed = {}
    try:
        completed["cms"] = call_cms(client_name, addresses)
    except (requests.RequestException, ET.ParseError) as e:
        raise SagaFailedError("cms", str(e))
    try:
        completed["wms"] = call_wms(order_id, addresses)
    except OSError as e:
        _compensate(completed)
        raise SagaFailedError("wms", str(e))
    try:
        completed["ros"] = call_ros(addresses)
    except pybreaker.CircuitBreakerError:
        _compensate(completed)
        raise SagaFailedError("ros", "circuit breaker open - ROS has failed repeatedly, giving it a cooldown")
    except requests.RequestException as e:
        _compensate(completed)
        raise SagaFailedError("ros", str(e))
    return completed


def process_order(event):
    order_id = event["orderId"]
    client_name = event["clientName"]
    addresses = event["addresses"]

    # Idempotent-consumer guard against RabbitMQ redelivery.
    current_status = get_order_status(order_id)
    if current_status not in (None, "PENDING"):
        print(f"Skipping {order_id}: already {current_status} (likely a redelivered message)")
        return

    try:
        result = run_saga(order_id, client_name, addresses)
    except SagaFailedError as e:
        update_order(order_id, status="FAILED", failed_step=e.step, failure_reason=e.reason[:200])
        publish_event("order.failed", {"orderId": order_id, "failedStep": e.step, "reason": e.reason})
        return

    update_order(
        order_id, status="CONFIRMED", cms_order_id=result["cms"],
        wms_package_id=result["wms"].get("packageId"), ros_route_id=result["ros"].get("routeId"),
    )
    publish_event("order.completed", {
        "orderId": order_id, "cmsOrderId": result["cms"], "wms": result["wms"], "ros": result["ros"],
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
