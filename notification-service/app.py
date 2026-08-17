"""
Notification service.
Subscribes to the 'swifttrack' exchange for both order.* and delivery.*
events and pushes every one to connected WebSocket clients in real time.

Fix applied in this pass: cors_allowed_origins was "*" (wide open);
now reads ALLOWED_ORIGINS from the environment, same as the Order
Service. RabbitMQ credentials also now come from the environment.

Not fixed in this pass (tracked separately): broadcasts go to every
connected client with no per-client scoping - see the documentation's
"cross-tenant data exposure" item. That needs Socket.IO rooms keyed off
the JWT subject claim, which is a larger change than this security pass.
"""
import json
import os
import threading

import jwt
import pika
from dotenv import load_dotenv
from flask import Flask
from flask_socketio import SocketIO, join_room

load_dotenv()

app = Flask(__name__)
allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
socketio = SocketIO(app, cors_allowed_origins=allowed_origins or [], async_mode="threading")

RABBITMQ_HOST = os.environ.get("RABBITMQ_HOST", "localhost")
RABBITMQ_USER = os.environ["RABBITMQ_USER"]
RABBITMQ_PASSWORD = os.environ["RABBITMQ_PASSWORD"]
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY")
if not JWT_SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY is not set. Copy .env.example to .env and configure it.")


@socketio.on("connect")
def authenticate_socket(auth):
    token = (auth or {}).get("token")
    if not token:
        return False
    try:
        claims = jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return False

    join_room(f"user:{claims['sub']}")
    if claims.get("role") == "driver":
        join_room("role:driver")

def consume_events():
    connection = pika.BlockingConnection(pika.ConnectionParameters(
        host=RABBITMQ_HOST, credentials=pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)
    ))
    channel = connection.channel()
    channel.exchange_declare(exchange="swifttrack", exchange_type="topic")

    queue = channel.queue_declare(queue="", exclusive=True)
    queue_name = queue.method.queue
    channel.queue_bind(exchange="swifttrack", queue=queue_name, routing_key="order.#")
    channel.queue_bind(exchange="swifttrack", queue=queue_name, routing_key="delivery.#")

    def on_message(ch, method, properties, body):
        event = json.loads(body)
        payload = {"type": method.routing_key, **event}
        socketio.emit("update", payload, to="role:driver")
        client_username = event.get("clientUsername")
        if client_username:
            socketio.emit("update", payload, to=f"user:{client_username}")

    channel.basic_consume(queue=queue_name, on_message_callback=on_message, auto_ack=True)
    channel.start_consuming()


@app.route("/health")
def health():
    return {"status": "UP", "system": "notification-service"}


if __name__ == "__main__":
    threading.Thread(target=consume_events, daemon=True).start()
    socketio.run(app, port=5003, allow_unsafe_werkzeug=True)
