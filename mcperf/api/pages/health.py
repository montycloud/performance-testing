"""Health Events page — mostly sequential, mirroring the HAR waterfall.

Exactly two parallel pairs: summary + first breakdown-summary, and the two
event-detail calls at the end, which need an event Id from the open-events list.
"""

from __future__ import annotations

import json
import random

from mcperf.locust.base_user import MontyCloudUser

P = "[Health]"
API = "/health/api/v1"


def _breakdown(user: MontyCloudUser, status: str):
    return user.call(
        f"{API}/events/breakdown-summary", f"{P} {API}/events/breakdown-summary ({status})",
        {"Filters": json.dumps({"Dimension": "EventTypeCode", "StatusCode": status}), "Limit": 25},
    )


def health_flow(user: MontyCloudUser) -> None:
    user.api_get(f"{API}/policies", f"{P} {API}/policies")

    user.run_batch([
        user.call(f"{API}/events/summary", f"{P} {API}/events/summary"),
        _breakdown(user, "open"),
    ])
    _breakdown(user, "closed")()
    _breakdown(user, "upcoming")()

    user.api_get(
        f"{API}/events/events",
        f"{P} {API}/events/events (EventTypeCode=AWS_ABUSE_PHISHING_CONTENT_REPORTED, open)",
        {"Filters": json.dumps({"EventTypeCode": "AWS_ABUSE_PHISHING_CONTENT_REPORTED", "StatusCode": "open"}),
         "Limit": 25},
    )
    # Source of the event Id for the detail calls.
    events = user.api_get(
        f"{API}/events/events", f"{P} {API}/events/events (StatusCode=open)",
        {"Filters": json.dumps({"StatusCode": "open"}), "Limit": 25},
    )
    health_events = events.get("HealthEvents", []) if isinstance(events, dict) else []
    if not health_events:
        user.log.warning("[%s] No HealthEvents returned; skipping event-timelines/affected-resources.", user.label)
        return
    event_id: str = random.choice(health_events).get("Id", "")
    if not event_id:
        user.log.warning(
            "[%s] Selected HealthEvent has no Id; skipping event-timelines/affected-resources.", user.label,
        )
        return
    user.log.debug("[%s] Health event Id = %s", user.label, event_id)

    user.run_batch([
        user.call(f"{API}/events/{event_id}/{detail}", f"{P} {API}/events/{{id}}/{detail}",
                  {"Offset": 0, "Limit": 10})
        for detail in ("event-timelines", "affected-resources")
    ])
