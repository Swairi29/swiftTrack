"""
Minimal JWT auth for the prototype. In a full deployment this belongs at
the API Gateway; here it is folded into the Order Service to keep the
prototype's moving parts manageable - this simplification should be
stated explicitly in the documentation, not left implicit.
"""
import datetime
import os
from functools import wraps

import jwt
from dotenv import load_dotenv
from flask import request, jsonify

load_dotenv()

SECRET_KEY = os.environ.get("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "JWT_SECRET_KEY is not set. Copy .env.example to .env and set a real "
        "secret (python -c \"import secrets; print(secrets.token_hex(32))\")."
    )


def generate_token(username):
    payload = {
        "sub": username,
        "iat": datetime.datetime.utcnow(),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=2),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or malformed Authorization header"}), 401
        token = auth_header.split(" ", 1)[1]
        try:
            jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return wrapper