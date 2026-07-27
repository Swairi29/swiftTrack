"""
Notification service - Week 2.
Subscribes to the 'swifttrack' exchange for both order.* and delivery.*
events and pushes every one to connected WebSocket clients in real time.
"""
import json
import threading

import pika
from flask import Flask
from flask_socketio import SocketIO

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


def consume_events():
    connection = pika.BlockingConnection(pika.ConnectionParameters(
        host="localhost", credentials=pika.PlainCredentials("swift", "swift123")
    ))
    channel = connection.channel()
    channel.exchange_declare(exchange="swifttrack", exchange_type="topic")

    queue = channel.queue_declare(queue="", exclusive=True)
    queue_name = queue.method.queue
    channel.queue_bind(exchange="swifttrack", queue=queue_name, routing_key="order.#")
    channel.queue_bind(exchange="swifttrack", queue=queue_name, routing_key="delivery.#")

    def on_message(ch, method, properties, body):
        event = json.loads(body)
        socketio.emit("update", {"type": method.routing_key, **event})

    channel.basic_consume(queue=queue_name, on_message_callback=on_message, auto_ack=True)
    channel.start_consuming()


@app.route("/health")
def health():
    return {"status": "UP", "system": "notification-service"}


if __name__ == "__main__":
    threading.Thread(target=consume_events, daemon=True).start()
    socketio.run(app, port=5003, allow_unsafe_werkzeug=True)
