from __future__ import annotations

import html
import re
import time
import urllib.parse
from typing import Any

from strategies.base import EnrichmentAttempt, StrategyContext, build_attempt, build_search_queries, clean_text, fetch


def absolute_url(href: str, base: str) -> str:
    return urllib.parse.urljoin(base, html.unescape(href))


def extract_links(page: str, base: str, limit: int) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen = set()
    for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, flags=re.I | re.S):
        url = absolute_url(match.group(1), base)
        title = clean_text(match.group(2))
        if not title or url in seen:
            continue
        if not any(term in title for term in ("中标", "成交", "结果", "合同", "公告")):
            continue
        seen.add(url)
        links.append({"title": title, "url": url})
        if len(links) >= limit:
            break
    return links


def run(record: dict[str, Any], context: StrategyContext) -> EnrichmentAttempt:
    if not context.government_sites:
        return EnrichmentAttempt(
            source_type="government_procurement_site",
            method="direct_site_search",
            success=False,
            message="no_government_sites_config",
    )

    errors = 0
    queries = build_search_queries(record)
    if not queries:
        return EnrichmentAttempt(
            source_type="government_procurement_site",
            method="direct_site_search",
            success=False,
            message="no_query_terms",
        )

    for site in context.government_sites:
        name = str(site.get("name") or "")
        search_template = str(site.get("search_url") or "")
        base_url = str(site.get("base_url") or site.get("base") or "")
        if not name or not search_template or not base_url:
            continue

        for raw_query in queries:
            search_url = search_template.format(query=urllib.parse.quote(raw_query))
            try:
                search_page = fetch(search_url, context.timeout)
            except Exception:
                errors += 1
                continue

            links = extract_links(search_page, base_url, context.government_results)
            for link in links:
                try:
                    page = clean_text(fetch(link["url"], context.timeout))
                except Exception:
                    errors += 1
                    continue
                attempt = build_attempt(
                    "government_procurement_site",
                    f"direct_site_search:{name}",
                    record,
                    f"{link['title']} {page}",
                    link["url"],
                    link["title"],
                )
                if attempt.success:
                    return attempt
            if context.sleep:
                time.sleep(context.sleep)

    return EnrichmentAttempt(
        source_type="government_procurement_site",
        method="direct_site_search",
        success=False,
        message="direct_site_search_failed_or_no_match" if errors else "no_direct_site_match",
    )
