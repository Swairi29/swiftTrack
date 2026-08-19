"""
Mock WMS (Warehouse Management System).
Real WMS = proprietary messaging protocol over TCP/IP. Simulated here with
newline-delimited JSON: send one JSON line, get one JSON line back.
"""
import socket
import threading
import json
import hmac
import os
import time

from dotenv import load_dotenv

HOST = "0.0.0.0"
PORT = 6000
load_dotenv()
WMS_AUTH_TOKEN = os.environ.get("WMS_AUTH_TOKEN")
if not WMS_AUTH_TOKEN:
    raise RuntimeError("WMS_AUTH_TOKEN must be set in .env")

# A real warehouse system wouldn't ack in a few milliseconds. Purely so a
# live demo/screencast can see PENDING held for a moment
SIMULATED_LATENCY_SECONDS = 1.0


def handle_client(conn, addr):
    with conn:
        buffer = ""
        while True:
            data = conn.recv(1024)
            if not data:
                break
            buffer += data.decode("utf-8")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not hmac.compare_digest(str(msg.get("authToken", "")), WMS_AUTH_TOKEN):
                    conn.sendall((json.dumps({"status": "ERROR", "message": "WMS authentication failed"}) + "\n").encode("utf-8"))
                    continue
                if "cancelPackageId" in msg:
                    ack = {"packageId": msg["cancelPackageId"], "status": "CANCELLED"}
                else:
                    time.sleep(SIMULATED_LATENCY_SECONDS)
                    package_id = f"WMS-{msg.get('orderId', 'UNKNOWN')}"
                    ack = {"packageId": package_id, "status": "RECEIVED",
                           "packageCount": msg.get("packageCount", 1)}
                conn.sendall((json.dumps(ack) + "\n").encode("utf-8"))


def start_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen()
        print(f"Mock WMS TCP server listening on port {PORT}")
        while True:
            conn, addr = s.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    start_server()
