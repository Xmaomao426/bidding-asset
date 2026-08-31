from __future__ import annotations

from dataclasses import asdict

from src.extractor import extract_document

from .models import (
    SEMANTIC_RESULT_SCHEMA_VERSION,
    Evidence,
    EvidenceLocator,
    NormalizedDocument,
    SemanticExtractionResult,
    SemanticFact,
    SemanticValue,
)


RULE_PROVIDER_NAME = "existing-rule-extractor/v1"


class RuleSemanticProvider:
    """Adapt the existing Extractor; do not add or duplicate extraction rules."""

    name = RULE_PROVIDER_NAME

    def extract(self, document: NormalizedDocument) -> SemanticExtractionResult:
        legacy_record = extract_document(document.to_extractor_document())
        record = asdict(legacy_record)
        facts = (
            self._fact(document, "project_name", record["project_name"]),
            self._fact(document, "project_number", "", unavailable_reason="not_provided_by_existing_extractor"),
            self._fact(document, "customer", record["customer"]),
            self._fact(document, "winner", record["winner"], role="supplier"),
            self._fact(document, "amount", record["budget"], role="budget"),
            self._fact(document, "amount", record["award_amount"], role="award"),
            self._fact(document, "project_content", record["content"]),
            self._fact(document, "date", record["bid_open_time"], role="bid_open_time"),
        )
        doc_type = str(record["doc_type"] or "")
        return SemanticExtractionResult(
            schema_version=SEMANTIC_RESULT_SCHEMA_VERSION,
            document_id=document.document_id,
            provider=self.name,
            document_type=SemanticValue(
                value=doc_type,
                status="known" if doc_type else "unknown",
                evidence=self._evidence(document, doc_type, "classification_evidence_unavailable") if doc_type else (),
            ),
            facts=facts,
            diagnostics={
                "legacy_record": record,
                "normalized_document_schema": document.schema_version,
                "warnings": list(document.warnings),
                "fact_confidence_policy": "not_provided_in_v1",
            },
        )

    def _fact(
        self,
        document: NormalizedDocument,
        field_name: str,
        value: str,
        *,
        role: str = "",
        unavailable_reason: str = "value_not_locatable_in_normalized_text",
    ) -> SemanticFact:
        value = str(value or "")
        return SemanticFact(
            field_name=field_name,
            value=value,
            role=role,
            status="known" if value else "unknown",
            evidence=self._evidence(document, value, unavailable_reason) if value else (),
        )

    @staticmethod
    def _evidence(document: NormalizedDocument, value: str, unavailable_reason: str) -> tuple[Evidence, ...]:
        span = find_text_span(document.text, value)
        if span is None:
            return (
                Evidence(
                    source_document_id=document.document_id,
                    availability="evidence_unavailable",
                    reason=unavailable_reason,
                ),
            )
        start, end = span
        return (
            Evidence(
                source_document_id=document.document_id,
                availability="available",
                text=document.text[start:end],
                locator=EvidenceLocator(
                    level=1,
                    kind="text_span",
                    document_id=document.document_id,
                    start=start,
                    end=end,
                ),
            ),
        )


def find_text_span(text: str, value: str) -> tuple[int, int] | None:
    """Return a reliable exact or whitespace-only-normalized span."""

    if not text or not value:
        return None
    start = text.casefold().find(value.casefold())
    if start >= 0:
        return start, start + len(value)

    compact_text: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(text):
        if char.isspace():
            continue
        compact_text.append(char.casefold())
        positions.append(index)
    compact_value = "".join(char.casefold() for char in value if not char.isspace())
    if not compact_value:
        return None
    compact_start = "".join(compact_text).find(compact_value)
    if compact_start < 0:
        return None
    compact_end = compact_start + len(compact_value) - 1
    return positions[compact_start], positions[compact_end] + 1
