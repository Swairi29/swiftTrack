"""
Mock ROS (Route Optimisation System).
Real ROS = modern, cloud-based, RESTful/JSON. Returns a stub optimised route
built from the real delivery addresses submitted.
"""
import random
import hmac
import os
import time

from dotenv import load_dotenv
from flask import Flask, request, jsonify

app = Flask(__name__)
load_dotenv()
ROS_API_KEY = os.environ.get("ROS_API_KEY")
if not ROS_API_KEY:
    raise RuntimeError("ROS_API_KEY must be set in .env")
# Toggle this on to simulate ROS being down - lets the team demo saga
# compensation and the circuit breaker without killing the process.
FAIL_MODE = {"enabled": False}
# A real route-optimisation call wouldn't return in a few milliseconds.
# Purely so a live demo/screencast can see PENDING 
SIMULATED_LATENCY_SECONDS = 1.5


@app.before_request
def require_api_key():
    if request.path == "/health":
        return None
    if not hmac.compare_digest(request.headers.get("X-API-Key", ""), ROS_API_KEY):
        return jsonify({"error": "ROS API key required"}), 401

@app.route("/routes/toggle-failure", methods=["POST"])
def toggle_failure():
    FAIL_MODE["enabled"] = not FAIL_MODE["enabled"]
    return jsonify({"failMode": FAIL_MODE["enabled"]})


@app.route("/routes/optimize", methods=["POST"])
def optimize_route():
    if FAIL_MODE["enabled"]:
        return jsonify({"error": "ROS temporarily unavailable"}), 503
    time.sleep(SIMULATED_LATENCY_SECONDS)
    data = request.get_json(force=True)
    stops = data.get("deliveryAddresses", [])
    route_id = f"ROS-{random.randint(1000, 9999)}"
    return jsonify({
        "routeId": route_id,
        "optimizedStops": stops,
        "estimatedMinutes": (len(stops) * 12) or 12,
    })


@app.route("/health", methods=["GET"])
def health():
    return {"status": "UP", "system": "mock-ros"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002)
