"""
WanderWise Maps integration — Google Maps Platform (Places API + Geocoding).

Single responsibility: take an itinerary with activity locations and enrich
each location with real-world metadata: place_id, geocoded coordinates,
opening hours summary, and accessibility flag (wheelchair-accessible entrance).

Uses the official googlemaps Python client. Calls are sync but FastAPI runs
sync route handlers in a threadpool so concurrency is fine for hackathon scale.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

import googlemaps

logger = logging.getLogger("wanderwise.maps")


# Lazy initialisation — Maps client is optional. If MAPS_API_KEY is absent,
# the rest of the app still works; activities just don't get enriched.
_MAPS_API_KEY = os.environ.get("MAPS_API_KEY")
_CLIENT: Optional[googlemaps.Client] = None

if _MAPS_API_KEY:
    try:
        _CLIENT = googlemaps.Client(key=_MAPS_API_KEY)
        logger.info("Google Maps client initialised")
    except Exception as exc:
        logger.warning("Maps client init failed: %s — proceeding without enrichment", exc)
        _CLIENT = None
else:
    logger.warning("MAPS_API_KEY not set — Maps enrichment disabled")


def is_enabled() -> bool:
    """True iff the Maps client is configured and usable."""
    return _CLIENT is not None


def find_place(query: str, near: Optional[str] = None) -> Optional[dict]:
    """
    Look up a place by free-text query. Returns the first match with
    geocoded coords, place_id, and (when available) accessibility flag.

    Returns None if Maps is disabled, the query yields no matches, or
    the API errors. Never raises — callers treat this as best-effort.
    """
    if _CLIENT is None:
        return None

    try:
        # Compose a contextual query: "Anjuna Beach, Goa" beats "Anjuna Beach".
        composed = f"{query}, {near}" if near else query

        # find_place returns candidates; ask only for fields the legacy API supports.
        # (wheelchair_accessible_entrance is a Places API New-only field.)
        response = _CLIENT.find_place(
            input=composed,
            input_type="textquery",
            fields=[
                "place_id",
                "name",
                "formatted_address",
                "geometry/location",
                "opening_hours",
            ],
        )

        candidates = response.get("candidates", []) if response else []
        if not candidates:
            return None

        top = candidates[0]
        loc = top.get("geometry", {}).get("location", {})
        return {
            "place_id": top.get("place_id"),
            "formatted_address": top.get("formatted_address"),
            "latitude": loc.get("lat"),
            "longitude": loc.get("lng"),
            "is_accessible": None,  # not exposed by legacy Places API
            "open_now": (top.get("opening_hours") or {}).get("open_now"),
        }
    except googlemaps.exceptions.ApiError as exc:
        logger.warning("Maps API error for '%s': %s", composed, exc)
        return None
    except Exception as exc:
        logger.warning("Unexpected Maps error for '%s': %s", composed, exc)
        return None


def geocode_destination(destination: str) -> Optional[Tuple[float, float]]:
    """
    Best-effort geocode of the trip destination. Returns (lat, lng) or None.
    Used to anchor downstream Place searches.
    """
    if _CLIENT is None:
        return None
    try:
        results = _CLIENT.geocode(destination)
        if not results:
            return None
        loc = results[0].get("geometry", {}).get("location", {})
        if "lat" in loc and "lng" in loc:
            return (loc["lat"], loc["lng"])
        return None
    except Exception as exc:
        logger.warning("Geocode failed for '%s': %s", destination, exc)
        return None
