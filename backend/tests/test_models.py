import pytest
from pydantic import ValidationError

from browserbuddy.models import EventEnvelope, HistoryVisitsEvent, TabStateEvent


def test_tab_state_envelope_roundtrip():
    env = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 7,
            "event": {
                "type": "tab_state",
                "tabs": [
                    {
                        "tab_id": 3,
                        "window_id": 1,
                        "url": "https://example.com",
                        "title": "Example",
                        "container": "Research",
                        "active_ms": 12000,
                    }
                ],
            },
        }
    )
    assert isinstance(env.event, TabStateEvent)
    assert env.event.tabs[0].container == "Research"
    assert env.event.tabs[0].group_id is None  # optional enhancement fields default


def test_history_visits_envelope():
    env = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 8,
            "event": {
                "type": "history_visits",
                "visits": [{"url": "https://example.com", "visit_time": 1720000000000}],
            },
        }
    )
    assert isinstance(env.event, HistoryVisitsEvent)
    assert env.event.visits[0].title == ""


def test_unknown_event_type_rejected():
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate({"v": 1, "seq": 1, "event": {"type": "bogus"}})


def test_seq_must_be_positive():
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            {"v": 1, "seq": 0, "event": {"type": "tab_state", "tabs": []}}
        )


def test_envelope_tolerates_extra_fields():
    """Forward-compat: older backend must accept newer extension envelopes."""
    env = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 1,
            "event": {"type": "tab_state", "tabs": []},
            "future_field": "ignored",
        }
    )
    assert isinstance(env.event, TabStateEvent)
