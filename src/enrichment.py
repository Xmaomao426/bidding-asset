from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from strategies.base import EnrichmentAttempt, StrategyContext
from strategies import favorites, government, local, search_broker, tianyancha, web_candidates


DEFAULT_INPUT = Path("data/cache/extracted_records.json")
DEFAULT_OUTPUT = Path("data/cache/enriched_records.json")
DEFAULT_SUMMARY = Path("data/cache/enrichment_summary.json")
DEFAULT_FAVORITES = Path("config/favorite_sites.json")
DEFAULT_GOVERNMENT_SITES = Path("config/government_sites.json")
DEFAULT_REVIEW_QUEUE = Path("data/diagnostics/manual_review_queue.json")
DEFAULT_WEB_CANDIDATES = Path("data/diagnostics/web_enrichment_candidates.json")
DEFAULT_WEB_CANDIDATES_MD = Path("data/diagnostics/web_enrichment_candidates.md")
DEFAULT_WEB_CANDIDATES_SUMMARY = Path("data/diagnostics/web_enrichment_candidates_summary.json")
DEFAULT_FINAL_RECORDS = Path("data/cache/final_records.json")
DEFAULT_SEARCH_SOURCES = Path("config/search_sources.json")
DEFAULT_SEARCH_BROKER_HITS = Path("data/diagnostics/search_broker_hits.json")
DEFAULT_SEARCH_BROKER_HITS_MD = Path("data/diagnostics/search_broker_hits.md")
DEFAULT_SEARCH_BROKER_CANDIDATES = Path("data/diagnostics/search_broker_candidates.json")
DEFAULT_SEARCH_BROKER_CANDIDATES_MD = Path("data/diagnostics/search_broker_candidates.md")
DEFAULT_SEARCH_BROKER_SUMMARY = Path("data/diagnostics/search_broker_summary.json")
DEFAULT_TIANYANCHA_KEYWORDS = Path("config/tianyancha_keywords.json")
DEFAULT_TIANYANCHA_RAW_ROWS = Path("data/diagnostics/tianyancha_raw_rows.json")
DEFAULT_TIANYANCHA_MATCHED = Path("data/diagnostics/tianyancha_matched_candidates.json")
DEFAULT_TIANYANCHA_MATCHED_MD = Path("data/diagnostics/tianyancha_matched_candidates.md")
DEFAULT_TIANYANCHA_NEW_RECORDS = Path("data/diagnostics/tianyancha_new_record_candidates.json")
DEFAULT_TIANYANCHA_NEW_RECORDS_MD = Path("data/diagnostics/tianyancha_new_record_candidates.md")
DEFAULT_TIANYANCHA_SUMMARY = Path("data/diagnostics/tianyancha_summary.json")


@dataclass
class EnrichedRecord:
    source_file: str
    source_document_id: str
    doc_type: str
    customer: str
    project_name: str
    content: str
    budget: str
    bid_open_time: str
    winner: str
    award_amount: str
    note: str
    text_chars: int
    error: str
    original_winner: str = ""
    original_award_amount: str = ""
    enrichment_success: bool = False
    enrichment_method: str = ""
    enrichment_source_url: str = ""
    enrichment_source_title: str = ""
    winner_source_url: str = ""
    award_amount_source_url: str = ""
    enrichment_attempts: list[dict[str, Any]] = field(default_factory=list)


def load_records(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(payload: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected JSON array: {path}")
    return [item for item in payload if isinstance(item, dict) and item.get("enabled", True)]


def sort_by_priority(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: int(item.get("priority", 999)))


def strategy_chain(args: argparse.Namespace):
    chain = [local.run]
    if not args.offline:
        chain.extend([favorites.run, government.run])
    return chain


def merge_attempt(record: dict[str, Any], attempt: EnrichmentAttempt) -> tuple[str, str, bool]:
    winner = record.get("winner") or ""
    award_amount = record.get("award_amount") or ""
    changed = False
    if not winner and attempt.winner:
        winner = attempt.winner
        changed = True
    if not award_amount and attempt.award_amount:
        award_amount = attempt.award_amount
        changed = True
    return winner, award_amount, changed


def enrich_one(record: dict[str, Any], context: StrategyContext, args: argparse.Namespace) -> EnrichedRecord:
    base = dict(record)
    attempts: list[EnrichmentAttempt] = []
    winner = record.get("winner") or ""
    award_amount = record.get("award_amount") or ""
    successful_attempt: EnrichmentAttempt | None = None

    if winner and award_amount:
        successful_attempt = EnrichmentAttempt(
            source_type="extractor",
            method="already_complete",
            success=True,
            winner=winner,
            award_amount=award_amount,
            message="record_already_complete",
        )
        attempts.append(successful_attempt)
    else:
        for strategy in strategy_chain(args):
            attempt = strategy(record, context)
            attempts.append(attempt)
            next_winner, next_amount, changed = merge_attempt({"winner": winner, "award_amount": award_amount}, attempt)
            if changed:
                winner, award_amount = next_winner, next_amount
                successful_attempt = attempt
            if winner and award_amount:
                break

    attempt_payload = [asdict(attempt) for attempt in attempts]
    source_url = successful_attempt.source_url if successful_attempt else ""

    base["winner"] = winner
    base["award_amount"] = award_amount

    return EnrichedRecord(
        **base,
        original_winner=record.get("winner") or "",
        original_award_amount=record.get("award_amount") or "",
        enrichment_success=bool(successful_attempt and successful_attempt.source_type != "extractor"),
        enrichment_method=successful_attempt.method if successful_attempt else "",
        enrichment_source_url=source_url,
        enrichment_source_title=successful_attempt.source_title if successful_attempt else "",
        winner_source_url=source_url if not record.get("winner") and winner else "",
        award_amount_source_url=source_url if not record.get("award_amount") and award_amount else "",
        enrichment_attempts=attempt_payload,
    )


def enrich_records(records: list[dict[str, Any]], args: argparse.Namespace) -> list[EnrichedRecord]:
    context = StrategyContext(
        records=records,
        favorite_links=sort_by_priority(read_json_list(Path(args.favorites))),
        government_sites=sort_by_priority(read_json_list(Path(args.government_sites))),
        timeout=args.timeout,
        max_favorite_links=args.max_favorite_links,
        government_results=args.government_results,
        sleep=args.sleep,
    )
    targets = records[: args.limit] if args.limit else records
    enriched = [enrich_one(record, context, args) for record in targets]
    if args.limit:
        enriched.extend(
            EnrichedRecord(
                **record,
                original_winner=record.get("winner") or "",
                original_award_amount=record.get("award_amount") or "",
            )
            for record in records[args.limit :]
        )
    return enriched


def build_summary(records: list[EnrichedRecord]) -> dict[str, Any]:
    changed = [
        record
        for record in records
        if (record.winner and not record.original_winner) or (record.award_amount and not record.original_award_amount)
    ]
    return {
        "records": len(records),
        "enrichedRecords": len(changed),
        "winnerFilled": sum(1 for record in records if record.winner and not record.original_winner),
        "awardAmountFilled": sum(1 for record in records if record.award_amount and not record.original_award_amount),
        "winnerCompleteness": round(sum(1 for record in records if record.winner) * 100 / len(records), 1) if records else 0,
        "awardAmountCompleteness": round(sum(1 for record in records if record.award_amount) * 100 / len(records), 1) if records else 0,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich ExtractedRecord winner and award amount fields with source URLs.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Input ExtractedRecord JSON path.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output EnrichedRecord JSON path.")
    parser.add_argument("--summary", default=str(DEFAULT_SUMMARY), help="Output enrichment summary JSON path.")
    parser.add_argument("--favorites", default=str(DEFAULT_FAVORITES), help="JSON file with favorite tender sites.")
    parser.add_argument("--government-sites", default=str(DEFAULT_GOVERNMENT_SITES), help="JSON file with government procurement site search config.")
    parser.add_argument("--offline", action="store_true", help="Use local result announcements only; skip network sources.")
    parser.add_argument("--limit", type=int, default=0, help="Limit records processed, for verification runs.")
    parser.add_argument("--timeout", type=int, default=12, help="Network timeout in seconds.")
    parser.add_argument("--government-results", type=int, default=4, help="Direct government-site results to inspect per site.")
    parser.add_argument("--max-favorite-links", type=int, default=80, help="Favorite links to inspect.")
    parser.add_argument("--sleep", type=float, default=0.4, help="Delay between government direct search requests.")
    parser.add_argument("--web-candidates", action="store_true", help="Generate Web Enrichment V2 candidates without changing records.")
    parser.add_argument("--review-queue", default=str(DEFAULT_REVIEW_QUEUE), help="Manual review queue JSON path for candidate generation.")
    parser.add_argument("--final-records", default=str(DEFAULT_FINAL_RECORDS), help="FinalRecord JSON path used as read-only context for candidates.")
    parser.add_argument("--web-candidates-output", default=str(DEFAULT_WEB_CANDIDATES), help="Web Enrichment V2 candidates JSON output path.")
    parser.add_argument("--web-candidates-md", default=str(DEFAULT_WEB_CANDIDATES_MD), help="Web Enrichment V2 candidates Markdown output path.")
    parser.add_argument("--web-candidates-summary", default=str(DEFAULT_WEB_CANDIDATES_SUMMARY), help="Web Enrichment V2 candidate summary JSON output path.")
    parser.add_argument("--candidate-limit", type=int, default=20, help="Manual review queue records to inspect for web candidates.")
    parser.add_argument("--candidate-results", type=int, default=4, help="Search result links to inspect per source/query.")
    parser.add_argument("--candidate-max-requests", type=int, default=40, help="Maximum network requests for Web Enrichment V2 candidate generation.")
    parser.add_argument("--search-broker", action="store_true", help="Generate controlled search broker hits and candidates without changing records.")
    parser.add_argument("--search-sources", default=str(DEFAULT_SEARCH_SOURCES), help="Allowed official source domains for Search Broker.")
    parser.add_argument("--search-broker-hits", default=str(DEFAULT_SEARCH_BROKER_HITS), help="Search Broker hits JSON output path.")
    parser.add_argument("--search-broker-hits-md", default=str(DEFAULT_SEARCH_BROKER_HITS_MD), help="Search Broker hits Markdown output path.")
    parser.add_argument("--search-broker-candidates", default=str(DEFAULT_SEARCH_BROKER_CANDIDATES), help="Search Broker candidates JSON output path.")
    parser.add_argument("--search-broker-candidates-md", default=str(DEFAULT_SEARCH_BROKER_CANDIDATES_MD), help="Search Broker candidates Markdown output path.")
    parser.add_argument("--search-broker-summary", default=str(DEFAULT_SEARCH_BROKER_SUMMARY), help="Search Broker summary JSON output path.")
    parser.add_argument("--broker-limit", type=int, default=30, help="Manual review queue records to inspect with Search Broker.")
    parser.add_argument("--broker-results", type=int, default=3, help="Search hits to inspect per broker query.")
    parser.add_argument("--broker-max-queries", type=int, default=80, help="Maximum controlled search queries for Search Broker.")
    parser.add_argument("--broker-engine", choices=("google", "bing", "baidu", "sogou", "so"), default="google", help="Search engine used by Search Broker.")
    parser.add_argument("--tianyancha-ingest", action="store_true", help="Parse Tianyancha exported XLSX files into review-only diagnostics.")
    parser.add_argument("--tianyancha-export", action="append", default=[], help="Tianyancha XLSX export path or glob. Can be passed more than once.")
    parser.add_argument("--tianyancha-keywords", default=str(DEFAULT_TIANYANCHA_KEYWORDS), help="Keyword config for Tianyancha new-record candidates.")
    parser.add_argument("--tianyancha-raw-rows", default=str(DEFAULT_TIANYANCHA_RAW_ROWS), help="Tianyancha raw rows JSON output path.")
    parser.add_argument("--tianyancha-matched", default=str(DEFAULT_TIANYANCHA_MATCHED), help="Tianyancha matched candidates JSON output path.")
    parser.add_argument("--tianyancha-matched-md", default=str(DEFAULT_TIANYANCHA_MATCHED_MD), help="Tianyancha matched candidates Markdown output path.")
    parser.add_argument("--tianyancha-new-records", default=str(DEFAULT_TIANYANCHA_NEW_RECORDS), help="Tianyancha new-record candidates JSON output path.")
    parser.add_argument("--tianyancha-new-records-md", default=str(DEFAULT_TIANYANCHA_NEW_RECORDS_MD), help="Tianyancha new-record candidates Markdown output path.")
    parser.add_argument("--tianyancha-summary", default=str(DEFAULT_TIANYANCHA_SUMMARY), help="Tianyancha summary JSON output path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.tianyancha_ingest:
        payload = tianyancha.generate(
            exports=args.tianyancha_export,
            keyword_config_path=Path(args.tianyancha_keywords),
            final_records_path=Path(args.final_records),
            review_queue_path=Path(args.review_queue),
        )
        write_json(payload["raw_rows"], Path(args.tianyancha_raw_rows))
        write_json(
            {
                "generated_at": payload["generated_at"],
                "summary": payload["summary"],
                "matched_candidates": payload["matched_candidates"],
            },
            Path(args.tianyancha_matched),
        )
        tianyancha.write_candidates_markdown(payload, Path(args.tianyancha_matched_md))
        write_json(
            {
                "generated_at": payload["generated_at"],
                "summary": payload["summary"],
                "new_record_candidates": payload["new_record_candidates"],
            },
            Path(args.tianyancha_new_records),
        )
        tianyancha.write_new_records_markdown(payload, Path(args.tianyancha_new_records_md))
        write_json(payload["summary"], Path(args.tianyancha_summary))
        print(
            "tianyancha ingest completed "
            f"rows={payload['summary']['parsed_rows']} "
            f"matched={payload['summary']['matched_candidate_count']} "
            f"new_records={payload['summary']['new_record_candidate_count']} "
            f"keyword_matches={payload['summary']['keyword_matched_rows']}"
        )
        return

    if args.search_broker:
        payload = search_broker.generate(
            review_queue_path=Path(args.review_queue),
            final_records_path=Path(args.final_records),
            search_sources_path=Path(args.search_sources),
            limit=args.broker_limit,
            results_per_query=args.broker_results,
            max_queries=args.broker_max_queries,
            timeout=args.timeout,
            sleep=args.sleep,
            search_engine=args.broker_engine,
        )
        write_json(payload["hits"], Path(args.search_broker_hits))
        search_broker.write_hits_markdown(payload, Path(args.search_broker_hits_md))
        candidates_payload = {
            "generated_at": payload["generated_at"],
            "summary": payload["summary"],
            "confirmed_suggestions": payload["confirmed_suggestions"],
            "review_candidates": payload["review_candidates"],
        }
        write_json(candidates_payload, Path(args.search_broker_candidates))
        search_broker.write_candidates_markdown(payload, Path(args.search_broker_candidates_md))
        write_json(payload["summary"], Path(args.search_broker_summary))
        print(
            "search broker completed "
            f"records={payload['summary']['checked_records']} "
            f"queries={payload['summary']['generated_queries']} "
            f"hits={payload['summary']['search_hit_count']} "
            f"candidates={payload['summary']['candidate_count']}"
        )
        return

    if args.web_candidates:
        payload = web_candidates.generate_candidates(
            review_queue_path=Path(args.review_queue),
            final_records_path=Path(args.final_records),
            government_sites_path=Path(args.government_sites),
            favorite_sites_path=Path(args.favorites),
            timeout=args.timeout,
            limit=args.candidate_limit,
            results_per_source=args.candidate_results,
            sleep=args.sleep,
            max_requests=args.candidate_max_requests,
        )
        write_json(payload, Path(args.web_candidates_output))
        web_candidates.write_candidates_markdown(payload, Path(args.web_candidates_md))
        web_candidates.write_candidates_summary(payload, Path(args.web_candidates_summary))
        print(
            "web enrichment candidates completed "
            f"records={payload['records_considered']} "
            f"candidates={payload['candidate_count']} "
            f"confirmed={payload['confirmed_suggestions_count']} "
            f"review={payload['review_candidates_count']} "
            f"requests={payload['summary']['used_requests']}"
        )
        return

    records = load_records(Path(args.input))
    enriched = enrich_records(records, args)
    payload = [asdict(record) for record in enriched]
    summary = build_summary(enriched)
    write_json(payload, Path(args.output))
    write_json(summary, Path(args.summary))
    print(
        "enrichment completed "
        f"records={summary['records']} "
        f"enriched={summary['enrichedRecords']} "
        f"winner_filled={summary['winnerFilled']} "
        f"amount_filled={summary['awardAmountFilled']}"
    )


if __name__ == "__main__":
    main()
