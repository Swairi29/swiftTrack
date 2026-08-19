"""
Mock CMS (Client Management System).
Real CMS = legacy on-prem, SOAP/XML. Parses the real order XML it
receives (client name + addresses) so the Saga Worker's CMS call is
doing real protocol/data translation, not sending a hardcoded stub.

Fix applied in this pass: XML parsing now goes through defusedxml
rather than the standard library's xml.etree.ElementTree, which is
vulnerable to XXE if the input can ever be influenced by an attacker.
"""
import hmac
import os
import time

from dotenv import load_dotenv
from flask import Flask, request, Response, jsonify
from defusedxml import ElementTree as ET  # hardened against XXE

app = Flask(__name__)
load_dotenv()
CMS_USERNAME = os.environ.get("CMS_USERNAME")
CMS_PASSWORD = os.environ.get("CMS_PASSWORD")
if not CMS_USERNAME or not CMS_PASSWORD:
    raise RuntimeError("CMS_USERNAME and CMS_PASSWORD must be set in .env")

# A real legacy CMS wouldn't respond in a few milliseconds. This is purely
# so a live demo/screencast can actually see the order sit at PENDING for
# a moment instead of the saga completing too fast to watch.
SIMULATED_LATENCY_SECONDS = 1.0


@app.before_request
def require_basic_auth():
    if request.path == "/health":
        return None
    auth = request.authorization
    valid = bool(auth and hmac.compare_digest(auth.username or "", CMS_USERNAME)
                 and hmac.compare_digest(auth.password or "", CMS_PASSWORD))
    if not valid:
        return jsonify({"error": "CMS authentication required"}), 401, {"WWW-Authenticate": 'Basic realm="SwiftTrack CMS"'}


@app.route("/cms/order", methods=["POST"])
def create_order():
    try:
        root = ET.fromstring(request.data)
        client_name = root.findtext("ClientName", default="UNKNOWN")
        address_count = len(root.findall("./Addresses/Address"))
    except ET.ParseError:
        return Response("<Error>Malformed order XML</Error>", mimetype="text/xml", status=400)

    time.sleep(SIMULATED_LATENCY_SECONDS)
    order_id = f"CMS-{abs(hash(client_name + str(address_count))) % 100000}"
    response_xml = f"""<?xml version="1.0"?>
<OrderResponse>
  <OrderId>{order_id}</OrderId>
  <ClientName>{client_name}</ClientName>
  <Status>ACCEPTED</Status>
</OrderResponse>"""
    return Response(response_xml, mimetype="text/xml")


@app.route("/cms/order/cancel", methods=["POST"])
def cancel_order():
    data = request.get_json(force=True)
    return jsonify({"cancelled": True, "orderId": data.get("orderId")})


@app.route("/health", methods=["GET"])
def health():
    return {"status": "UP", "system": "mock-cms"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
