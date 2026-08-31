from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DocumentSource:
    source_id: str
    source_type: str
    source_url: str
    capture_time: str
    title: str
    html_content: str
    text_content: str
    attachments: list[dict[str, str]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
