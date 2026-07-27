"""
Mock WMS (Warehouse Management System).
Real WMS = proprietary messaging protocol over TCP/IP. Simulated here with
newline-delimited JSON: send one JSON line, get one JSON line back.
"""
import socket
import threading
import json

HOST = "0.0.0.0"
PORT = 6000


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
                if "cancelPackageId" in msg:
                    ack = {"packageId": msg["cancelPackageId"], "status": "CANCELLED"}
                else:
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
