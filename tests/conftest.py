"""Shared test configuration — ensure project root is on sys.path and stub heavy deps."""
import sys
import os
import types
import importlib.util
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _has_module(mod_name: str) -> bool:
    try:
        return importlib.util.find_spec(mod_name) is not None
    except (ImportError, ValueError):
        return False


# Stub optional dependencies only when they are not installed. Do not replace
# real FastAPI/Starlette/Pydantic modules: route tests import their subpackages.
for mod_name in [
    "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.types", "sqlalchemy.ext", "sqlalchemy.ext.declarative",
    "sqlalchemy.ext.hybrid", "sqlalchemy.sql", "sqlalchemy.sql.expression",
    "sqlalchemy.sql.sqltypes", "bcrypt", "pyotp",
    "httpx", "fastapi", "fastapi.responses", "fastapi.routing",
    "starlette", "starlette.responses", "starlette.middleware", "starlette.middleware.base",
    "pydantic",
]:
    if mod_name not in sys.modules and not _has_module(mod_name):
        sys.modules[mod_name] = MagicMock()

if "src.database" not in sys.modules:
    _db = types.ModuleType("src.database")
    _db.SessionLocal = MagicMock()
    _db.ModelEndpoint = MagicMock()
    sys.modules["src.database"] = _db

# Pre-import the real lightweight core modules when their dependencies exist.
# Several test files stub these at module level with an "if mod not in
# sys.modules" guard (test_agent_loop.py, test_llm_core_sanitize_tool_calls.py,
# ...). Collection order made those stubs win for the WHOLE session, so
# later-collected tests that need the real Session/ChatMessage/ScheduledTask/
# AuthManager classes failed only in full runs (classic cross-test pollution).
# With the real modules already in sys.modules those guards become no-ops.
if _has_module("sqlalchemy"):
    import sqlalchemy  # noqa: F401
    import core.models  # noqa: F401
    import core.database  # noqa: F401
if _has_module("bcrypt"):
    import core.auth  # noqa: F401
