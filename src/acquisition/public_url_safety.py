from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit


DNS_LABEL_PATTERN = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


class PublicUrlSafetyError(ValueError):
    """Stable, platform-neutral rejection for non-public web destinations."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_public_http_url(value: str) -> str:
    """Require an HTTP(S) URL whose every resolved address is globally routable."""
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise PublicUrlSafetyError("invalid_url", "URL 格式无效。") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise PublicUrlSafetyError("unsupported_scheme", "URL 仅支持 HTTP/HTTPS。")
    if not parsed.hostname:
        raise PublicUrlSafetyError("missing_hostname", "URL 缺少主机名。")
    if parsed.username is not None or parsed.password is not None:
        raise PublicUrlSafetyError("url_credentials", "URL 不允许包含凭证。")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PublicUrlSafetyError("invalid_port", "URL 端口无效。") from exc
    if port is not None and not 1 <= port <= 65_535:
        raise PublicUrlSafetyError("invalid_port", "URL 端口无效。")

    hostname = str(parsed.hostname or "").rstrip(".").lower()
    if not hostname or hostname == "localhost" or hostname.endswith(".localhost"):
        raise PublicUrlSafetyError("non_public_destination", "URL 必须指向公网地址。")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        _require_global(literal)
        return url

    ascii_hostname = _validated_hostname(hostname)
    try:
        answers = socket.getaddrinfo(
            ascii_hostname,
            port or (443 if parsed.scheme.lower() == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except (OSError, UnicodeError) as exc:
        raise PublicUrlSafetyError("dns_resolution_failed", "URL 的 DNS 解析失败。") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for answer in answers:
        try:
            addresses.append(ipaddress.ip_address(str(answer[4][0])))
        except (IndexError, TypeError, ValueError) as exc:
            raise PublicUrlSafetyError("dns_resolution_failed", "URL 的 DNS 解析结果无效。") from exc
    if not addresses:
        raise PublicUrlSafetyError("dns_resolution_failed", "URL 的 DNS 解析结果为空。")
    for address in addresses:
        _require_global(address)
    return url


def _validated_hostname(hostname: str) -> str:
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise PublicUrlSafetyError("invalid_hostname", "URL 主机名格式无效。") from exc
    if len(ascii_hostname) > 253:
        raise PublicUrlSafetyError("invalid_hostname", "URL 主机名格式无效。")
    labels = ascii_hostname.split(".")
    if not labels or any(not DNS_LABEL_PATTERN.fullmatch(label) for label in labels):
        raise PublicUrlSafetyError("invalid_hostname", "URL 主机名格式无效。")
    return ascii_hostname


def _require_global(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if not address.is_global:
        raise PublicUrlSafetyError("non_public_destination", "URL 必须仅解析到公网地址。")
