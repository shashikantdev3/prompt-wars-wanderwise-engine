"""
WanderWise itinerary generation — Gemini-backed, Maps-grounded.

Public surface:
- generate_itinerary(req)             : initial plan (validated + Maps-enriched)
- refine_itinerary(prev, instruction, original_req): conversational re-plan
- validate_constraints(itinerary, req): post-Gemini hard-constraint check
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import List, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, field_validator, model_validator

import maps as maps_module

logger = logging.getLogger("wanderwise.itinerary")


# ---------- Gemini client (configured once at import time) ----------

_API_KEY = os.environ.get("GEMINI_API_KEY")
if not _API_KEY:
    raise ValueError(
        "GEMINI_API_KEY is not set. "
        "For local dev, copy .env.example to .env and add your key. "
        "For Cloud Run, inject via Secret Manager."
    )

_CLIENT = genai.Client(api_key=_API_KEY)
_MODEL_NAME = "gemini-2.5-flash"


# ---------- Pydantic models ----------

class Activity(BaseModel):
    time: str = Field(..., description="HH:MM 24-hour")
    activity: str
    location: str
    estimated_cost_inr: int = Field(ge=0)
    notes: Optional[str] = None
    # Maps enrichment (best-effort, may be None)
    place_id: Optional[str] = None
    formatted_address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    is_accessible: Optional[bool] = None


class Day(BaseModel):
    day_number: int = Field(ge=1)
    date: date
    theme: str
    activities: List[Activity]


class ItineraryRequest(BaseModel):
    destination: str = Field(min_length=2, max_length=200)
    start_date: date
    end_date: date
    budget_inr: int = Field(gt=0, le=10_000_000)
    preferences: str = Field(min_length=1, max_length=1000)

    @field_validator("destination", "preferences")
    @classmethod
    def _strip_and_check_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Field cannot be empty or whitespace only")
        return v

    @model_validator(mode="after")
    def _check_date_order(self) -> "ItineraryRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class RefineRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    instruction: str = Field(min_length=1, max_length=500)

    @field_validator("instruction")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("instruction cannot be empty")
        return v


class ConstraintViolation(BaseModel):
    type: str
    message: str
    severity: str = "warning"  # "warning" | "blocker"


class ItineraryResponse(BaseModel):
    session_id: Optional[str] = None
    destination: str
    start_date: date
    end_date: date
    budget_inr: int
    total_estimated_cost_inr: int
    days: List[Day]
    constraint_violations: List[ConstraintViolation] = []
    maps_enriched: bool = False  # transparency: did we ground against Maps?


# ---------- Prompt construction ----------

_SYSTEM_INSTRUCTION = (
    "You are a precise travel planning assistant. You return ONLY valid JSON "
    "matching the schema requested. Hard constraints (budget, dietary, mobility) "
    "are inviolable. Preferences (vibe, cuisine) are guidance. All costs are in "
    "INR. All times are 24-hour HH:MM. Use real, named places — no placeholders."
)


def _build_initial_prompt(req: ItineraryRequest) -> str:
    num_days = (req.end_date - req.start_date).days + 1
    return (
        f"Plan a {num_days}-day trip. Return JSON with this shape:\n\n"
        "{\n"
        f'  "destination": "{req.destination}",\n'
        f'  "start_date": "{req.start_date.isoformat()}",\n'
        f'  "end_date": "{req.end_date.isoformat()}",\n'
        f'  "budget_inr": {req.budget_inr},\n'
        f'  "total_estimated_cost_inr": <integer, total trip cost INR, must be <= {req.budget_inr}>,\n'
        '  "days": [\n'
        '    {\n'
        f'      "day_number": <1-{num_days}>,\n'
        '      "date": "<YYYY-MM-DD>",\n'
        '      "theme": "<3-6 word theme>",\n'
        '      "activities": [\n'
        '        {\n'
        '          "time": "<HH:MM>",\n'
        '          "activity": "<short description>",\n'
        '          "location": "<specific real place name>",\n'
        '          "estimated_cost_inr": <integer>,\n'
        '          "notes": "<optional note or null>"\n'
        '        }\n'
        '      ]\n'
        '    }\n'
        '  ]\n'
        "}\n\n"
        f"Trip details:\n"
        f"- Destination: {req.destination}\n"
        f"- Dates: {req.start_date.isoformat()} to {req.end_date.isoformat()} ({num_days} days)\n"
        f"- Hard budget ceiling: INR {req.budget_inr}\n"
        f"- Preferences and constraints: {req.preferences}\n\n"
        "Rules:\n"
        "- 3 to 6 activities per day.\n"
        "- Total cost MUST be <= budget.\n"
        "- Respect dietary, mobility, and any other hard constraint.\n"
        "- Use real places. No 'TBD' or placeholders."
    )


def _build_refine_prompt(
    current: ItineraryResponse,
    instruction: str,
    original_req: ItineraryRequest,
) -> str:
    num_days = (original_req.end_date - original_req.start_date).days + 1
    return (
        "You are revising an existing travel itinerary based on a user's instruction. "
        "Return the FULL revised itinerary as JSON in the same schema as before. "
        "Preserve every original hard constraint unless the instruction explicitly relaxes it.\n\n"
        f"USER INSTRUCTION: {instruction}\n\n"
        f"ORIGINAL HARD CONSTRAINTS (still apply):\n"
        f"- Destination: {original_req.destination}\n"
        f"- Dates: {original_req.start_date.isoformat()} to {original_req.end_date.isoformat()} ({num_days} days)\n"
        f"- Hard budget ceiling: INR {original_req.budget_inr}\n"
        f"- Preferences: {original_req.preferences}\n\n"
        "CURRENT ITINERARY:\n"
        f"{current.model_dump_json(indent=2)}\n\n"
        "Rules for revision:\n"
        "- Return JSON in the same shape.\n"
        "- Keep day_number and date the same unless the instruction requires otherwise.\n"
        "- Total cost MUST stay <= budget.\n"
        "- Keep real place names. No 'TBD'.\n"
        "- If the instruction conflicts with a hard constraint, apply the instruction PARTIALLY in a way that respects the constraint, and add a note in the affected activity."
    )


# ---------- Gemini call helpers ----------

def _call_gemini(prompt: str) -> str:
    response = _CLIENT.models.generate_content(
        model=_MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_INSTRUCTION,
            temperature=0.7,
            response_mime_type="application/json",
        ),
    )
    return response.text or ""


# ---------- Public functions ----------

def generate_itinerary(req: ItineraryRequest) -> ItineraryResponse:
    """Generate, validate, enrich. Never raises on Gemini parse failure — returns degraded response."""
    raw = _call_gemini(_build_initial_prompt(req))
    itinerary = parse_itinerary_response(raw, req)
    itinerary = enrich_with_maps(itinerary)
    itinerary.constraint_violations = validate_constraints(itinerary, req)
    return itinerary


def refine_itinerary(
    current: ItineraryResponse,
    instruction: str,
    original_req: ItineraryRequest,
) -> ItineraryResponse:
    """Re-plan an existing itinerary against a free-text user instruction."""
    raw = _call_gemini(_build_refine_prompt(current, instruction, original_req))
    revised = parse_itinerary_response(raw, original_req)
    revised.session_id = current.session_id  # preserve session linkage
    revised = enrich_with_maps(revised)
    revised.constraint_violations = validate_constraints(revised, original_req)
    return revised


def parse_itinerary_response(text: str, req: ItineraryRequest) -> ItineraryResponse:
    """Parse JSON; on failure, return a degraded one-day blob with the raw text surfaced."""
    try:
        data = json.loads(text)
        return ItineraryResponse(**data)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Gemini parse failure: %s", exc)
        return ItineraryResponse(
            destination=req.destination,
            start_date=req.start_date,
            end_date=req.end_date,
            budget_inr=req.budget_inr,
            total_estimated_cost_inr=0,
            days=[
                Day(
                    day_number=1,
                    date=req.start_date,
                    theme="Itinerary (parse fallback)",
                    activities=[
                        Activity(
                            time="00:00",
                            activity=text[:500] if text else "No content returned",
                            location=req.destination,
                            estimated_cost_inr=0,
                            notes=f"Parse error: {exc}",
                        )
                    ],
                )
            ],
        )


def enrich_with_maps(itinerary: ItineraryResponse) -> ItineraryResponse:
    """
    For each activity, look up its location via Google Maps Places API and
    attach place_id, coords, and accessibility flag. Best-effort: never fails.
    """
    if not maps_module.is_enabled():
        itinerary.maps_enriched = False
        return itinerary

    enriched_count = 0
    for day in itinerary.days:
        for activity in day.activities:
            place = maps_module.find_place(activity.location, near=itinerary.destination)
            if place:
                activity.place_id = place.get("place_id")
                activity.formatted_address = place.get("formatted_address")
                activity.latitude = place.get("latitude")
                activity.longitude = place.get("longitude")
                activity.is_accessible = place.get("is_accessible")
                enriched_count += 1

    itinerary.maps_enriched = enriched_count > 0
    logger.info("Maps enriched %d activities", enriched_count)
    return itinerary


def validate_constraints(
    itinerary: ItineraryResponse, req: ItineraryRequest
) -> List[ConstraintViolation]:
    """
    Post-Gemini hard-constraint check. Returns a list of violations to surface
    in the response. Logical decision-making: we tell the user what's broken
    rather than silently failing.
    """
    violations: List[ConstraintViolation] = []

    # Budget
    if itinerary.total_estimated_cost_inr > req.budget_inr:
        violations.append(
            ConstraintViolation(
                type="budget",
                message=(
                    f"Total estimated cost (₹{itinerary.total_estimated_cost_inr:,}) "
                    f"exceeds budget ceiling (₹{req.budget_inr:,})."
                ),
                severity="blocker",
            )
        )

    # Activity-cost rollup sanity check
    rolled_up = sum(
        a.estimated_cost_inr for d in itinerary.days for a in d.activities
    )
    if abs(rolled_up - itinerary.total_estimated_cost_inr) > max(
        100, int(0.05 * itinerary.total_estimated_cost_inr)
    ):
        violations.append(
            ConstraintViolation(
                type="cost_rollup_mismatch",
                message=(
                    f"Sum of activity costs (₹{rolled_up:,}) does not match the "
                    f"reported total (₹{itinerary.total_estimated_cost_inr:,})."
                ),
                severity="warning",
            )
        )

    # Date span
    expected_days = (req.end_date - req.start_date).days + 1
    if len(itinerary.days) != expected_days:
        violations.append(
            ConstraintViolation(
                type="day_count_mismatch",
                message=(
                    f"Expected {expected_days} day(s) based on the date range, "
                    f"got {len(itinerary.days)}."
                ),
                severity="blocker",
            )
        )

    # Dietary heuristic — if user mentions vegetarian, scan for non-veg keywords.
    prefs_lower = req.preferences.lower()
    if "vegetarian" in prefs_lower or "vegan" in prefs_lower:
        non_veg_keywords = [
            "beef", "pork", "chicken", "mutton", "lamb", "fish",
            "prawn", "shrimp", "crab", "seafood",
        ]
        offending: List[str] = []
        for d in itinerary.days:
            for a in d.activities:
                text = f"{a.activity} {a.notes or ''}".lower()
                for kw in non_veg_keywords:
                    if kw in text:
                        offending.append(f"Day {d.day_number}: {a.activity}")
                        break
        if offending:
            violations.append(
                ConstraintViolation(
                    type="dietary",
                    message=(
                        "Vegetarian/vegan constraint may be violated by: "
                        + "; ".join(offending[:3])
                        + ("..." if len(offending) > 3 else ".")
                    ),
                    severity="warning",
                )
            )

    return violations
