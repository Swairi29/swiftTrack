import os

import psycopg2
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()


def get_connection():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ.get("POSTGRES_DB", "swifttrack"),
        user=os.environ.get("POSTGRES_USER", "swift"),
        password=os.environ.get("POSTGRES_PASSWORD"),
    )


def _required_setting(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set. Copy .env.example to .env and configure it.")
    return value


def ensure_demo_users():
    """Create local demo users once. PostgreSQL stores password hashes only."""
    users = (
        (_required_setting("CLIENT_USERNAME"), _required_setting("CLIENT_PASSWORD"), "client"),
        (_required_setting("DRIVER_USERNAME"), _required_setting("DRIVER_PASSWORD"), "driver"),
    )
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS client_username TEXT")
            cur.execute("""CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('client', 'driver')),
                created_at TIMESTAMP NOT NULL DEFAULT now()
            )""")
            for username, password, role in users:
                cur.execute(
                    """INSERT INTO users (username, password_hash, role)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (username) DO UPDATE
                       SET password_hash = EXCLUDED.password_hash, role = EXCLUDED.role""",
                    (username, generate_password_hash(password), role),
                )
        conn.commit()
    finally:
        conn.close()


def authenticate_user(username, password):
    ensure_demo_users()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash, role FROM users WHERE username = %s", (username,))
            row = cur.fetchone()
    finally:
        conn.close()
    if row and check_password_hash(row[0], password):
        return {"username": username, "role": row[1]}
    return None
