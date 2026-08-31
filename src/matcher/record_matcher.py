from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any

from src.project_number import is_valid_project_number


DEFAULT_MATCH_OUTPUT = Path("data/diagnostics/match_results.json")
DEFAULT_THRESHOLD = 0.68
DEFAULT_TIME_WINDOW_DAYS = 90


@dataclass
class MatchResult:
    source_record: dict[str, Any]
    target_record: dict[str, Any]
    match_score: float
    match_reason: list[str]


def compact_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_document_id": record.get("source_document_id", ""),
        "source_file": record.get("source_file", ""),
        "customer": record.get("customer", ""),
        "project_name": record.get("project_name", ""),
        "bid_open_time": record.get("bid_open_time", ""),
    }


def normalize_text(value: str | None) -> str:
    value = value or ""
    value = value.lower()
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "", value)
    for token in ("公开招标公告", "竞争性磋商公告", "采购公告", "招标公告", "中标结果公告", "成交结果公告", "中标公告", "成交公告", "采购文件", "招标文件"):
        value = value.replace(token.lower(), "")
    return value


def project_numbers(record: dict[str, Any]) -> set[str]:
    haystack = " ".join(
        str(record.get(key, "") or "")
        for key in ("project_name", "source_file", "content", "note")
    )
    boundary = r"(?<![A-Za-z0-9-]){}(?![A-Za-z0-9-])"
    matches = re.findall(boundary.format(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+"), haystack)
    matches.extend(re.findall(boundary.format(r"[A-Za-z]{1,}[A-Za-z0-9]*(?:-[A-Za-z0-9]+){1,}"), haystack))
    matches.extend(re.findall(boundary.format(r"[A-Za-z]{2,}\d{2,}[A-Za-z0-9-]*"), haystack))
    return {
        match.upper().strip("-")
        for match in matches
        if any(ch.isdigit() for ch in match) and len(match) >= 5 and is_valid_project_number(match)
    }


def project_name_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_name = normalize_text(str(left.get("project_name", "") or ""))
    right_name = normalize_text(str(right.get("project_name", "") or ""))
    if not left_name or not right_name:
        return 0.0
    return SequenceMatcher(None, left_name, right_name).ratio()


def customer_score(left: dict[str, Any], right: dict[str, Any]) -> tuple[float, str]:
    left_customer = normalize_text(str(left.get("customer", "") or ""))
    right_customer = normalize_text(str(right.get("customer", "") or ""))
    if not left_customer or not right_customer:
        return 0.0, ""
    if left_customer == right_customer:
        return 0.20, "customer_exact"
    if left_customer in right_customer or right_customer in left_customer:
        return 0.12, "customer_contains"
    return 0.0, ""


def parse_date(value: str | None) -> date | None:
    value = value or ""
    match = re.search(r"(\d{4})[-/.年]\s*(\d{1,2})[-/.月]\s*(\d{1,2})", value)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def time_score(left: dict[str, Any], right: dict[str, Any], window_days: int) -> tuple[float, str]:
    left_date = parse_date(str(left.get("bid_open_time", "") or ""))
    right_date = parse_date(str(right.get("bid_open_time", "") or ""))
    if not left_date or not right_date:
        return 0.0, ""
    delta = abs((left_date - right_date).days)
    if delta <= window_days:
        return 0.05, f"time_window_{delta}d"
    return 0.0, ""


def score_pair(left: dict[str, Any], right: dict[str, Any], window_days: int = DEFAULT_TIME_WINDOW_DAYS) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    left_numbers = project_numbers(left)
    right_numbers = project_numbers(right)
    exact_numbers = left_numbers & right_numbers
    if exact_numbers:
        score += 0.45
        reasons.append(f"project_number_exact:{','.join(sorted(exact_numbers))}")

    similarity = project_name_similarity(left, right)
    if similarity >= 0.55:
        score += similarity * 0.55
        reasons.append(f"project_name_similarity:{similarity:.2f}")

    customer_points, customer_reason = customer_score(left, right)
    if customer_points:
        score += customer_points
        reasons.append(customer_reason)

    time_points, time_reason = time_score(left, right, window_days)
    if time_points:
        score += time_points
        reasons.append(time_reason)

    return min(score, 1.0), reasons


def match_records(
    records: list[dict[str, Any]],
    threshold: float = DEFAULT_THRESHOLD,
    window_days: int = DEFAULT_TIME_WINDOW_DAYS,
) -> list[MatchResult]:
    results: list[MatchResult] = []
    for left, right in combinations(records, 2):
        score, reasons = score_pair(left, right, window_days=window_days)
        if score >= threshold:
            results.append(
                MatchResult(
                    source_record=compact_record(left),
                    target_record=compact_record(right),
                    match_score=round(score, 4),
                    match_reason=reasons,
                )
            )
    return sorted(results, key=lambda item: item.match_score, reverse=True)


def load_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected JSON array: {path}")
    return payload


def write_match_results(results: list[MatchResult], output_path: Path = DEFAULT_MATCH_OUTPUT) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate rule-based diagnostic match results for ExtractedRecord JSON.")
    parser.add_argument("--input", default="data/cache/extracted_records.json", help="Input ExtractedRecord JSON path.")
    parser.add_argument("--output", default=str(DEFAULT_MATCH_OUTPUT), help="Output match results JSON path.")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="Minimum score to emit a match.")
    parser.add_argument("--window-days", type=int, default=DEFAULT_TIME_WINDOW_DAYS, help="Bid/open date matching window.")
    args = parser.parse_args()

    records = load_records(Path(args.input))
    results = match_records(records, threshold=args.threshold, window_days=args.window_days)
    write_match_results(results, Path(args.output))
    print(f"wrote {args.output} matches={len(results)}")


if __name__ == "__main__":
    main()
