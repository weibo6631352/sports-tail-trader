from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class DiscoveryQuery:
    """Extension-provided remote discovery filter fragment."""

    name: str = "default"
    params: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def title_search(cls, title_search: str) -> "DiscoveryQuery":
        text = title_search.strip()
        return cls(name=f"title_search:{text}", params={"title_search": text})
