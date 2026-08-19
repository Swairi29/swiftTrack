# Tests for order-service/app.py's HTTP-facing behaviour: auth/role
# enforcement, request validation, idempotent order submission, order
# ownership on GET, and the delivery-status update. Postgres is replaced
# with FakeConnection (see helpers.py) and publish_event is mocked out so
# these run with no RabbitMQ/Postgres and no network calls.
from unittest.mock import MagicMock

import pytest

from helpers import FakeConnection, import_fresh


@pytest.fixture
def app_module():
    return import_fresh("order-service", "app")


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


def _client_token(app_module, username="alice"):
    return app_module.generate_token(username, "client")


def _driver_token(app_module, username="driver-demo"):
    return app_module.generate_token(username, "driver")


# --- POST /orders -----------------------------------------------------

def test_orders_requires_auth(client):
    resp = client.post(
        "/orders",
        json={"clientName": "Kandy Traders", "addresses": ["123 Galle Rd"]},
        headers={"Idempotency-Key": "k1"},
    )
    assert resp.status_code == 401


def test_orders_rejects_non_client_role(app_module, client):
    token = _driver_token(app_module)
    resp = client.post(
        "/orders",
        json={"clientName": "Kandy Traders", "addresses": ["123 Galle Rd"]},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k1"},
    )
    assert resp.status_code == 403


def test_orders_requires_idempotency_key_header(app_module, client):
    token = _client_token(app_module)
    resp = client.post(
        "/orders",
        json={"clientName": "Kandy Traders", "addresses": ["123 Galle Rd"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_orders_validation_error_on_missing_fields(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection([None]))
    monkeypatch.setattr(app_module, "publish_event", MagicMock())
    token = _client_token(app_module)
    resp = client.post(
        "/orders",
        json={"clientName": "", "addresses": []},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k1"},
    )
    assert resp.status_code == 400


def test_orders_success_returns_202_and_publishes_event(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection([None]))
    publish_mock = MagicMock()
    monkeypatch.setattr(app_module, "publish_event", publish_mock)
    token = _client_token(app_module)

    resp = client.post(
        "/orders",
        json={"clientName": "Kandy Traders", "addresses": ["123 Galle Rd"]},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k1"},
    )

    assert resp.status_code == 202
    body = resp.get_json()
    assert body["status"] == "PENDING"
    assert body["orderId"]

    publish_mock.assert_called_once()
    routing_key, payload = publish_mock.call_args[0]
    assert routing_key == "order.created"
    assert payload["clientUsername"] == "alice"


def test_orders_idempotent_replay_skips_duplicate_publish(app_module, client, monkeypatch):
    fake_conn = FakeConnection([("ORD-existing",), ("CONFIRMED", "alice")])
    monkeypatch.setattr(app_module, "get_connection", lambda: fake_conn)
    publish_mock = MagicMock()
    monkeypatch.setattr(app_module, "publish_event", publish_mock)
    token = _client_token(app_module)

    resp = client.post(
        "/orders",
        json={"clientName": "Kandy Traders", "addresses": ["123 Galle Rd"]},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "dup-key"},
    )

    assert resp.status_code == 202
    assert resp.get_json() == {"orderId": "ORD-existing", "status": "CONFIRMED"}
    publish_mock.assert_not_called()


def test_orders_idempotency_key_owned_by_other_client_is_rejected(app_module, client, monkeypatch):
    fake_conn = FakeConnection([("ORD-existing",), ("CONFIRMED", "someone-else")])
    monkeypatch.setattr(app_module, "get_connection", lambda: fake_conn)
    monkeypatch.setattr(app_module, "publish_event", MagicMock())
    token = _client_token(app_module)

    resp = client.post(
        "/orders",
        json={"clientName": "Kandy Traders", "addresses": ["123 Galle Rd"]},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "dup-key"},
    )

    assert resp.status_code == 409


# --- GET /orders (history/list) -----------------------------------------

def test_list_orders_requires_auth(client):
    resp = client.get("/orders")
    assert resp.status_code == 401


def test_list_orders_client_scoped_to_own_orders(app_module, client, monkeypatch):
    row = ("ORD-1", "Kandy Traders", "alice", ["123 Galle Rd"], "CONFIRMED",
           "CMS-1", "WMS-1", "ROS-1", None, None, None, None)
    fake_conn = FakeConnection(fetchall_result=[row])
    monkeypatch.setattr(app_module, "get_connection", lambda: fake_conn)
    token = _client_token(app_module, username="alice")

    resp = client.get("/orders", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body) == 1
    assert body[0]["orderId"] == "ORD-1"
    assert body[0]["clientUsername"] == "alice"
    # the query must be scoped to this client, not "all orders"
    _, params = fake_conn._cursor.executed[0]
    assert params == ("alice",)


def test_list_orders_driver_sees_every_clients_orders(app_module, client, monkeypatch):
    rows = [
        ("ORD-1", "Kandy Traders", "alice", ["123 Galle Rd"], "CONFIRMED",
         "CMS-1", "WMS-1", "ROS-1", None, None, None, None),
        ("ORD-2", "Colombo Mart", "bob", ["10 Marine Drive"], "PENDING",
         None, None, None, None, None, None, None),
    ]
    fake_conn = FakeConnection(fetchall_result=rows)
    monkeypatch.setattr(app_module, "get_connection", lambda: fake_conn)
    token = _driver_token(app_module)

    resp = client.get("/orders", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert {o["orderId"] for o in body} == {"ORD-1", "ORD-2"}
    # unscoped query for drivers - no per-client filter parameter
    _, params = fake_conn._cursor.executed[0]
    assert params is None


def test_list_orders_empty_for_new_client(app_module, client, monkeypatch):
    fake_conn = FakeConnection(fetchall_result=[])
    monkeypatch.setattr(app_module, "get_connection", lambda: fake_conn)
    token = _client_token(app_module)

    resp = client.get("/orders", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.get_json() == []


# --- GET /orders/<id> ---------------------------------------------------

def test_get_order_requires_auth(client):
    resp = client.get("/orders/ORD-1")
    assert resp.status_code == 401


def test_get_order_forbidden_for_non_owning_client(app_module, client, monkeypatch):
    row = ("ORD-1", "bob", "CONFIRMED", "CMS-1", "WMS-1", "ROS-1", None, None, None, None)
    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection([row]))
    token = _client_token(app_module, username="alice")

    resp = client.get("/orders/ORD-1", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_get_order_allowed_for_owning_client(app_module, client, monkeypatch):
    row = ("ORD-1", "alice", "CONFIRMED", "CMS-1", "WMS-1", "ROS-1", None, None, None, None)
    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection([row]))
    token = _client_token(app_module, username="alice")

    resp = client.get("/orders/ORD-1", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.get_json()["orderId"] == "ORD-1"


def test_get_order_driver_can_view_any_clients_order(app_module, client, monkeypatch):
    row = ("ORD-1", "alice", "CONFIRMED", "CMS-1", "WMS-1", "ROS-1", None, None, None, None)
    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection([row]))
    token = _driver_token(app_module)

    resp = client.get("/orders/ORD-1", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_get_order_not_found(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection([None]))
    token = _client_token(app_module)

    resp = client.get("/orders/ORD-missing", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


# --- POST /deliveries/<id>/status ---------------------------------------

def test_delivery_status_requires_driver_role(app_module, client):
    token = _client_token(app_module)
    resp = client.post(
        "/deliveries/ORD-1/status",
        json={"status": "DELIVERED"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_delivery_status_rejects_invalid_status_value(app_module, client):
    token = _driver_token(app_module)
    resp = client.post(
        "/deliveries/ORD-1/status",
        json={"status": "MAYBE"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_delivery_status_success_publishes_event(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection([("alice",)]))
    publish_mock = MagicMock()
    monkeypatch.setattr(app_module, "publish_event", publish_mock)
    token = _driver_token(app_module)

    resp = client.post(
        "/deliveries/ORD-1/status",
        json={"status": "DELIVERED"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    publish_mock.assert_called_once()
    routing_key, payload = publish_mock.call_args[0]
    assert routing_key == "delivery.updated"
    assert payload["status"] == "DELIVERED"


def test_delivery_status_order_not_found(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection([None]))
    monkeypatch.setattr(app_module, "publish_event", MagicMock())
    token = _driver_token(app_module)

    resp = client.post(
        "/deliveries/ORD-missing/status",
        json={"status": "DELIVERED"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
