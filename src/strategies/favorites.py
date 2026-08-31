from __future__ import annotations

from typing import Any

from strategies.base import EnrichmentAttempt, StrategyContext, build_attempt, clean_text, fetch, is_relevant


def run(record: dict[str, Any], context: StrategyContext) -> EnrichmentAttempt:
    if not context.favorite_links:
        return EnrichmentAttempt(source_type="favorite_site", method="fetch_favorite_link", success=False, message="no_favorite_links")

    errors = 0
    for link in context.favorite_links[: context.max_favorite_links]:
        name = str(link.get("name") or link.get("title") or "")
        url = str(link.get("url") or "")
        if not name or not url:
            continue
        if not is_relevant(name, record):
            continue
        try:
            page = clean_text(fetch(url, context.timeout))
        except Exception:
            errors += 1
            continue
        attempt = build_attempt("favorite_site", "fetch_favorite_link", record, f"{name} {page}", url, name)
        if attempt.success:
            return attempt

    return EnrichmentAttempt(
        source_type="favorite_site",
        method="fetch_favorite_link",
        success=False,
        message="no_matching_favorite_result" if not errors else f"fetch_errors={errors}",
    )
