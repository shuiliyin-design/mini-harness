"""Deterministic offline EVENT observation and candidate adapters."""

from collections import deque

from ..domain import (
    EventObservationQuery, EventSourceObservation, EventSourceResult,
    event_observation_identity, event_source_content_fingerprint,
)


class FakeOpenAIEventSource:
    """Return queued typed source observations without network access."""

    def __init__(self, fixtures=(), *, clock):
        self.fixtures = deque(fixtures)
        self.clock = clock
        self.calls = []

    def enqueue(self, fixture):
        self.fixtures.append(fixture)

    def observe(self, query):
        if not isinstance(query, EventObservationQuery):
            raise ValueError("invalid EVENT query")
        self.calls.append(query)
        fixture = self.fixtures.popleft() if self.fixtures else {}
        if isinstance(fixture, Exception):
            raise fixture
        retrieved_at = fixture.get("retrieved_at", self.clock())
        results = []
        for raw in fixture.get("results", []):
            fields = {
                "source_ref": raw["source_ref"],
                "canonical_url": raw["canonical_url"],
                "publisher": raw["publisher"],
                "source_kind": raw["source_kind"],
                "title": raw["title"],
                "snippet": raw["snippet"],
                "published_at": raw.get("published_at"),
            }
            results.append(EventSourceResult(
                **fields, content_fingerprint=event_source_content_fingerprint(
                    **{name: fields[name] for name in (
                        "canonical_url", "publisher", "source_kind", "title",
                        "snippet", "published_at",
                    )},
                ),
            ))
        results = tuple(results)
        complete = fixture.get("coverage_complete", True)
        truncated = fixture.get("truncated", False)
        observation_id = event_observation_identity(
            query.entity_key, query.window_start_at, query.window_end_at,
            retrieved_at, complete, truncated, results, "fake_event_search",
        )
        return EventSourceObservation(
            observation_id, query.entity_key, query.window_start_at,
            query.window_end_at, retrieved_at, complete, truncated, results,
        )


class FakeEventCandidateAgent:
    """Propose, but never verify, at most one event candidate."""

    def __init__(self, overrides=()):
        self.overrides = deque(overrides)
        self.calls = []

    def enqueue(self, envelope):
        self.overrides.append(envelope)

    def propose(self, observation):
        self.calls.append(observation.observation_id)
        if self.overrides:
            return self.overrides.popleft()
        for source in observation.results:
            text = f"{source.title} {source.snippet}"
            marker = "OpenAI released "
            title = source.title
            if marker not in title:
                marker = "OpenAI is releasing "
            if marker not in title:
                continue
            tail = title.split(marker, 1)[1].strip()
            name = tail.split(".", 1)[0].split(" is now available", 1)[0]
            name = name.strip(" ：:,，")
            if not name:
                continue
            return {
                "schema_version": 1,
                "candidates": [{
                    "entity_key": "openai",
                    "event_type": "MODEL_RELEASED",
                    "object_type": "MODEL",
                    "display_name": name,
                    "canonical_name_candidate": name,
                    "occurred_at_candidate": source.published_at,
                    "support": [{
                        "source_ref": source.source_ref,
                        "exact_span": text,
                    }],
                }],
            }
        return {"schema_version": 1, "candidates": []}
