"""
Mock ROS (Route Optimisation System).
Real ROS = modern, cloud-based, RESTful/JSON. Returns a stub optimised route
built from the real delivery addresses submitted.
"""
from flask import Flask, request, jsonify
import random

app = Flask(__name__)
# Toggle this on to simulate ROS being down - lets the team demo saga
# compensation and the circuit breaker without killing the process.
FAIL_MODE = {"enabled": False}

@app.route("/routes/toggle-failure", methods=["POST"])
def toggle_failure():
    FAIL_MODE["enabled"] = not FAIL_MODE["enabled"]
    return jsonify({"failMode": FAIL_MODE["enabled"]})


@app.route("/routes/optimize", methods=["POST"])
def optimize_route():
    if FAIL_MODE["enabled"]:
        return jsonify({"error": "ROS temporarily unavailable"}), 503
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
    app.run(port=5002)
