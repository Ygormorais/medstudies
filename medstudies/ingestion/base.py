"""Base adapter interface — all sources implement this."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IngestResult:
    source: str = ""
    records_created: int = 0
    records_updated: int = 0
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


class BaseIngestionAdapter(ABC):
    """Implement one adapter per external source."""

    @property
    @abstractmethod
    def source_name(self) -> str: ...

    @abstractmethod
    def ingest(self, **kwargs) -> IngestResult: ...
