"""
WanderWise Engine — FastAPI app.
POST /api/plan accepts a travel request and returns a structured itinerary.
GET /         serves the static HTML frontend.
GET /healthz  Cloud Run health check.
"""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv

# Load .env for local dev BEFORE importing modules that read env.
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from itinerary import ItineraryRequest, ItineraryResponse, generate_itinerary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("wanderwise")

app = FastAPI(
    title="WanderWise Engine",
    version="0.1.0",
    description="Smart, dynamic travel planning. PromptWars Gurgaon 2026.",
)


@app.get("/healthz")
def healthz() -> dict:
    """Cloud Run health probe."""
    return {"status": "ok", "service": "wanderwise-engine", "version": "0.1.0"}


@app.post("/api/plan", response_model=ItineraryResponse)
def plan(req: ItineraryRequest) -> ItineraryResponse:
    """Generate an itinerary from preferences and constraints."""
    num_days = (req.end_date - req.start_date).days + 1
    logger.info(
        "plan_request destination=%s days=%d budget=%d",
        req.destination, num_days, req.budget_inr,
    )
    try:
        return generate_itinerary(req)
    except Exception as exc:
        logger.exception("Gemini call failed")
        raise HTTPException(
            status_code=503,
            detail="Itinerary generation temporarily unavailable. Please retry.",
        ) from exc


# Serve static frontend.
_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/")
def root() -> FileResponse:
    """Serve the WanderWise frontend."""
    return FileResponse(_STATIC_DIR / "index.html")
