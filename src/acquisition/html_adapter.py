from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class LinkReference:
    text: str
    url: str
    raw_href: str = ""
    download: str = ""
    executable: bool = False


@dataclass(frozen=True)
class NoticeContentRegion:
    structured_dom: dict[str, Any]
    title: str
    text: str
    links: tuple[LinkReference, ...]
    locator: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(self.structured_dom, ensure_ascii=False, separators=(",", ":"))
