"""
Smoke tests for WanderWise Engine.

Mocks Gemini to avoid live API calls in CI / local runs.
Run from repo root: pytest -q
"""

from __future__ import annotations

import os

# Set a dummy key BEFORE importing modules that read GEMINI_API_KEY at import time.
os.environ.setdefault("GEMINI_API_KEY", "dummy-key-for-tests")

from datetime import date
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from main import app
from itinerary import ItineraryRequest, parse_itinerary_response


client = TestClient(app)


_VALID_GEMINI_JSON = (
    '{"destination":"Goa","start_date":"2026-05-10","end_date":"2026-05-11",'
    '"budget_inr":15000,"total_estimated_cost_inr":12000,"days":['
    '{"day_number":1,"date":"2026-05-10","theme":"Arrival",'
    '"activities":['
    '{"time":"09:00","activity":"Land","location":"Dabolim",'
    '"estimated_cost_inr":0,"notes":null}'
    ']}'
    ']}'
)


def test_parse_itinerary_response_handles_valid_json():
    """Unit test: the parser builds a correct ItineraryResponse from valid JSON."""
    req = ItineraryRequest(
        destination="Goa",
        start_date=date(2026, 5, 10),
        end_date=date(2026, 5, 11),
        budget_inr=15000,
        preferences="vegetarian",
    )
    result = parse_itinerary_response(_VALID_GEMINI_JSON, req)
    assert result.destination == "Goa"
    assert result.total_estimated_cost_inr == 12000
    assert len(result.days) == 1
    assert result.days[0].activities[0].time == "09:00"


def test_parse_itinerary_response_handles_invalid_json_with_fallback():
    """Unit test: malformed Gemini output produces a fallback day, not a crash."""
    req = ItineraryRequest(
        destination="Goa",
        start_date=date(2026, 5, 10),
        end_date=date(2026, 5, 11),
        budget_inr=15000,
        preferences="vegetarian",
    )
    result = parse_itinerary_response("not json at all", req)
    assert result.destination == "Goa"
    assert len(result.days) == 1
    assert "Parse error" in (result.days[0].activities[0].notes or "")


@patch("itinerary._CLIENT.models.generate_content")
def test_plan_endpoint_returns_itinerary(mock_gen):
    """Integration test: /api/plan calls Gemini (mocked) and returns 200 JSON."""
    mock_response = MagicMock()
    mock_response.text = _VALID_GEMINI_JSON
    mock_gen.return_value = mock_response

    res = client.post(
        "/api/plan",
        json={
            "destination": "Goa",
            "start_date": "2026-05-10",
            "end_date": "2026-05-11",
            "budget_inr": 15000,
            "preferences": "vegetarian, offbeat",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["destination"] == "Goa"
    assert len(data["days"]) == 1


def test_plan_endpoint_rejects_invalid_dates():
    """Integration test: end_date < start_date returns 422 from Pydantic validation."""
    res = client.post(
        "/api/plan",
        json={
            "destination": "Goa",
            "start_date": "2026-05-12",
            "end_date": "2026-05-10",
            "budget_inr": 15000,
            "preferences": "vegetarian",
        },
    )
    assert res.status_code == 422


def test_healthz_endpoint():
    """Smoke: the Cloud Run health probe responds OK."""
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
