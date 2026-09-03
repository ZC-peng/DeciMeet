"""Regression checks for features intentionally excluded from the public runtime."""

from app import models as _models  # noqa: F401 - populate SQLAlchemy metadata
from app.db.base import Base
from app.main import app


def test_retired_realtime_routes_are_not_published() -> None:
    paths = {route.path for route in app.routes}

    assert not any(path.startswith("/api/rooms") for path in paths)
    assert "/api/sfu/health" not in paths
    assert not any(path.startswith("/api/realtime") for path in paths)


def test_retired_realtime_tables_are_not_in_model_metadata() -> None:
    assert "rooms" not in Base.metadata.tables
    assert "realtime_sessions" not in Base.metadata.tables
