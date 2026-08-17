import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()

def get_connection():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ.get("POSTGRES_DB", "swifttrack"),
        user=os.environ.get("POSTGRES_USER", "swift"),
        password=os.environ.get("POSTGRES_PASSWORD"),
    )

