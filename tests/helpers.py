# Every service directory (order-service/, saga-worker/, ...) is a plain
# script folder, not an importable package (hyphens aren't legal in
# Python package names), and several of them define same-named modules
# (app.py, db.py). import_fresh() loads a given module from a given
# service directory in isolation: it clears any same-named module already
# in sys.modules, puts that one directory at the front of sys.path just
# long enough to import it (so its own bare `from db import ...` /
# `from auth import ...` resolve to its own sibling files, not another
# service's), then removes the path entry again.
import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Names that collide across service directories and must never be served
# from a stale sys.modules entry belonging to a different service.
_RESET_MODULE_NAMES = ("app", "db", "auth", "worker")


def import_fresh(service_dir, module_name):
    dirpath = os.path.join(ROOT, service_dir)
    for name in _RESET_MODULE_NAMES:
        sys.modules.pop(name, None)
    sys.path.insert(0, dirpath)
    try:
        return importlib.import_module(module_name)
    finally:
        sys.path.remove(dirpath)


class FakeCursor:
    """Stands in for a psycopg2 cursor. fetchone() returns queued results
    in call order, matching how each route issues its `with conn.cursor()`
    blocks sequentially in the source. fetchall() returns a single fixed
    result list (routes here only ever call it once per request)."""

    def __init__(self, fetchone_results=(), fetchall_result=None):
        self._fetchone_results = list(fetchone_results)
        self._fetchall_result = list(fetchall_result) if fetchall_result is not None else []
        self.executed = []
        self.rowcount = 1

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        if self._fetchone_results:
            return self._fetchone_results.pop(0)
        return None

    def fetchall(self):
        return self._fetchall_result

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeConnection:
    """Stands in for a psycopg2 connection. One shared cursor instance is
    returned from every conn.cursor() call, so a route's several
    sequential `with conn.cursor() as cur:` blocks all draw from the same
    fetchone()/fetchall() queue, same as they would against a real
    cursor."""

    def __init__(self, fetchone_results=(), fetchall_result=None):
        self._cursor = FakeCursor(fetchone_results, fetchall_result)
        self.committed = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True
