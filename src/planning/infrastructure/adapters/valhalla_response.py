"""Classify Valhalla responses that mean a requested route is unavailable."""

import httpx

# A route that waits overnight can be valid GTFS but slower than walking.
# Compare those unusually long transit trips with a pedestrian route.
LONG_TRANSIT_ROUTE_SECONDS = 2 * 60 * 60


def route_unavailable(response: httpx.Response) -> bool:
    if response.status_code != 400:
        return False
    try:
        return response.json().get("error_code") in {170, 441, 442}
    except (TypeError, ValueError):
        return False


def multimodal_costing_options(use_rail: bool) -> dict:
    """Avoid the local GTFS rail route that crashes Valhalla when configured."""
    return {} if use_rail else {"transit": {"use_rail": 0}}
