"""Bounded local headless-browser DOM capture for operator-triggered acquisition."""

from __future__ import annotations

import ipaddress
import hashlib
import json
import re
import socket
import time
import unicodedata
from difflib import SequenceMatcher
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_VIRTUAL_TIME_BUDGET_MS = 45_000
DEFAULT_TIMEOUT_SECONDS = 60
MAX_ATTEMPT_TIMEOUT_SECONDS = 60
MAX_ATTEMPTS = 1
MAX_DOM_BYTES = 5 * 1024 * 1024
MAX_HTTP_HTML_BYTES = 5 * 1024 * 1024
MAX_HTTP_REDIRECTS = 5
DEFAULT_HTTP_TIMEOUT_SECONDS = 20
ATTACHMENT_PATH_SUFFIXES = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".7z"}
DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


@dataclass(frozen=True)
class HeadlessBrowserCapture:
    dom: str
    metadata: dict[str, Any]


class HeadlessBrowserCaptureError(RuntimeError):
    """Stable operator-safe failure with no subprocess or local-path disclosure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _HttpHtmlResponse:
    html: str
    final_url: str
    status: int
    content_type: str
    byte_count: int
    redirect_count: int


class _SafeHtmlRedirectHandler(HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.redirect_count = 0

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        count = int(getattr(req, "notice_redirect_count", 0) or 0) + 1
        if count > MAX_HTTP_REDIRECTS:
            raise HeadlessBrowserCaptureError(
                "http_redirect_limit", "公告页面 HTTP 重定向次数超过限制。"
            )
        safe_url = validate_automatic_capture_url(urljoin(str(req.full_url), newurl))
        redirected = super().redirect_request(req, fp, code, msg, headers, safe_url)
        if redirected is not None:
            redirected.notice_redirect_count = count
            self.redirect_count = count
        return redirected


def validate_capture_url(value: str) -> str:
    """Validate the operator item's original URL before launching a browser."""
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise _invalid_url_error() from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise _invalid_url_error()
    if parsed.username is not None or parsed.password is not None:
        raise _invalid_url_error()
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise _invalid_url_error()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    ):
        raise _invalid_url_error()
    if port is not None and not 1 <= port <= 65_535:
        raise _invalid_url_error()
    return url


def validate_automatic_capture_url(value: str) -> str:
    """Allow automatic capture only for a DNS-verified public web-document URL."""
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise _automatic_url_error("invalid_hostname", "URL 主机名格式无效。") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise _automatic_url_error("unsupported_scheme", "自动浏览器仅支持 HTTP/HTTPS URL。")
    if not parsed.hostname:
        raise _automatic_url_error("missing_hostname", "自动浏览器 URL 缺少主机名。")
    if parsed.username is not None or parsed.password is not None:
        raise _automatic_url_error("url_credentials", "自动浏览器 URL 不允许包含凭证。")
    try:
        port = parsed.port
    except ValueError as exc:
        raise _automatic_url_error("invalid_port", "自动浏览器 URL 端口无效。") from exc
    if port is not None and not 1 <= port <= 65_535:
        raise _automatic_url_error("invalid_port", "自动浏览器 URL 端口无效。")

    hostname = str(parsed.hostname or "").rstrip(".").lower()
    if not hostname or hostname == "localhost" or hostname.endswith(".localhost"):
        raise _automatic_url_error("non_public_destination", "自动浏览器 URL 必须指向公网地址。")
    if Path(unquote(parsed.path)).suffix.lower() in ATTACHMENT_PATH_SUFFIXES:
        raise _automatic_url_error("attachment_path", "自动浏览器不处理附件型 URL。")

    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        _require_global_address(literal_address)
        return url

    ascii_hostname = _validated_dns_hostname(hostname)
    try:
        answers = socket.getaddrinfo(
            ascii_hostname,
            port or (443 if parsed.scheme.lower() == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except (OSError, UnicodeError) as exc:
        raise _automatic_url_error("dns_resolution_failed", "自动浏览器 URL 的 DNS 解析失败。") from exc
    resolved_addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for answer in answers:
        try:
            resolved_addresses.append(ipaddress.ip_address(str(answer[4][0])))
        except (IndexError, TypeError, ValueError) as exc:
            raise _automatic_url_error("dns_resolution_failed", "自动浏览器 URL 的 DNS 解析结果无效。") from exc
    if not resolved_addresses:
        raise _automatic_url_error("dns_resolution_failed", "自动浏览器 URL 的 DNS 解析结果为空。")
    for address in resolved_addresses:
        _require_global_address(address)
    return url


def _validated_dns_hostname(hostname: str) -> str:
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise _automatic_url_error("invalid_hostname", "自动浏览器 URL 主机名格式无效。") from exc
    if len(ascii_hostname) > 253:
        raise _automatic_url_error("invalid_hostname", "自动浏览器 URL 主机名格式无效。")
    labels = ascii_hostname.split(".")
    if not labels or any(not DNS_LABEL_PATTERN.fullmatch(label) for label in labels):
        raise _automatic_url_error("invalid_hostname", "自动浏览器 URL 主机名格式无效。")
    return ascii_hostname


def _require_global_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if not address.is_global:
        raise _automatic_url_error("non_public_destination", "自动浏览器 URL 必须仅解析到公网地址。")


def capture_rendered_dom(
    url: str,
    *,
    virtual_time_budget_ms: int = DEFAULT_VIRTUAL_TIME_BUDGET_MS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = MAX_ATTEMPTS,
) -> HeadlessBrowserCapture:
    """Use one isolated Playwright-managed Chrome page to select one structured region."""
    target_url = validate_capture_url(url)
    virtual_budget = int(virtual_time_budget_ms)
    requested_timeout = int(timeout_seconds)
    if virtual_budget <= 0 or requested_timeout <= 0:
        raise ValueError("Headless browser time limits must be positive")
    bounded_attempts = int(max_attempts)
    if bounded_attempts <= 0 or bounded_attempts > MAX_ATTEMPTS:
        raise ValueError("Headless browser attempt limit is invalid")
    attempt_timeout = min(requested_timeout, MAX_ATTEMPT_TIMEOUT_SECONDS)
    region = _capture_structured_region_with_playwright(
        target_url,
        timeout_seconds=attempt_timeout,
        render_budget_seconds=min(virtual_budget / 1000.0, attempt_timeout - 1.0),
    )
    payload = json.dumps(region, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_DOM_BYTES:
        raise HeadlessBrowserCaptureError(
            "dom_too_large",
            "本机 Google Chrome 返回的公告内容区域超过大小限制。",
        )
    return HeadlessBrowserCapture(
        dom=payload,
        metadata={
            "browser_family": "chrome",
            "capture_mode": "playwright_chrome_frame_structured_dom",
            "virtual_time_budget_ms": virtual_budget,
            "attempt_count": 1,
            "prior_retryable_failure_code": "",
        },
    )


def _capture_http_html_dom(
    url: str,
    *,
    timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
    max_bytes: int = MAX_HTTP_HTML_BYTES,
) -> HeadlessBrowserCapture:
    """Fetch one bounded public HTML response and run the current Region script locally."""
    response = _http_get(url, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
    region = _capture_structured_region_from_html(
        response.html,
        base_url=response.final_url,
        timeout_seconds=timeout_seconds,
    )
    payload = json.dumps(region, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_DOM_BYTES:
        raise HeadlessBrowserCaptureError("dom_too_large", "HTTP 公告内容区域超过大小限制。")
    return HeadlessBrowserCapture(
        dom=payload,
        metadata={
            "capture_mode": "http_html_current_playwright_region_script",
            "http_attempt_count": 1,
            "target_site_browser_navigation_count": 0,
            "http_status": response.status,
            "http_final_url": response.final_url,
            "http_content_type": response.content_type,
            "http_body_bytes": response.byte_count,
            "http_redirect_count": response.redirect_count,
            "attempt_count": 1,
            "acquisition_route": {
                "selected_method": "http",
                "http_attempted": 1,
                "http_result": "success",
                "chrome_attempted": 0,
                "chrome_result": "not_attempted",
            },
        },
    )


def _http_get(url: str, *, timeout_seconds: int, max_bytes: int) -> _HttpHtmlResponse:
    safe_url = validate_automatic_capture_url(url)
    request = Request(
        safe_url,
        headers={"User-Agent": "BiddingAssetNoticeFetcher/1.0"},
    )
    request.notice_redirect_count = 0
    redirect_handler = _SafeHtmlRedirectHandler()
    try:
        with build_opener(redirect_handler).open(
            request, timeout=max(int(timeout_seconds), 1)
        ) as response:
            final_url = validate_automatic_capture_url(str(response.geturl()))
            status = int(getattr(response, "status", 200) or 200)
            headers = response.headers
            content_type = str(headers.get("Content-Type") or "")
            media_type = content_type.split(";", 1)[0].strip().casefold()
            if media_type not in {"text/html", "application/xhtml+xml"}:
                raise HeadlessBrowserCaptureError(
                    "http_content_type", "HTTP 响应不是公告 HTML。"
                )
            content_length = headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise HeadlessBrowserCaptureError("http_body_too_large", "HTTP 公告正文超过大小限制。")
            content = response.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise HeadlessBrowserCaptureError("http_body_too_large", "HTTP 公告正文超过大小限制。")
            charset = headers.get_content_charset() if isinstance(headers, Message) else None
            html = content.decode(charset or "utf-8", errors="replace")
            redirect_count = redirect_handler.redirect_count
    except HeadlessBrowserCaptureError:
        raise
    except HTTPError as exc:
        raise HeadlessBrowserCaptureError(
            "http_status_error", "HTTP 公告页面返回不可用状态。"
        ) from exc
    except (OSError, URLError, ValueError) as exc:
        raise HeadlessBrowserCaptureError(
            "http_transport_failed", "HTTP 公告页面获取失败。"
        ) from exc
    return _HttpHtmlResponse(
        html=html,
        final_url=final_url,
        status=status,
        content_type=content_type,
        byte_count=len(content),
        redirect_count=redirect_count,
    )


def _capture_structured_region_from_html(
    html: str, *, base_url: str, timeout_seconds: int
) -> dict[str, Any]:
    """Evaluate fetched HTML without any target-site navigation or subresource access."""
    playwright = browser = context = page = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(accept_downloads=False, service_workers="block")
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        page.set_default_timeout(max(int(timeout_seconds * 1000), 1))
        page.set_content(
            str(html or ""),
            wait_until="domcontentloaded",
            timeout=max(int(timeout_seconds * 1000), 1),
        )
        page.evaluate(
            """baseUrl => {
                let base = document.head && document.head.querySelector('base');
                if (!base && document.head) {
                    base = document.createElement('base');
                    document.head.prepend(base);
                }
                if (base) base.href = baseUrl;
            }""",
            base_url,
        )
        region = _select_unique_playwright_region(_evaluate_playwright_frames(page))
        if region is None:
            raise HeadlessBrowserCaptureError(
                "notice_content_frame_not_found",
                "HTTP HTML does not contain a unique reliable notice-content region.",
            )
        return region
    except HeadlessBrowserCaptureError:
        raise
    except PlaywrightTimeoutError as exc:
        raise HeadlessBrowserCaptureError(
            "http_region_timeout", "HTTP HTML 未能在限定时间内形成公告内容区域。"
        ) from exc
    except PlaywrightError as exc:
        raise HeadlessBrowserCaptureError(
            "http_region_technical_error", "HTTP HTML 公告内容区域计算失败。"
        ) from exc
    finally:
        for resource in (page, context, browser):
            if resource is not None:
                try:
                    resource.close()
                except PlaywrightError:
                    pass
        if playwright is not None:
            try:
                playwright.stop()
            except PlaywrightError:
                pass


_PLAYWRIGHT_NOTICE_CANDIDATE_SCRIPT = r"""
() => {
  const ignored = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','CANVAS']);
  const headings = new Set(['H1','H2','H3','H4','H5','H6']);
  const shellTags = new Set(['ASIDE','FOOTER','HEADER','NAV']);
  const contentBlocks = new Set(['BLOCKQUOTE','DD','DT','FIGCAPTION','FIELDSET','LI','P','PRE','TR']);
  const controls = new Set(['BUTTON','INPUT','OPTION','SELECT','TEXTAREA']);
  const ANCHOR_CAP = 64;
  const REGION_CAP = 256;
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
  const renderedCache = new WeakMap();
  const visibleCache = new WeakMap();
  const elementsCache = new WeakMap();
  const textCache = new WeakMap();
  const statsCache = new WeakMap();
  const shellCache = new WeakMap();
  const childElementsCache = new WeakMap();
  const childNodesCache = new WeakMap();
  const visibleChildrenCache = new WeakMap();
  const memo = (cache, key, compute) => {
    if (!cache.has(key)) cache.set(key, compute());
    return cache.get(key);
  };
  const rendered = element => {
    if (!element || element.nodeType !== Node.ELEMENT_NODE) return false;
    return memo(renderedCache, element, () => {
    const style = getComputedStyle(element);
    return !element.hidden
      && element.getAttribute('aria-hidden') !== 'true'
      && style.display !== 'none'
      && style.visibility !== 'hidden'
      && (!element.parentElement || rendered(element.parentElement));
    });
  };
  const visible = element => {
    if (!element || ignored.has(element.tagName)) return false;
    return memo(visibleCache, element, () => rendered(element));
  };
  const childElements = root => memo(childElementsCache, root, () => Array.from(root.children));
  const childNodes = root => memo(childNodesCache, root, () => Array.from(root.childNodes));
  const visibleChildren = root => memo(visibleChildrenCache, root, () => childElements(root).filter(visible));
  const elements = root => {
    return memo(elementsCache, root, () => {
      const rows = [];
      for (const child of childElements(root)) {
        if (visible(child)) rows.push(child);
        rows.push(...elements(child));
      }
      return rows;
    });
  };
  const text = node => {
    return memo(textCache, node, () => {
      if (node.nodeType === Node.TEXT_NODE) return normalize(node.nodeValue);
      if (node.nodeType === Node.ELEMENT_NODE && visible(node)) return normalize(childNodes(node).map(text).filter(Boolean).join(' '));
      return '';
    });
  };
  const navigationHref = anchor => {
    if (anchor.hasAttribute('download')) return false;
    const raw = normalize(anchor.getAttribute('href'));
    if (!raw || /^(javascript:|mailto:|data:|#)/i.test(raw)) return false;
    try {
      const parsed = new URL(raw, document.baseURI);
      const parts = parsed.pathname.split('/').filter(Boolean);
      return !parsed.search && !parsed.hash && parts.length <= 1;
    } catch (_) { return false; }
  };
  const stats = root => {
    return memo(statsCache, root, () => {
      const nodes = [root, ...elements(root)];
      const value = text(root);
      const anchors = nodes.filter(node => node.tagName === 'A' && node.hasAttribute('href'));
      const linkText = anchors.reduce((total, node) => total + text(node).length, 0);
      let depth = 0; for (let node = root.parentElement; node; node = node.parentElement) depth += 1;
      return {
        textChars: value.length,
        elementCount: nodes.length,
        blockCount: nodes.filter(node => contentBlocks.has(node.tagName)).length,
        headingCount: nodes.filter(node => headings.has(node.tagName)).length,
        tableCount: nodes.filter(node => node.tagName === 'TABLE').length,
        linkCount: anchors.length,
        navigationLinkCount: anchors.filter(navigationHref).length,
        linkRatio: linkText / Math.max(value.length, 1),
        controlCount: nodes.filter(node => controls.has(node.tagName)).length,
        depth,
      };
    });
  };
  const shell = node => {
    if (shellTags.has(node.tagName)) return true;
    return memo(shellCache, node, () => {
      const s = stats(node);
      if (s.tableCount || s.headingCount || s.textChars > 180) return false;
      const anchors = [node, ...elements(node)].filter(item => item.tagName === 'A' && item.hasAttribute('href'));
      return anchors.length >= 2 && anchors.every(navigationHref) && s.linkRatio >= 0.55;
    });
  };
  const attributes = node => {
    const result = {};
    if (node.tagName === 'A' && node.hasAttribute('href')) {
      const raw = normalize(node.getAttribute('href'));
      result.href = raw;
      let executable = false;
      try {
        const resolved = new URL(raw, document.baseURI);
        executable = resolved.protocol === 'http:' || resolved.protocol === 'https:';
        if (executable) result.resolved_href = resolved.href;
      } catch (_) {}
      result.executable = executable;
      if (node.hasAttribute('download')) result.download = node.getAttribute('download') || '';
    }
    for (const name of ['rowspan','colspan','scope']) if (node.hasAttribute(name)) result[name] = node.getAttribute(name);
    return result;
  };
  const serialize = node => {
    const children = [];
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        const value = normalize(child.nodeValue); if (value) children.push({type:'text', text:value});
      } else if (child.nodeType === Node.ELEMENT_NODE && visible(child)) {
        children.push(serialize(child));
      }
    }
    const result = {type:'element', tag:node.tagName.toLowerCase(), children};
    const kept = attributes(node); if (Object.keys(kept).length) result.attributes = kept;
    return result;
  };
  const structuredText = node => node.type === 'text' ? node.text : (node.children || []).map(structuredText).filter(Boolean).join('\n');
  const linkRow = node => {
    const raw = normalize(node.getAttribute('href'));
    let resolved = '', executable = false;
    try { const url = new URL(raw, document.baseURI); executable = url.protocol === 'http:' || url.protocol === 'https:'; if (executable) resolved = url.href; } catch (_) {}
    const download = node.hasAttribute('download') ? (node.getAttribute('download') || '') : '';
    return {text:text(node) || download || raw, url:resolved, raw_href:raw, download, executable};
  };
  const attachmentSuffixes = new Set(['.pdf','.doc','.docx','.xls','.xlsx','.zip','.rar','.7z']);
  const fileAttachmentLink = node => {
    if (node.hasAttribute('download')) return true;
    const candidates = [normalize(node.getAttribute('href')), text(node)];
    try { candidates.push(new URL(node.getAttribute('href'), document.baseURI).pathname); } catch (_) {}
    return candidates.some(value => {
      const normalized = String(value || '').split(/[?#]/, 1)[0].toLowerCase();
      return Array.from(attachmentSuffixes).some(suffix => normalized.endsWith(suffix));
    });
  };
  const all = Array.from(document.getElementsByTagName('*')).filter(visible);
  const ids = new Set(all.map(node => (node.id || '').toLowerCase()));
  const classes = new Set(all.flatMap(node => Array.from(node.classList || []).map(value => value.toLowerCase())));
  const technicalText = normalize(document.body ? document.body.textContent : '');
  if ((/\bERR_[A-Z0-9_]+\b/.test(technicalText) && (ids.has('main-frame-error') || ids.has('main-message') || classes.has('interstitial-wrapper') || classes.has('neterror'))) || location.protocol === 'chrome-error:') return {status:'technical_error'};

  const documentTitle = normalize(document.title);
  const blockedMarker = `${documentTitle} ${technicalText}`;
  if (/\b403\s+forbidden\b/i.test(blockedMarker) || /\bsorry[,]?\s+you\s+have\s+been\s+blocked\b/i.test(blockedMarker) || /\baccess\s+denied\b/i.test(documentTitle)) return {status:'access_blocked'};
  const titleSimilarity = value => {
    if (!value || !documentTitle) return 0;
    if (value === documentTitle) return 1;
    if (value.includes(documentTitle) || documentTitle.includes(value)) return Math.min(value.length, documentTitle.length) / Math.max(value.length, documentTitle.length);
    return 0;
  };
  const titleLike = node => {
    const value = text(node);
    if (value.length < 4 || value.length > 240 || shellTags.has(node.tagName)) return false;
    if (headings.has(node.tagName)) return true;
    if (['CAPTION','LEGEND'].includes(node.tagName)) return value.length <= 200;
    if (!['DIV','LABEL','P','SECTION','TD','TH'].includes(node.tagName) && !node.tagName.includes('-')) return false;
    if (elements(node).some(child => headings.has(child.tagName) || child.tagName === 'TABLE')) return false;
    const childBlocks = Array.from(node.children).filter(visible).filter(child => contentBlocks.has(child.tagName)).length;
    if (childBlocks > 1) return false;
    const style = getComputedStyle(node);
    const weight = Number.parseInt(style.fontWeight, 10) || (style.fontWeight === 'bold' ? 700 : 400);
    const fontSize = Number.parseFloat(style.fontSize) || 0;
    const siblings = node.parentElement ? Array.from(node.parentElement.children).filter(visible) : [];
    const position = siblings.indexOf(node);
    const table = ['TD','TH'].includes(node.tagName) ? node.closest('table') : null;
    const row = table ? node.closest('tr') : null;
    const tableRows = table ? Array.from(table.rows || []).filter(visible) : [];
    const rowCells = row ? Array.from(row.cells || []).filter(visible) : [];
    const tableTitleCell = Boolean(table && row && tableRows.length >= 2 && tableRows[0] === row && rowCells.length === 1);
    const isolatedLead = position >= 0 && position <= 1 && siblings.length >= 3 && node.children.length === 0 && value.length <= 160;
    return titleSimilarity(value) >= 0.45 || weight >= 600 || fontSize >= 18 || tableTitleCell || isolatedLead;
  };
  const anchorScore = node => {
    const value = text(node);
    const level = headings.has(node.tagName) ? 8 - Number(node.tagName.slice(1)) : 0;
    const style = getComputedStyle(node);
    const weight = Number.parseInt(style.fontWeight, 10) || (style.fontWeight === 'bold' ? 700 : 400);
    const fontSize = Number.parseFloat(style.fontSize) || 0;
    const rect = node.getBoundingClientRect();
    return titleSimilarity(value) * 500 + level * 35 + Math.min(fontSize, 48) * 4 + (weight >= 600 ? 80 : 0) - Math.max(rect.top, 0) / 1000;
  };
  const anchors = all.filter(titleLike).map((node, order) => ({node, order, score:anchorScore(node)}));
  anchors.sort((a,b) => b.score - a.score || a.order - b.order);
  if (!anchors.length) return {status:'not_found'};

  const nodeIds = new WeakMap(); let nextNodeId = 1;
  const nodeId = node => { if (!nodeIds.has(node)) nodeIds.set(node, nextNodeId++); return nodeIds.get(node); };
  const intervalNodesCache = new WeakMap();
  const intervalElementsCache = new WeakMap();
  const intervalTextCache = new WeakMap();
  const intervalNodes = candidate => {
    return memo(intervalNodesCache, candidate, () => {
      const children = childNodes(candidate.parent);
      const first = children.indexOf(candidate.startElement), last = children.indexOf(candidate.endElement);
      return first < 0 || last < first ? [] : children.slice(first, last + 1);
    });
  };
  const intervalElements = candidate => {
    return memo(intervalElementsCache, candidate, () => {
      const rows = [];
      for (const node of intervalNodes(candidate)) if (node.nodeType === Node.ELEMENT_NODE && visible(node)) rows.push(node, ...elements(node));
      return rows;
    });
  };
  const intervalText = candidate => memo(intervalTextCache, candidate, () => normalize(intervalNodes(candidate).map(node => node.nodeType === Node.TEXT_NODE ? normalize(node.nodeValue) : (node.nodeType === Node.ELEMENT_NODE && visible(node) ? text(node) : '')).join(' ')));
  const insideShell = node => {
    for (let current = node; current && current !== document.body; current = current.parentElement) if (shellTags.has(current.tagName)) return true;
    return false;
  };
  const columnBreak = (anchorBranch, sibling) => {
    const left = anchorBranch.getBoundingClientRect(), right = sibling.getBoundingClientRect();
    if (left.width <= 1 || left.height <= 1 || right.width <= 1 || right.height <= 1) return false;
    const overlap = Math.max(0, Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top));
    const verticalRatio = overlap / Math.max(1, Math.min(left.height, right.height));
    const separated = right.right <= left.left + 1 || right.left >= left.right - 1;
    return separated && verticalRatio >= 0.35;
  };
  const candidateSignals = candidate => {
    const nodes = intervalElements(candidate);
    const value = intervalText(candidate);
    const linkNodes = nodes.filter(node => node.tagName === 'A' && node.hasAttribute('href'));
    const linkText = linkNodes.reduce((total, node) => total + text(node).length, 0);
    const leafBlocks = nodes.filter(node => {
      if (!contentBlocks.has(node.tagName) || node === candidate.anchor || node.contains(candidate.anchor)) return false;
      if (text(node).length < 4) return false;
      return !Array.from(node.children).filter(visible).some(child => contentBlocks.has(child.tagName) && text(child).length >= 4);
    });
    return {
      textChars:value.length,
      blockCount:leafBlocks.length,
      tableCount:nodes.filter(node => node.tagName === 'TABLE').length + Number(candidate.parent.tagName === 'TABLE'),
      linkCount:linkNodes.length,
      navigationLinkCount:linkNodes.filter(navigationHref).length,
      linkRatio:linkText / Math.max(value.length, 1),
      controlCount:nodes.filter(node => controls.has(node.tagName)).length + Number(controls.has(candidate.parent.tagName)),
    };
  };
  const nestedFrame = window.top !== window;
  const nestedWholeBodyMergesIndependentRoots = candidate => {
    if (!nestedFrame || candidate.parent.tagName !== 'BODY' || candidate.visibleStart !== 0 || candidate.visibleEnd !== candidate.visibleChildren.length - 1) return false;
    const businessRoots = candidate.visibleChildren.filter(child => {
      if (shell(child)) return false;
      const nodes = [child, ...elements(child)];
      const hasPrimaryHeading = nodes.some(node => node.tagName === 'H1' && text(node));
      return hasPrimaryHeading && stats(child).blockCount >= 2;
    });
    return businessRoots.length >= 2;
  };
  const accepted = candidate => {
    if (!candidate.anchor.isConnected || insideShell(candidate.anchor)) return false;
    if (!nestedFrame && candidate.parent.tagName === 'BODY' && candidate.visibleStart === 0 && candidate.visibleEnd === candidate.visibleChildren.length - 1) return false;
    const signal = candidateSignals(candidate); candidate.signal = signal;
    if (!signal.textChars || (!signal.tableCount && signal.blockCount < 2)) return false;
    if (!signal.tableCount && signal.controlCount >= Math.max(2, signal.blockCount)) return false;
    if (!signal.tableCount && signal.linkCount >= 3 && (signal.linkRatio >= 0.55 || signal.navigationLinkCount >= 3)) return false;
    return true;
  };
  const intervals = new Map(); let overflow = false;
  for (const anchor of anchors) {
    let branch = anchor.node, parent = branch.parentElement;
    while (parent && parent.tagName !== 'HTML') {
      if (!visible(parent)) break;
      const visibleChildRows = visibleChildren(parent);
      const branchIndex = visibleChildRows.indexOf(branch);
      if (branchIndex >= 0) {
        let start = 0, end = visibleChildRows.length - 1;
        while (start < branchIndex && shell(visibleChildRows[start])) start += 1;
        while (end > branchIndex && shell(visibleChildRows[end])) end -= 1;
        for (let index = branchIndex - 1; index >= start; index -= 1) if (columnBreak(branch, visibleChildRows[index])) { start = index + 1; break; }
        for (let index = branchIndex + 1; index <= end; index += 1) if (columnBreak(branch, visibleChildRows[index])) { end = index - 1; break; }
        if (start <= branchIndex && branchIndex <= end) {
          const startElement = visibleChildRows[start], endElement = visibleChildRows[end];
          const elementChildren = childElements(parent);
          const startIndex = elementChildren.indexOf(startElement), endIndex = elementChildren.indexOf(endElement);
          const key = `${nodeId(parent)}:${startIndex}:${endIndex}`;
          if (!intervals.has(key)) {
            intervals.set(key, {parent, startElement, endElement, startIndex, endIndex, visibleStart:start, visibleEnd:end, visibleChildren:visibleChildRows, depth:stats(parent).depth, anchors:[]});
            if (intervals.size > REGION_CAP) { overflow = true; break; }
          }
          intervals.get(key).anchors.push(anchor);
        }
      }
      if (parent.tagName === 'BODY') break;
      branch = parent; parent = parent.parentElement;
    }
    if (overflow) break;
  }
  if (overflow) return {status:'ambiguous', reason:'region_overflow', region_count:intervals.size, region_cap:REGION_CAP};
  const candidates = [];
  for (const interval of intervals.values()) {
    const acceptedAnchors = interval.anchors.map(anchor => ({...interval, anchor:anchor.node, anchorScore:anchor.score})).filter(accepted);
    if (acceptedAnchors.length) candidates.push({...acceptedAnchors[0], acceptedAnchors});
  }
  if (!candidates.length) return {status:'not_found'};
  if (candidates.length > ANCHOR_CAP) return {status:'ambiguous', reason:'anchor_overflow', anchor_count:candidates.length, anchor_cap:ANCHOR_CAP};

  const coverageTokens = candidate => {
    const tokens = [];
    const visit = node => {
      if (node.nodeType === Node.TEXT_NODE) {
        if (normalize(node.nodeValue)) tokens.push(`t${nodeId(node)}`);
      } else if (node.nodeType === Node.ELEMENT_NODE && visible(node)) {
        const visibleElements = Array.from(node.children).filter(visible);
        if (!visibleElements.length || ['A','FORM','TABLE','TBODY','TD','TFOOT','TH','THEAD','TR'].includes(node.tagName) || controls.has(node.tagName)) tokens.push(`e${nodeId(node)}`);
        for (const child of node.childNodes) visit(child);
      }
    };
    for (const node of intervalNodes(candidate)) visit(node);
    return tokens;
  };
  const groups = new Map();
  for (const candidate of candidates) {
    const tokens = coverageTokens(candidate);
    const key = tokens.join(',');
    if (!groups.has(key)) groups.set(key, {tokens, candidates:[]});
    groups.get(key).candidates.push(candidate);
  }
  let selectedGroup = null;
  if (groups.size === 1) selectedGroup = Array.from(groups.values())[0];
  else {
    const rows = Array.from(groups.values());
    if (rows.some(row => row.candidates.some(nestedWholeBodyMergesIndependentRoots))) {
      return {status:'ambiguous', reason:'non_equivalent_regions', accepted_region_count:groups.size};
    }
    const maximal = rows.filter(row => rows.every(other => {
      if (row === other) return true;
      if (row.tokens.length <= other.tokens.length) return false;
      const own = new Set(row.tokens);
      return other.tokens.every(token => own.has(token));
    }));
    if (maximal.length === 1) selectedGroup = maximal[0];
  }
  if (!selectedGroup) return {status:'ambiguous', reason:'non_equivalent_regions', accepted_region_count:groups.size};
  const equivalent = selectedGroup.candidates;
  equivalent.sort((a,b) => a.depth - b.depth || b.anchorScore - a.anchorScore || a.startIndex - b.startIndex || a.endIndex - b.endIndex);
  const selected = equivalent[0];
  const acceptedHeadingAnchors = selected.acceptedAnchors.filter(candidate => headings.has(candidate.anchor.tagName));
  acceptedHeadingAnchors.sort((a,b) => {
    if (a.anchor === b.anchor) return 0;
    return a.anchor.compareDocumentPosition(b.anchor) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
  });
  const titleAnchor = acceptedHeadingAnchors.length ? acceptedHeadingAnchors[0].anchor : selected.anchor;
  const serializedChildren = [];
  for (const node of intervalNodes(selected)) {
    if (node.nodeType === Node.TEXT_NODE) {
      const value = normalize(node.nodeValue); if (value) serializedChildren.push({type:'text', text:value});
    } else if (node.nodeType === Node.ELEMENT_NODE && visible(node)) serializedChildren.push(serialize(node));
  }
  let regionChildren = serializedChildren;
  if (selected.parent.tagName !== 'BODY') {
    const projected = {type:'element', tag:selected.parent.tagName.toLowerCase(), children:serializedChildren};
    const kept = attributes(selected.parent); if (Object.keys(kept).length) projected.attributes = kept;
    regionChildren = [projected];
  }
  const structured = {type:'element', tag:'notice-region', children:regionChildren};
  const links = intervalElements(selected).filter(node => node.tagName === 'A' && node.hasAttribute('href') && fileAttachmentLink(node)).map(linkRow);
  const title = text(titleAnchor);
  return {status:'found', region:{structured_dom:structured, title, text:structuredText(structured), links, locator:{strategy:'site-neutral-playwright-anchor-contiguous-region/v1', common_parent:selected.parent.tagName.toLowerCase(), start_child_index:selected.startIndex, end_child_index:selected.endIndex, anchor_cap:ANCHOR_CAP, region_cap:REGION_CAP, generated_region_count:intervals.size, equivalent_region_count:equivalent.length, text_chars:selected.signal.textChars, block_count:selected.signal.blockCount, table_count:selected.signal.tableCount, link_count:selected.signal.linkCount, navigation_link_count:selected.signal.navigationLinkCount, iframe_count:intervalElements(selected).filter(node => node.tagName === 'IFRAME').length}}};
}
"""


def _capture_structured_region_with_playwright(
    target_url: str,
    *,
    timeout_seconds: int,
    render_budget_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    render_deadline = min(deadline, time.monotonic() + max(render_budget_seconds, 1.0))
    playwright = browser = context = page = None
    navigation_timed_out = False
    latest_results: list[dict[str, Any]] = []
    stable_digest = ""
    stable_since = 0.0
    ambiguous_digest = ""
    ambiguous_since = 0.0
    saw_region = False
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(
            accept_downloads=False,
            service_workers="block",
        )
        page = context.new_page()
        page.set_default_timeout(max(int(timeout_seconds * 1000), 1))
        try:
            page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=max(int(min(render_budget_seconds, timeout_seconds) * 1000), 1),
            )
        except PlaywrightTimeoutError:
            navigation_timed_out = True
        except PlaywrightError:
            latest_results = [{"status": "technical_error"}]

        while time.monotonic() < render_deadline:
            latest_results = _evaluate_playwright_frames(page)
            try:
                region = _select_unique_playwright_region(latest_results)
            except HeadlessBrowserCaptureError as exc:
                if exc.code == "notice_content_frame_ambiguous":
                    now = time.monotonic()
                    digest = hashlib.sha256(
                        json.dumps(
                            [
                                {
                                    "frame_identity": row.get("frame_identity"),
                                    "status": row.get("status"),
                                    "reason": row.get("reason"),
                                }
                                for row in latest_results
                            ],
                            sort_keys=True,
                        ).encode("utf-8")
                    ).hexdigest()
                    if digest == ambiguous_digest:
                        ambiguous_since = ambiguous_since or now
                    else:
                        ambiguous_digest = digest
                        ambiguous_since = 0.0
                    if ambiguous_since and now - ambiguous_since >= 0.75:
                        raise
                    region = None
                elif exc.code in {"notice_content_technical_error", "external_access_blocked"}:
                    raise
                else:
                    region = None
            else:
                ambiguous_digest = ""
                ambiguous_since = 0.0
            if region is not None:
                saw_region = True
                frame_identity = next(
                    (
                        row.get("frame_identity")
                        for row in latest_results
                        if row.get("status") == "found" and row.get("region") is region
                    ),
                    None,
                )
                digest = hashlib.sha256(
                    json.dumps(
                        {"frame_identity": frame_identity, "region": region},
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                now = time.monotonic()
                if digest == stable_digest:
                    stable_since = stable_since or now
                else:
                    stable_digest = digest
                    stable_since = 0.0
                if stable_since and now - stable_since >= 0.75:
                    return region
            else:
                stable_digest = ""
                stable_since = 0.0
            page.wait_for_timeout(100)

        region = _select_unique_playwright_region(latest_results)
        if region is not None or saw_region:
            raise HeadlessBrowserCaptureError(
                "browser_timeout",
                "本机 Google Chrome 未能在限定时间内形成稳定公告内容区域。",
            )
        if navigation_timed_out or time.monotonic() >= deadline:
            raise HeadlessBrowserCaptureError(
                "browser_timeout", "本机 Google Chrome 获取渲染正文超时。"
            )
        raise HeadlessBrowserCaptureError(
            "notice_content_frame_not_found",
            "Rendered frames do not contain a unique reliable notice-content region.",
        )
    except HeadlessBrowserCaptureError:
        raise
    except PlaywrightTimeoutError as exc:
        raise HeadlessBrowserCaptureError(
            "browser_timeout", "本机 Google Chrome 获取渲染正文超时。"
        ) from exc
    except PlaywrightError as exc:
        raise HeadlessBrowserCaptureError(
            "browser_launch_failed", "本机 Google Chrome 未能启动或渲染公告页面。"
        ) from exc
    finally:
        for resource in (page, context, browser):
            if resource is not None:
                try:
                    resource.close()
                except PlaywrightError:
                    pass
        if playwright is not None:
            try:
                playwright.stop()
            except PlaywrightError:
                pass


def _evaluate_playwright_frames(page: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for frame_index, frame in enumerate(tuple(page.frames)):
        try:
            if frame.is_detached():
                continue
            payload = frame.evaluate(_PLAYWRIGHT_NOTICE_CANDIDATE_SCRIPT)
        except PlaywrightError:
            if frame.is_detached():
                continue
            results.append({"status": "frame_error"})
            continue
        if isinstance(payload, dict):
            results.append(
                {
                    **payload,
                    "frame_index": frame_index,
                    "frame_identity": id(frame),
                }
            )
    return results


def _normalized_region_title(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _region_titles_match(left: Any, right: Any) -> bool:
    left_normalized = _normalized_region_title(left)
    right_normalized = _normalized_region_title(right)
    if not left_normalized or not right_normalized:
        return False
    if left_normalized in right_normalized or right_normalized in left_normalized:
        return True
    return SequenceMatcher(None, left_normalized, right_normalized).ratio() >= 0.72


def _region_is_iframe_shell(region: dict[str, Any]) -> bool:
    locator = region.get("locator")
    if not isinstance(locator, dict) or int(locator.get("iframe_count") or 0) < 1:
        return False
    table_count = int(locator.get("table_count") or 0)
    link_count = int(locator.get("link_count") or 0)
    navigation_links = int(locator.get("navigation_link_count") or 0)
    block_count = int(locator.get("block_count") or 0)
    return table_count == 0 and (
        navigation_links >= 3 or link_count >= max(3, block_count * 2)
    )


def _select_unique_playwright_region(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not results:
        return None
    main = next(
        (row for row in results if int(row.get("frame_index", -1)) == 0),
        results[0],
    )
    main_status = str(main.get("status") or "")
    if main_status == "access_blocked":
        raise HeadlessBrowserCaptureError(
            "external_access_blocked",
            "The public notice page denied automated access.",
        )
    if main_status == "ambiguous":
        raise HeadlessBrowserCaptureError(
            "notice_content_frame_ambiguous",
            "Rendered frames contain multiple reliable notice-content regions.",
        )
    if main_status in {"technical_error", "frame_error"}:
        raise HeadlessBrowserCaptureError(
            "notice_content_technical_error",
            "Rendered frames contain a browser technical error instead of notice content.",
        )
    if main_status == "found":
        region = main.get("region")
        if isinstance(region, dict):
            if _region_is_iframe_shell(region):
                matching_children = [
                    row.get("region") for row in results if row is not main
                    and row.get("status") == "found"
                    and isinstance(row.get("region"), dict)
                    and _region_titles_match(region.get("title"), row["region"].get("title"))
                ]
                if len(matching_children) > 1:
                    raise HeadlessBrowserCaptureError(
                        "notice_content_frame_ambiguous",
                        "Rendered frames contain multiple reliable notice-content regions.",
                    )
                if matching_children:
                    return matching_children[0]
            return region
        raise HeadlessBrowserCaptureError(
            "notice_content_technical_error",
            "Rendered frames contain a browser technical error instead of notice content.",
        )

    fallback = [row for row in results if row is not main]
    if any(str(row.get("status") or "") == "ambiguous" for row in fallback):
        raise HeadlessBrowserCaptureError(
            "notice_content_frame_ambiguous",
            "Rendered frames contain multiple reliable notice-content regions.",
        )
    regions = [row.get("region") for row in fallback if row.get("status") == "found"]
    regions = [row for row in regions if isinstance(row, dict)]
    if len(regions) > 1:
        raise HeadlessBrowserCaptureError(
            "notice_content_frame_ambiguous",
            "Rendered frames contain multiple reliable notice-content regions.",
        )
    if regions:
        return regions[0]
    if any(row.get("status") == "access_blocked" for row in fallback):
        raise HeadlessBrowserCaptureError(
            "external_access_blocked",
            "The public notice page denied automated access.",
        )
    if any(row.get("status") == "technical_error" for row in fallback):
        raise HeadlessBrowserCaptureError(
            "notice_content_technical_error",
            "Rendered frames contain a browser technical error instead of notice content.",
        )
    return None


def _invalid_url_error() -> HeadlessBrowserCaptureError:
    return HeadlessBrowserCaptureError(
        "invalid_url",
        "该 URL 不允许使用本机浏览器获取。",
    )


def _automatic_url_error(code: str, message: str) -> HeadlessBrowserCaptureError:
    return HeadlessBrowserCaptureError(code, message)
