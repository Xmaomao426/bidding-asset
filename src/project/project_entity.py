from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.document.document_entity import DocumentEntity


@dataclass
class ProjectEntity:
    project_id: str
    project_name: str
    records: list[dict[str, Any]] = field(default_factory=list)
    documents: list[DocumentEntity] = field(default_factory=list)
    organizations: list[str] = field(default_factory=list)
    timeline: list[dict[str, str]] = field(default_factory=list)
