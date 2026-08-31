"""Shared project-number safety rules used before matching or persistence."""

from __future__ import annotations

import re


_YEAR = r"(?:19|20)\d{2}"
_YEAR_OR_SHORT = rf"(?:{_YEAR}|\d{{2}})"
_YEAR_ONLY = re.compile(
    rf"^\s*(?:{_YEAR}\s*年?|{_YEAR}\s*年?\s*(?:-|—|–|至|/)\s*{_YEAR_OR_SHORT}\s*年?)\s*$"
)


def is_year_only_project_number(value: object) -> bool:
    """Return True only when the complete value is a year or year range."""
    return bool(_YEAR_ONLY.fullmatch(str(value or "").strip()))


def is_valid_project_number(value: object) -> bool:
    normalized = str(value or "").strip()
    return bool(normalized) and not is_year_only_project_number(normalized)


def validated_project_number(value: object) -> str:
    normalized = str(value or "").strip()
    return normalized if is_valid_project_number(normalized) else ""
