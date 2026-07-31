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
        EventEnvelope.model_validate({"v": 1, "seq": 0, "event": {"type": "tab_state", "tabs": []}})


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


def test_session_and_page_events_validate():
    env = EventEnvelope.model_validate(
        {"v": 1, "seq": 1, "event": {"type": "session_start", "name": "s", "started_at": 5}}
    )
    assert env.event.type == "session_start"
    env = EventEnvelope.model_validate(
        {"v": 1, "seq": 2, "event": {"type": "session_end", "ended_at": 9}}
    )
    assert env.event.type == "session_end"
    env = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 3,
            "event": {
                "type": "page_capture",
                "url": "https://a.com",
                "visited_at": 7,
                "text": "body text",
                "html": "<html></html>",
            },
        }
    )
    assert env.event.type == "page_capture"
    assert env.event.title == ""
    assert env.event.container is None


def test_session_start_requires_nonempty_name():
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            {"v": 1, "seq": 1, "event": {"type": "session_start", "name": "", "started_at": 5}}
        )


def test_page_capture_accepts_links_and_defaults_empty():
    env = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 1,
            "event": {
                "type": "page_capture",
                "url": "https://a.com",
                "visited_at": 5,
                "links": [{"href": "https://x.com", "text": "X", "rel": "nofollow"}],
            },
        }
    )
    assert env.event.links[0].href == "https://x.com"
    env2 = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 2,
            "event": {"type": "page_capture", "url": "https://a.com", "visited_at": 5},
        }
    )
    assert env2.event.links == []


def test_nav_edge_and_link_click_validate():
    env = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 1,
            "event": {
                "type": "nav_edge",
                "to_url": "https://a.com",
                "occurred_at": 7,
                "from_url": "https://google.com/search?q=x",
                "transition": "link",
                "qualifiers": "client_redirect",
                "query": "x",
            },
        }
    )
    assert env.event.type == "nav_edge"
    assert env.event.from_url == "https://google.com/search?q=x"
    env2 = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 2,
            "event": {"type": "nav_edge", "to_url": "https://a.com", "occurred_at": 7},
        }
    )
    assert env2.event.from_url is None and env2.event.query is None
    env3 = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 3,
            "event": {
                "type": "link_click",
                "page_url": "https://a.com",
                "href": "https://x.com",
                "clicked_at": 9,
            },
        }
    )
    assert env3.event.type == "link_click" and env3.event.text == ""
