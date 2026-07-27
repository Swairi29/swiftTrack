"""
Mock CMS (Client Management System).
Real CMS = legacy on-prem, SOAP/XML. Unlike the earlier draft, this mock
actually parses the order XML it receives (client name + addresses) so the
Saga Worker's CMS call is doing real protocol/data translation, not sending
a hardcoded stub regardless of input.
"""
from flask import Flask, request, Response, jsonify
import xml.etree.ElementTree as ET

app = Flask(__name__)


@app.route("/cms/order", methods=["POST"])
def create_order():
    try:
        root = ET.fromstring(request.data)
        client_name = root.findtext("ClientName", default="UNKNOWN")
        address_count = len(root.findall("./Addresses/Address"))
    except ET.ParseError:
        return Response("<Error>Malformed order XML</Error>", mimetype="text/xml", status=400)

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
    # Not called yet this week - the Saga Worker is straight-line for now.
    # Wired up in Week 3 once compensation is added.
    data = request.get_json(force=True)
    return jsonify({"cancelled": True, "orderId": data.get("orderId")})


@app.route("/health", methods=["GET"])
def health():
    return {"status": "UP", "system": "mock-cms"}


if __name__ == "__main__":
    app.run(port=5001)
