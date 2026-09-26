"""Classify Valhalla responses that mean a requested route is unavailable."""

import httpx


def route_unavailable(response: httpx.Response) -> bool:
    if response.status_code != 400:
        return False
    try:
        return response.json().get("error_code") in {170, 171, 441, 442}
    except (TypeError, ValueError):
        return False
