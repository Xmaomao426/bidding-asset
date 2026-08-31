from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .document_entity import DocumentEntity, document_entity_from_mapping


DEFAULT_DOCUMENT_REPOSITORY_PATH = Path("data/diagnostics/document_repository.json")


class DocumentRepository:
    def __init__(self, path: Path | str = DEFAULT_DOCUMENT_REPOSITORY_PATH) -> None:
        self.path = Path(path)
        self._documents: dict[str, DocumentEntity] = {}
        self._load()

    def create_document(self, document: DocumentEntity | dict[str, Any] | None = None, **kwargs: Any) -> DocumentEntity:
        if isinstance(document, DocumentEntity):
            entity = document
        elif isinstance(document, dict):
            payload = dict(document)
            payload.update(kwargs)
            entity = document_entity_from_mapping(payload)
        else:
            entity = document_entity_from_mapping(kwargs)
        self._documents[entity.document_id] = entity
        self._save()
        return entity

    def get_document(self, document_id: str) -> DocumentEntity | None:
        return self._documents.get(document_id)

    def list_documents(self) -> list[DocumentEntity]:
        return sorted(self._documents.values(), key=lambda document: document.created_time)

    def attach_record(self, document_id: str, record: dict[str, Any]) -> DocumentEntity:
        document = self._documents[document_id]
        record_key = _record_key(record)
        if record_key and any(_record_key(item) == record_key for item in document.records):
            return document
        document.records.append(dict(record))
        self._save()
        return document

    def _load(self) -> None:
        if not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            rows = payload.get("documents") or []
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []
        self._documents = {
            document.document_id: document
            for document in (document_entity_from_mapping(row) for row in rows if isinstance(row, dict))
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"documents": [asdict(document) for document in self.list_documents()]}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_key(record: dict[str, Any]) -> str:
    return str(record.get("source_document_id") or record.get("source_file") or record.get("record_id") or "")


_default_repository = DocumentRepository()


def create_document(document: DocumentEntity | dict[str, Any] | None = None, **kwargs: Any) -> DocumentEntity:
    return _default_repository.create_document(document, **kwargs)


def get_document(document_id: str) -> DocumentEntity | None:
    return _default_repository.get_document(document_id)


def list_documents() -> list[DocumentEntity]:
    return _default_repository.list_documents()


def attach_record(document_id: str, record: dict[str, Any]) -> DocumentEntity:
    return _default_repository.attach_record(document_id, record)
