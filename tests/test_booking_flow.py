import os
import sys
from datetime import date

# ensure project root on path
sys.path.append(os.path.dirname(os.path.dirname(__file__)))


class DummyResp:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def test_booking_flow(monkeypatch):
    os.environ["WP_BASE_URL"] = "https://example.com"
    os.environ["BUSINESS_PHONE"] = "+10000000000"
    os.environ["AI_API_KEY"] = "token"

    import app

    def fake_get(url, params=None, headers=None, timeout=None):
        assert url == "https://example.com/wp-json/ai-reception/v1/ai/availability"
        assert params["phone"] == "+10000000000"
        return DummyResp({"slots": [{"room_id": "room1", "start_ts": "2024-06-01T15:00:00Z", "end_ts": "2024-06-01T16:00:00Z"}]})

    def fake_post(url, params=None, json=None, headers=None, timeout=None):
        if url == "https://example.com/wp-json/ai-reception/v1/ai/hold":
            return DummyResp({"hold_id": "h1"})
        if url == "https://example.com/wp-json/ai-reception/v1/ai/confirm":
            return DummyResp({"booking_id": 123})
        return DummyResp({})

    monkeypatch.setattr(app.requests, "get", fake_get)
    monkeypatch.setattr(app.requests, "post", fake_post)

    avail = app.check_availability("2024-06-01T00:00:00Z", "2024-06-02T00:00:00Z", 2, "svc1")
    assert avail["slots"][0]["room_id"] == "room1"

    hold = app.hold_slot("room1", "svc1", "2024-06-01T15:00:00Z", "2024-06-01T16:00:00Z", 2, "Alice", "+123")
    assert hold["hold_id"] == "h1"

    confirm = app.confirm_hold("h1", "room1", 2, "Alice", "+123", "a@example.com")
    assert confirm["booking_id"] == 123


def test_find_consecutive_nights(monkeypatch):
    import app

    def fake_check(from_iso, to_iso, party, service_id, room_id="any"):
        day = from_iso[:10]
        return {"slots": [{"room_id": "room1", "start_ts": f"{day}T15:00:00Z", "end_ts": f"{day}T16:00:00Z"}]}

    monkeypatch.setattr(app, "check_availability", fake_check)

    options = app.find_consecutive_nights(date(2024, 6, 1), 2, 2, "svc1")
    assert options[0]["room_id"] == "room1"
    assert options[0]["end_ts"] == "2024-06-03T15:00:00Z"
