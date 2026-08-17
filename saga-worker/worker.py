"""
Saga Worker.

Fixes applied in this pass:
  - Atomic claim before processing: the previous version only wrote
    CONFIRMED/FAILED at the very end of process_order(), so a crash
    mid-saga left the row at PENDING. On redelivery, the old
    read-then-check guard saw PENDING and re-ran the ENTIRE saga,
    calling CMS/WMS/ROS a second time for the same order. The fix below
    claims the order atomically (UPDATE ... WHERE status='PENDING') the
    moment processing starts, so a redelivered message after a crash
    finds the row already claimed and skips - the failure mode changes
    from "silently duplicated side effects" to "stuck at PROCESSING",
    which is recoverable and, critically, doesn't duplicate a CMS order
    or a WMS package. The claim also has a staleness clause so a
    genuinely abandoned claim (the worker that took it crashed and
    RabbitMQ redelivers the message) can be re-claimed after a timeout,
    rather than being stuck forever.
  - Compensation failures now go to a dead-letter queue instead of a
    bare `except: pass` - this was an explicit TODO in the code before.
  - RabbitMQ/backend credentials come from the environment.

Requires an additional `claimed_at TIMESTAMP` column on `orders` - see
the migration note in the accompanying README/PR description.
"""
import json
import os
import socket

import pika
import pybreaker
import requests
from defusedxml import ElementTree as ET  # hardened against XXE
from dotenv import load_dotenv

from db import get_connection

load_dotenv()

CMS_URL = "http://localhost:5001/cms/order"
CMS_CANCEL_URL = "http://localhost:5001/cms/order/cancel"
ROS_URL = "http://localhost:5002/routes/optimize"
WMS_HOST = "localhost"
WMS_PORT = 6000

CMS_USERNAME = os.environ.get("CMS_USERNAME")
CMS_PASSWORD = os.environ.get("CMS_PASSWORD")
ROS_API_KEY = os.environ.get("ROS_API_KEY")
WMS_AUTH_TOKEN = os.environ.get("WMS_AUTH_TOKEN")
if not all((CMS_USERNAME, CMS_PASSWORD, ROS_API_KEY, WMS_AUTH_TOKEN)):
    raise RuntimeError("Missing service credentials. Copy .env.example to .env and configure it.")

RABBITMQ_HOST = os.environ.get("RABBITMQ_HOST", "localhost")
RABBITMQ_USER = os.environ["RABBITMQ_USER"]
RABBITMQ_PASSWORD = os.environ["RABBITMQ_PASSWORD"]

# How long a claim is honored before it's considered abandoned (e.g. the
# worker that took it crashed) and can be re-claimed by a redelivery.
CLAIM_STALE_AFTER = "2 minutes"

# After 3 consecutive ROS failures, stop calling ROS for 20s and fail fast.
ros_breaker = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=20)


class SagaFailedError(Exception):
    def __init__(self, step, reason):
        self.step = step
        self.reason = reason
        super().__init__(f"Saga failed at step '{step}': {reason}")


def _rabbitmq_channel():
    connection = pika.BlockingConnection(pika.ConnectionParameters(
        host=RABBITMQ_HOST, credentials=pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)
    ))
    return connection, connection.channel()


def publish_event(routing_key, payload):
    connection, channel = _rabbitmq_channel()
    channel.exchange_declare(exchange="swifttrack", exchange_type="topic")
    channel.basic_publish(exchange="swifttrack", routing_key=routing_key, body=json.dumps(payload))
    connection.close()


def publish_to_dlq(payload):
    """Compensation actions that themselves fail land here instead of
    being silently dropped, so there's an operator-visible trail."""
    connection, channel = _rabbitmq_channel()
    channel.queue_declare(queue="compensation.dlq", durable=True)
    channel.basic_publish(exchange="", routing_key="compensation.dlq", body=json.dumps(payload))
    connection.close()


def claim_order(order_id):
    """Atomically claim an order for processing. Returns True if this
    call won the claim, False if it's already been claimed/processed
    recently and should be skipped."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE orders
                SET status = 'PROCESSING', claimed_at = now(), updated_at = now()
                WHERE order_id = %s
                  AND (status = 'PENDING'
                       OR (status = 'PROCESSING' AND claimed_at < now() - interval '{CLAIM_STALE_AFTER}'))
                """,
                (order_id,),
            )
            claimed = cur.rowcount == 1
        conn.commit()
        return claimed
    finally:
        conn.close()


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
    address_xml = "".join(f"<Address>{a}</Address>" for a in addresses)
    order_xml = f"<Order><ClientName>{client_name}</ClientName><Addresses>{address_xml}</Addresses></Order>"
    resp = requests.post(CMS_URL, data=order_xml, auth=(CMS_USERNAME, CMS_PASSWORD), headers={"Content-Type": "application/xml"}, timeout=5)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    return root.findtext("OrderId")


def compensate_cms(order_id, cms_order_id):
    if not cms_order_id:
        return
    try:
        requests.post(CMS_CANCEL_URL, json={"orderId": cms_order_id}, auth=(CMS_USERNAME, CMS_PASSWORD), timeout=5)
    except requests.RequestException as e:
        publish_to_dlq({"orderId": order_id, "step": "compensate_cms", "cmsOrderId": cms_order_id, "error": str(e)})


def call_wms(order_id, addresses):
    with socket.create_connection((WMS_HOST, WMS_PORT), timeout=5) as s:
        s.sendall((json.dumps({"authToken": WMS_AUTH_TOKEN, "orderId": order_id, "packageCount": len(addresses)}) + "\n").encode("utf-8"))
        response = json.loads(s.recv(1024).decode("utf-8"))
        if response.get("status") == "ERROR":
            raise OSError(response.get("message", "WMS rejected request"))
        return response


def compensate_wms(order_id, package_id):
    if not package_id:
        return
    try:
        with socket.create_connection((WMS_HOST, WMS_PORT), timeout=5) as s:
            s.sendall((json.dumps({"authToken": WMS_AUTH_TOKEN, "cancelPackageId": package_id}) + "\n").encode("utf-8"))
            s.recv(1024)
    except OSError as e:
        publish_to_dlq({"orderId": order_id, "step": "compensate_wms", "packageId": package_id, "error": str(e)})


@ros_breaker
def call_ros(addresses):
    resp = requests.post(ROS_URL, json={"deliveryAddresses": addresses}, headers={"X-API-Key": ROS_API_KEY}, timeout=5)
    resp.raise_for_status()
    return resp.json()


def run_saga(order_id, client_name, addresses):
    completed = {}

    try:
        completed["cms"] = call_cms(client_name, addresses)
    except (requests.RequestException, ET.ParseError) as e:
        raise SagaFailedError("cms", str(e))

    try:
        completed["wms"] = call_wms(order_id, addresses)
    except OSError as e:
        _compensate(order_id, completed)
        raise SagaFailedError("wms", str(e))

    try:
        completed["ros"] = call_ros(addresses)
    except pybreaker.CircuitBreakerError:
        _compensate(order_id, completed)
        raise SagaFailedError("ros", "circuit breaker open - ROS has failed repeatedly, giving it a cooldown")
    except requests.RequestException as e:
        _compensate(order_id, completed)
        raise SagaFailedError("ros", str(e))

    return completed


def _compensate(order_id, completed):
    if "wms" in completed:
        compensate_wms(order_id, completed["wms"].get("packageId"))
    if "cms" in completed:
        compensate_cms(order_id, completed["cms"])


def process_order(event):
    order_id = event["orderId"]
    client_name = event["clientName"]
    client_username = event.get("clientUsername")
    addresses = event["addresses"]

    if not claim_order(order_id):
        print(f"Skipping {order_id}: already claimed/processed (redelivery or duplicate)")
        return

    try:
        result = run_saga(order_id, client_name, addresses)
    except SagaFailedError as e:
        update_order(order_id, status="FAILED", failed_step=e.step, failure_reason=e.reason[:200])
        publish_event("order.failed", {"orderId": order_id, "clientUsername": client_username, "failedStep": e.step, "reason": e.reason})
        return

    update_order(
        order_id, status="CONFIRMED", cms_order_id=result["cms"],
        wms_package_id=result["wms"].get("packageId"), ros_route_id=result["ros"].get("routeId"),
    )
    publish_event("order.completed", {
        "orderId": order_id, "clientUsername": client_username, "cmsOrderId": result["cms"], "wms": result["wms"], "ros": result["ros"],
    })


def start_worker():
    connection, channel = _rabbitmq_channel()
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
