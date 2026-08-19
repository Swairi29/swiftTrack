# Test-only defaults for the env vars every service module requires at
# import time (JWT_SECRET_KEY, RabbitMQ credentials, backend service
# credentials). Real values live in .env / .env.example; these are never
# used outside the test run. Set with setdefault so a developer's real
# .env (loaded via python-dotenv inside the service modules themselves)
# can still override them if present.
import os

os.environ.setdefault("JWT_SECRET_KEY", "test-only-jwt-secret-do-not-use-in-prod-0123456789")
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:5500")
os.environ.setdefault("RABBITMQ_USER", "test-rabbitmq-user")
os.environ.setdefault("RABBITMQ_PASSWORD", "test-rabbitmq-password")
os.environ.setdefault("CMS_USERNAME", "test-cms-user")
os.environ.setdefault("CMS_PASSWORD", "test-cms-password")
os.environ.setdefault("ROS_API_KEY", "test-ros-api-key")
os.environ.setdefault("WMS_AUTH_TOKEN", "test-wms-token")
