# Unit tests for order-service/auth.py: token issuance/verification and
# the require_auth / require_role decorators. No DB or RabbitMQ needed -
# these are pure JWT + Flask-request-context concerns.
import datetime

import jwt
import pytest
from flask import Flask

from helpers import import_fresh


@pytest.fixture
def auth_module():
    return import_fresh("order-service", "auth")


def _protected_app(auth_module, *roles):
    app = Flask(__name__)

    @app.route("/protected")
    @auth_module.require_auth
    def protected():
        if roles:
            return auth_module.require_role(*roles)(lambda: {"ok": True})()
        return {"ok": True}

    return app


def test_generate_and_decode_token_roundtrip(auth_module):
    token = auth_module.generate_token("alice", "client")
    claims = auth_module.decode_token(token)
    assert claims["sub"] == "alice"
    assert claims["role"] == "client"


def test_decode_expired_token_raises(auth_module):
    payload = {
        "sub": "alice",
        "role": "client",
        "iat": datetime.datetime.utcnow() - datetime.timedelta(hours=3),
        "exp": datetime.datetime.utcnow() - datetime.timedelta(hours=1),
    }
    expired = jwt.encode(payload, auth_module.SECRET_KEY, algorithm="HS256")
    with pytest.raises(jwt.ExpiredSignatureError):
        auth_module.decode_token(expired)


def test_require_auth_rejects_missing_header(auth_module):
    client = _protected_app(auth_module).test_client()
    resp = client.get("/protected")
    assert resp.status_code == 401


def test_require_auth_rejects_malformed_header(auth_module):
    client = _protected_app(auth_module).test_client()
    resp = client.get("/protected", headers={"Authorization": "not-a-bearer-token"})
    assert resp.status_code == 401


def test_require_auth_rejects_invalid_token(auth_module):
    client = _protected_app(auth_module).test_client()
    resp = client.get("/protected", headers={"Authorization": "Bearer garbage.token.value"})
    assert resp.status_code == 401


def test_require_auth_accepts_valid_token(auth_module):
    client = _protected_app(auth_module).test_client()
    token = auth_module.generate_token("alice", "client")
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_require_role_forbidden_for_wrong_role(auth_module):
    client = _protected_app(auth_module, "driver").test_client()
    token = auth_module.generate_token("alice", "client")
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_require_role_allows_matching_role(auth_module):
    client = _protected_app(auth_module, "driver").test_client()
    token = auth_module.generate_token("driver-demo", "driver")
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
