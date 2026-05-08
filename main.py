"""
WanderWise Engine — FastAPI app.

Endpoints:
  GET  /                  Static frontend
  GET  /healthz           Cloud Run health probe
  POST /api/plan          Generate initial itinerary
  POST /api/refine        Conversational re-plan of an existing itinerary
  GET  /api/sessions/{id} Retrieve current itinerary by session id

Sessions are kept in an in-memory dict for the warm-up scope. Cloud Run
serves a single instance under hackathon load, so this is sufficient.
Submission 3 will lift this to Firestore for true multi-instance correctness.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv

# Load .env BEFORE importing modules that read env at module-load time.
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from itinerary import (
    ItineraryRequest,
    ItineraryResponse,
    RefineRequest,
    generate_itinerary,
    refine_itinerary,
)
import maps as maps_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("wanderwise")

app = FastAPI(
    title="WanderWise Engine",
    version="0.2.0",
    description="Smart, dynamic travel planning. PromptWars Gurgaon 2026.",
)


# In-memory session store: session_id -> (current_itinerary, original_request)
# Pair tuple so refine can re-validate against original constraints.
_SESSIONS: Dict[str, tuple] = {}


@app.get("/healthz")
def healthz() -> dict:
    """Cloud Run health probe + service self-description."""
    return {
        "status": "ok",
        "service": "wanderwise-engine",
        "version": "0.2.0",
        "maps_enabled": maps_module.is_enabled(),
        "active_sessions": len(_SESSIONS),
    }


@app.post("/api/plan", response_model=ItineraryResponse)
def plan(req: ItineraryRequest) -> ItineraryResponse:
    """Generate a fresh itinerary. Returns it with a session_id for refinement."""
    num_days = (req.end_date - req.start_date).days + 1
    logger.info(
        "plan_request destination=%s days=%d budget=%d",
        req.destination, num_days, req.budget_inr,
    )
    try:
        itinerary = generate_itinerary(req)
    except Exception as exc:
        logger.exception("Gemini call failed in /api/plan")
        raise HTTPException(
            status_code=503,
            detail="Itinerary generation temporarily unavailable. Please retry.",
        ) from exc

    # Mint a session id and store both the itinerary and the original request
    session_id = str(uuid.uuid4())
    itinerary.session_id = session_id
    _SESSIONS[session_id] = (itinerary, req)

    logger.info(
        "plan_response session_id=%s violations=%d maps_enriched=%s",
        session_id, len(itinerary.constraint_violations), itinerary.maps_enriched,
    )
    return itinerary


@app.post("/api/refine", response_model=ItineraryResponse)
def refine(req: RefineRequest) -> ItineraryResponse:
    """Conversationally re-plan an existing itinerary."""
    bundle = _SESSIONS.get(req.session_id)
    if bundle is None:
        raise HTTPException(
            status_code=404,
            detail="Session not found. Generate a new plan first.",
        )

    current_itinerary, original_request = bundle
    logger.info(
        "refine_request session_id=%s instruction=%r",
        req.session_id, req.instruction[:120],
    )

    try:
        revised = refine_itinerary(
            current=current_itinerary,
            instruction=req.instruction,
            original_req=original_request,
        )
    except Exception as exc:
        logger.exception("Gemini call failed in /api/refine")
        raise HTTPException(
            status_code=503,
            detail="Refinement temporarily unavailable. Please retry.",
        ) from exc

    # Update session with the revised itinerary
    _SESSIONS[req.session_id] = (revised, original_request)
    logger.info(
        "refine_response session_id=%s violations=%d",
        req.session_id, len(revised.constraint_violations),
    )
    return revised


@app.get("/api/sessions/{session_id}", response_model=ItineraryResponse)
def get_session(session_id: str) -> ItineraryResponse:
    """Retrieve current itinerary by session id (used for page reload recovery)."""
    bundle = _SESSIONS.get(session_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return bundle[0]


# ---------- Static frontend ----------

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/")
def root() -> FileResponse:
    """Serve the WanderWise frontend."""
    return FileResponse(_STATIC_DIR / "index.html")
