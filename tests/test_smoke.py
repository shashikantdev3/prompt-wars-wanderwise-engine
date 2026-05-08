"""
Smoke tests for WanderWise Engine.

Mocks Gemini and Maps clients to avoid live API calls in CI / local runs.
Run from repo root: pytest -q
"""

from __future__ import annotations

import os

# Set dummy keys BEFORE importing modules that read env at import time.
os.environ.setdefault("GEMINI_API_KEY", "dummy-key-for-tests")
os.environ.setdefault("MAPS_API_KEY", "")  # Maps disabled in tests by default

from datetime import date
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from main import app
from itinerary import (
    ItineraryRequest,
    ItineraryResponse,
    Activity,
    Day,
    parse_itinerary_response,
    validate_constraints,
)


client = TestClient(app)


_VALID_GEMINI_JSON = (
    '{"destination":"Goa","start_date":"2026-05-10","end_date":"2026-05-11",'
    '"budget_inr":15000,"total_estimated_cost_inr":12000,"days":['
    '{"day_number":1,"date":"2026-05-10","theme":"Arrival",'
    '"activities":['
    '{"time":"09:00","activity":"Land at Dabolim","location":"Dabolim Airport",'
    '"estimated_cost_inr":0,"notes":null},'
    '{"time":"19:00","activity":"Vegetarian dinner at Vinayak","location":"Vinayak Family Restaurant",'
    '"estimated_cost_inr":1500,"notes":null}'
    ']},'
    '{"day_number":2,"date":"2026-05-11","theme":"Beach day",'
    '"activities":['
    '{"time":"10:00","activity":"Anjuna Beach walk","location":"Anjuna Beach",'
    '"estimated_cost_inr":0,"notes":null},'
    '{"time":"19:00","activity":"Sunset thali","location":"Plantain Leaf",'
    '"estimated_cost_inr":10500,"notes":null}'
    ']}'
    ']}'
)


# ---------- Parser ----------

def test_parse_itinerary_response_handles_valid_json():
    """Parser builds a correct ItineraryResponse from valid JSON."""
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
    assert len(result.days) == 2
    assert result.days[0].activities[0].time == "09:00"


def test_parse_itinerary_response_handles_invalid_json_with_fallback():
    """Malformed Gemini output produces a fallback day, not a crash."""
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


# ---------- Constraint validator ----------

def _build_itinerary(total_cost: int, num_days: int = 2) -> ItineraryResponse:
    """Helper: build a minimal valid ItineraryResponse for constraint tests."""
    activities = [
        Activity(
            time="09:00",
            activity="Test activity",
            location="Test location",
            estimated_cost_inr=total_cost // num_days,
        )
    ]
    days = [
        Day(
            day_number=i + 1,
            date=date(2026, 5, 10 + i),
            theme="Test theme",
            activities=activities,
        )
        for i in range(num_days)
    ]
    return ItineraryResponse(
        destination="Goa",
        start_date=date(2026, 5, 10),
        end_date=date(2026, 5, 9 + num_days),
        budget_inr=15000,
        total_estimated_cost_inr=total_cost,
        days=days,
    )


def test_validate_constraints_flags_over_budget():
    """Total cost exceeding budget is flagged as a blocker."""
    req = ItineraryRequest(
        destination="Goa",
        start_date=date(2026, 5, 10),
        end_date=date(2026, 5, 11),
        budget_inr=10000,
        preferences="vegetarian",
    )
    itinerary = _build_itinerary(total_cost=15000, num_days=2)
    violations = validate_constraints(itinerary, req)
    assert any(v.type == "budget" and v.severity == "blocker" for v in violations)


def test_validate_constraints_flags_day_count_mismatch():
    """A 2-day itinerary against a 3-day request is flagged."""
    req = ItineraryRequest(
        destination="Goa",
        start_date=date(2026, 5, 10),
        end_date=date(2026, 5, 12),  # 3 days expected
        budget_inr=20000,
        preferences="vegetarian",
    )
    itinerary = _build_itinerary(total_cost=10000, num_days=2)
    violations = validate_constraints(itinerary, req)
    assert any(v.type == "day_count_mismatch" for v in violations)


def test_validate_constraints_clean_itinerary_has_no_blockers():
    """A well-formed itinerary inside budget produces no blocker violations."""
    req = ItineraryRequest(
        destination="Goa",
        start_date=date(2026, 5, 10),
        end_date=date(2026, 5, 11),
        budget_inr=15000,
        preferences="general",
    )
    itinerary = _build_itinerary(total_cost=12000, num_days=2)
    violations = validate_constraints(itinerary, req)
    assert not any(v.severity == "blocker" for v in violations)


# ---------- /api/plan integration ----------

@patch("itinerary._CLIENT.models.generate_content")
def test_plan_endpoint_returns_itinerary_with_session_id(mock_gen):
    """/api/plan returns 200, valid JSON, and a session_id for refinement."""
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
    assert len(data["days"]) == 2
    assert data["session_id"]  # session id minted
    assert "constraint_violations" in data


def test_plan_endpoint_rejects_invalid_dates():
    """end_date < start_date returns 422."""
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


# ---------- /api/refine integration ----------

@patch("itinerary._CLIENT.models.generate_content")
def test_refine_endpoint_revises_existing_itinerary(mock_gen):
    """/api/refine takes a session id and an instruction, returns a revised plan."""
    mock_response = MagicMock()
    mock_response.text = _VALID_GEMINI_JSON
    mock_gen.return_value = mock_response

    # First create a session via /api/plan
    plan_res = client.post(
        "/api/plan",
        json={
            "destination": "Goa",
            "start_date": "2026-05-10",
            "end_date": "2026-05-11",
            "budget_inr": 15000,
            "preferences": "vegetarian",
        },
    )
    session_id = plan_res.json()["session_id"]

    # Now refine
    refine_res = client.post(
        "/api/refine",
        json={"session_id": session_id, "instruction": "make it cheaper"},
    )
    assert refine_res.status_code == 200
    data = refine_res.json()
    assert data["session_id"] == session_id
    assert data["destination"] == "Goa"


def test_refine_endpoint_returns_404_for_unknown_session():
    """Refine on an unknown session id returns 404."""
    res = client.post(
        "/api/refine",
        json={"session_id": "no-such-session", "instruction": "make it cheaper"},
    )
    assert res.status_code == 404


# ---------- Health probe ----------

def test_healthz_endpoint_reports_service_state():
    """Health probe responds OK and exposes service self-description."""
    res = client.get("/healthz")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["service"] == "wanderwise-engine"
    assert "maps_enabled" in body
    assert "active_sessions" in body
