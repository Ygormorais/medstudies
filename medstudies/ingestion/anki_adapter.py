"""
AnkiConnect adapter.

Connects to a running Anki desktop instance via its local HTTP API.
We fetch deck-level aggregates and store them as AnkiSnapshot records.
We never store individual card content — Anki owns that data.

AnkiConnect must be installed in Anki and Anki must be open.
Default endpoint: http://localhost:8765
"""
from __future__ import annotations
import json
import urllib.request
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from medstudies.ingestion.base import BaseIngestionAdapter, IngestResult
from medstudies.persistence.models import AnkiSnapshot, Topic


ANKICONNECT_URL = "http://localhost:8765"


def _anki_request(action: str, **params) -> Any:
    payload = json.dumps({"action": action, "version": 6, "params": params}).encode()
    req = urllib.request.Request(ANKICONNECT_URL, data=payload)
    with urllib.request.urlopen(req, timeout=5) as resp:
        result = json.loads(resp.read())
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect error: {result['error']}")
    return result["result"]


class AnkiAdapter(BaseIngestionAdapter):
    """
    Pulls card stats from AnkiConnect for all topics that have an anki_deck set.
    """

    def __init__(self, session: Session):
        self._session = session

    @property
    def source_name(self) -> str:
        return "anki"

    def ingest(self, **kwargs) -> IngestResult:
        result = IngestResult(source=self.source_name)

        topics: list[Topic] = (
            self._session.query(Topic).filter(Topic.anki_deck.isnot(None)).all()
        )

        if not topics:
            result.errors.append("No topics have anki_deck configured.")
            return result

        for topic in topics:
            try:
                snapshot = self._sync_deck(topic)
                self._session.add(snapshot)
                result.records_created += 1
            except Exception as exc:
                result.errors.append(f"Topic '{topic.name}' — {exc}")

        try:
            self._session.commit()
        except Exception as exc:
            self._session.rollback()
            result.errors.append(f"DB commit failed: {exc}")

        return result

    def _sync_deck(self, topic: Topic) -> AnkiSnapshot:
        deck = topic.anki_deck
        card_ids: list[int] = _anki_request("findCards", query=f'deck:"{deck}"')

        if not card_ids:
            return AnkiSnapshot(
                topic_id=topic.id,
                deck_name=deck,
                total_cards=0,
                synced_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )

        cards_info: list[dict] = _anki_request("cardsInfo", cards=card_ids)

        total = len(cards_info)
        due = sum(1 for c in cards_info if c.get("queue", -1) in (1, 2, 3))
        ease_values = [c["factor"] for c in cards_info if c.get("factor", 0) > 0]
        avg_ease = sum(ease_values) / len(ease_values) if ease_values else None
        intervals = [c["interval"] for c in cards_info if c.get("interval", 0) > 0]
        avg_interval = sum(intervals) / len(intervals) if intervals else None
        total_lapses = sum(c.get("lapses", 0) for c in cards_info)

        return AnkiSnapshot(
            topic_id=topic.id,
            deck_name=deck,
            synced_at=datetime.now(timezone.utc).replace(tzinfo=None),
            total_cards=total,
            due_cards=due,
            avg_ease=avg_ease,
            avg_interval=avg_interval,
            total_lapses=total_lapses,
        )

    def list_decks(self) -> list[str]:
        """Utility: list all deck names in Anki (for setup)."""
        return _anki_request("deckNames")
