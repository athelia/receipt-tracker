from fastapi.testclient import TestClient

from receipt_tracker import __version__
from receipt_tracker.web.app import create_app


def test_health():
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
