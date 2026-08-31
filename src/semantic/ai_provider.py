from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from .models import (
    NormalizedDocument,
)
BUSINESS_PROMPT_VERSION = "semantic-business/v1"
BUSINESS_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "semantic_business_v1.txt"
DOCUMENT_BUSINESS_V2_PROMPT_VERSION = "document-business/v2"
DOCUMENT_BUSINESS_V2_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "document_business_v2.txt"
DOCUMENT_BUSINESS_PROMPT_VERSION = "document-business/v3"
DOCUMENT_BUSINESS_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "document_business_v3.txt"
DOCUMENT_BUSINESS_V4_PROMPT_VERSION = "document-business/v4"
DOCUMENT_BUSINESS_V4_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "document_business_v4.txt"
DOCUMENT_BUSINESS_V5_PROMPT_VERSION = "document-business/v5"
DOCUMENT_BUSINESS_V5_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "document_business_v5.txt"
DOCUMENT_CRITICAL_REPAIR_PROMPT_VERSION = "document-business-critical-repair/v1"
DOCUMENT_CRITICAL_REPAIR_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "document_business_critical_repair_v1.txt"
DOCUMENT_CRITICAL_REPAIR_V2_PROMPT_VERSION = "document-business-critical-repair/v2"
DOCUMENT_CRITICAL_REPAIR_V2_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "document_critical_repair_v2.txt"
PROMPT_REGISTRY = MappingProxyType(
    {
        "semantic-business/v1": (
            BUSINESS_PROMPT_PATH,
            "01D6042DFC22F89EE4322B94C9A467701A15139DEE446DB601F6C3CAB819EF4C",
        ),
        "document-business/v2": (
            DOCUMENT_BUSINESS_V2_PROMPT_PATH,
            "FEB15F8253A162C9B4ED10BAAFCC29D482A60EBE9EF7C47F7A125B981632EB0F",
        ),
        "document-business/v3": (
            DOCUMENT_BUSINESS_PROMPT_PATH,
            "1274E7E56FCB6640D61062BFBCFC8119688C40FD3FA9F632C864B61CF9E472E3",
        ),
        "document-business/v4": (
            DOCUMENT_BUSINESS_V4_PROMPT_PATH,
            "6C53F64F2E7235713260AE08C2F6797C56FCDAC42FFED81689E68163EFD14F32",
        ),
        "document-business/v5": (
            DOCUMENT_BUSINESS_V5_PROMPT_PATH,
            "B6C98F1EA7B490516AF366E3A87215B61B48AA6890C71429731A9EDFB1EA5A34",
        ),
        "document-business-critical-repair/v1": (
            DOCUMENT_CRITICAL_REPAIR_PROMPT_PATH,
            "8C262DEBCEC8CD1D1A01E0782032C1FCCCA966F42343110DBAE275C2AB5A5ACC",
        ),
        "document-business-critical-repair/v2": (
            DOCUMENT_CRITICAL_REPAIR_V2_PROMPT_PATH,
            "02424A8E9DC33E85184AD930ED2FD47E6D684E7B82D7B9E8E006B2820E7A9B22",
        ),
    }
)


BUSINESS_FIELDS = (
    "project_name", "project_number", "customer", "content", "budget", "bid_open_time",
)
BUSINESS_AWARD_FIELDS = ("winner", "award_amount")
FIELD_EVIDENCE_FIELDS = ("project_name", "customer", "bid_open_time", "content")
MAX_FIELD_EVIDENCE_QUOTE_CHARS = 512
MAX_FIELD_EVIDENCE_SECTIONS = 4
@dataclass(frozen=True)
class ModelInvocation:
    raw_text: str
    model: str
    elapsed_seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
    cost_currency: str = ""
    request_id: str = ""
    retry_count: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SemanticModelTransport(Protocol):
    def invoke(self, *, model: str, prompt: str, parameters: Mapping[str, Any]) -> ModelInvocation:
        """Call one authorized model and return its auditable raw response."""


class AISemanticProvider:
    """Transport-agnostic AI adapter with no production side effects."""

    def __init__(
        self,
        *,
        transport: SemanticModelTransport,
        model: str,
        prompt_path: Path = BUSINESS_PROMPT_PATH,
        prompt_version: str = BUSINESS_PROMPT_VERSION,
        parameters: Mapping[str, Any] | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("AI Semantic Provider requires an explicit model name")
        self._verified_prompt_template = validate_prompt_identity(
            Path(prompt_path),
            prompt_version,
        )
        self.transport = transport
        self.model = model
        self.prompt_path = Path(prompt_path)
        self.prompt_version = prompt_version
        self.parameters = dict(parameters or {})

    def extract_business(
        self,
        document: NormalizedDocument,
        *,
        prompt_context: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Extract the active business contract from the selected structured DOM only."""
        structured_dom = document.metadata.get("notice_content_dom")
        if not isinstance(structured_dom, Mapping) or not structured_dom:
            raise ValueError("notice_content_dom is required for business semantic extraction")
        replacements: dict[str, Any] = {"NOTICE_CONTENT_DOM_JSON": structured_dom}
        replacements.update(dict(prompt_context or {}))
        prompt = self._verified_prompt_template
        for name, value in replacements.items():
            encoded = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False, separators=(",", ":")
            )
            prompt = prompt.replace("{{" + str(name) + "}}", encoded)
        invocation = self.transport.invoke(model=self.model, prompt=prompt, parameters=self.parameters)
        payload = validate_business_payload(parse_model_json(invocation.raw_text))
        diagnostics = {
            "model": invocation.model or self.model,
            "prompt_version": self.prompt_version,
            "parameters": self.parameters,
            "elapsed_seconds": invocation.elapsed_seconds,
            "input_tokens": invocation.input_tokens,
            "output_tokens": invocation.output_tokens,
            "cost": invocation.cost,
            "cost_currency": invocation.cost_currency,
            "request_id": invocation.request_id,
            "retry_count": invocation.retry_count,
            "response_sha256": hashlib.sha256(invocation.raw_text.encode("utf-8")).hexdigest(),
            "transport_metadata": dict(invocation.metadata),
        }
        return payload, diagnostics

def validate_business_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate only response JSON/schema/interface; do not reinterpret model semantics."""
    raw_fields = payload.get("fields")
    raw_corrections = payload.get("corrections")
    raw_field_evidence = payload.get("field_evidence")
    raw_packages = payload.get("packages")
    if raw_fields is None:
        raw_fields = {}
    if raw_corrections is None:
        raw_corrections = {}
    if raw_field_evidence is None:
        raw_field_evidence = {}
    if raw_packages is None:
        raw_packages = []
    if not isinstance(raw_fields, Mapping):
        raise ValueError("business semantic response fields must be an object")
    if not isinstance(raw_corrections, Mapping):
        raise ValueError("business semantic response corrections must be an object")
    if not isinstance(raw_field_evidence, Mapping):
        raise ValueError("business semantic response field_evidence must be an object")
    if not isinstance(raw_packages, list):
        raise ValueError("business semantic response packages must be an array")

    allowed_fields = frozenset((*BUSINESS_FIELDS, *BUSINESS_AWARD_FIELDS))
    fields = {
        field_name: str(raw_fields.get(field_name) or "").strip()
        for field_name in allowed_fields
    }
    corrections = {
        field_name: str(raw_corrections.get(field_name) or "").strip()
        for field_name in allowed_fields
    }
    field_evidence: dict[str, dict[str, Any]] = {}
    for field_name in FIELD_EVIDENCE_FIELDS:
        raw_reference = raw_field_evidence.get(field_name)
        if raw_reference is None:
            continue
        if not isinstance(raw_reference, Mapping):
            field_evidence[field_name] = {"section_index": None, "quote": ""}
            continue
        raw_quote = str(raw_reference.get("quote") or "").strip()
        normalized_reference: dict[str, Any] = {
            "quote": raw_quote[:MAX_FIELD_EVIDENCE_QUOTE_CHARS],
        }
        raw_indices = raw_reference.get("section_indices")
        if isinstance(raw_indices, list):
            safe_indices: list[int] = []
            for raw_index in raw_indices[:MAX_FIELD_EVIDENCE_SECTIONS]:
                if isinstance(raw_index, bool):
                    continue
                try:
                    index = int(raw_index)
                except (TypeError, ValueError, OverflowError):
                    continue
                if index >= 0 and index not in safe_indices:
                    safe_indices.append(index)
            if safe_indices:
                normalized_reference["section_indices"] = safe_indices
        if "section_indices" not in normalized_reference:
            raw_index = raw_reference.get("section_index")
            if isinstance(raw_index, bool):
                raw_index = None
            elif not isinstance(raw_index, int):
                try:
                    raw_index = int(raw_index) if raw_index is not None else None
                except (TypeError, ValueError, OverflowError):
                    raw_index = None
            normalized_reference["section_index"] = (
                raw_index if isinstance(raw_index, int) and raw_index >= 0 else None
            )
        field_evidence[field_name] = normalized_reference
    packages: list[dict[str, Any]] = []
    for raw_package in raw_packages:
        if not isinstance(raw_package, Mapping):
            raise ValueError("business semantic package entries must be objects")
        raw_awards = raw_package.get("awards") or []
        if not isinstance(raw_awards, list):
            raise ValueError("business semantic package awards must be an array")
        awards: list[dict[str, str]] = []
        for raw_award in raw_awards:
            if not isinstance(raw_award, Mapping):
                raise ValueError("business semantic award entries must be objects")
            awards.append({
                "winner": str(raw_award.get("winner") or "").strip(),
                "award_amount": str(raw_award.get("award_amount") or "").strip(),
            })
        packages.append({
            "package_identifier": str(raw_package.get("package_identifier") or "").strip(),
            "package_name": str(raw_package.get("package_name") or "").strip(),
            "awards": awards,
        })
    return {
        "fields": fields,
        "corrections": corrections,
        "field_evidence": field_evidence,
        "packages": packages,
    }


def parse_model_json(raw_text: str) -> dict[str, Any]:
    text = str(raw_text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("AI semantic response must be a JSON object")
    return payload


def validate_prompt_identity(
    prompt_path: Path,
    prompt_version: str,
) -> str:
    registration = PROMPT_REGISTRY.get(prompt_version)
    if registration is None:
        raise ValueError(f"unregistered AI semantic prompt version: {prompt_version}")
    registered_path, registered_sha256 = registration
    actual_path = prompt_path.resolve()
    if actual_path != registered_path.resolve():
        raise ValueError(
            f"AI semantic prompt path/version mismatch: {prompt_version}"
        )
    payload = actual_path.read_bytes()
    template = payload.decode("utf-8")
    first_line = template.splitlines()[0] if template else ""
    if first_line != f"Prompt version: {prompt_version}":
        raise ValueError(
            f"AI semantic prompt header/version mismatch: {prompt_version}"
        )
    actual_sha256 = hashlib.sha256(payload).hexdigest().upper()
    if actual_sha256 != registered_sha256:
        raise ValueError(
            f"AI semantic prompt hash mismatch: {prompt_version}"
        )
    return template
