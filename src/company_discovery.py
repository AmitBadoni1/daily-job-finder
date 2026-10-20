from __future__ import annotations

import html
import ipaddress
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from connectors import ConnectorError, JobPosting, SUPPORTED_CONNECTORS, USER_AGENT, fetch_jobs


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConnectorCandidate:
    connector: str
    connector_key: str
    source_url: str


@dataclass(frozen=True)
class DiscoveryResult:
    connector: str
    connector_key: str
    careers_url: str
    status: str
    note: str
    postings: tuple[JobPosting, ...] = ()


def _normalize_markup(value: str) -> str:
    return (
        html.unescape(value)
        .replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
    )


def _safe_http_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise DiscoveryError("Careers URL is missing")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DiscoveryError("Careers URL must be an http(s) URL")
    if parsed.hostname.lower() == "localhost":
        raise DiscoveryError("Localhost URLs are not allowed")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise DiscoveryError("Private or local network URLs are not allowed")
    return urllib.parse.urlunparse(parsed)


def fetch_careers_page(url: str, *, timeout: int = 30) -> tuple[str, str]:
    safe_url = _safe_http_url(url)
    request = urllib.request.Request(
        safe_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(5_000_001)
            if len(raw) > 5_000_000:
                raise DiscoveryError("Careers page is larger than the 5 MB discovery limit")
            charset = response.headers.get_content_charset() or "utf-8"
            return response.geturl(), raw.decode(charset, errors="replace")
    except (urllib.error.URLError, TimeoutError, UnicodeError) as exc:
        raise DiscoveryError(f"Could not load careers page: {exc}") from exc


def _workday_base_url(value: str) -> str | None:
    parsed = urllib.parse.urlparse(value)
    if not parsed.hostname or not parsed.hostname.lower().endswith(".myworkdayjobs.com"):
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if segments and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segments[0]):
        segments.pop(0)
    if not segments:
        return None
    return f"{parsed.scheme or 'https'}://{parsed.hostname}/{segments[0]}"


def extract_connector_candidates(text: str) -> list[ConnectorCandidate]:
    normalized = _normalize_markup(text)
    candidates: list[ConnectorCandidate] = []

    patterns = [
        (
            "greenhouse",
            r"https?://(?:job-boards|boards)\.greenhouse\.io/([A-Za-z0-9_-]+)",
        ),
        (
            "greenhouse",
            r"https?://boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)",
        ),
        (
            "greenhouse",
            r"greenhouse\.io/(?:embed/)?job_board\?for=([A-Za-z0-9_-]+)",
        ),
        ("ashby", r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)"),
        (
            "ashby",
            r"https?://api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_-]+)",
        ),
        ("lever", r"https?://jobs\.lever\.co/([A-Za-z0-9_-]+)"),
        (
            "lever",
            r"https?://api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)",
        ),
    ]
    for connector, pattern in patterns:
        for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
            candidates.append(
                ConnectorCandidate(connector, match.group(1), match.group(0))
            )

    workday_pattern = r"https?://[A-Za-z0-9.-]+\.myworkdayjobs\.com/[^\s\"'<>]+"
    for match in re.finditer(workday_pattern, normalized, flags=re.IGNORECASE):
        source_url = match.group(0).rstrip(".,);]")
        base_url = _workday_base_url(source_url)
        if base_url:
            candidates.append(ConnectorCandidate("workday", base_url, source_url))

    deduplicated: list[ConnectorCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        identity = (candidate.connector.lower(), candidate.connector_key.lower())
        if identity not in seen:
            seen.add(identity)
            deduplicated.append(candidate)
    return deduplicated


def discover_connector(careers_url: str) -> DiscoveryResult:
    final_url, page = fetch_careers_page(careers_url)
    candidates = extract_connector_candidates("\n".join([careers_url, final_url, page]))
    verified: list[tuple[ConnectorCandidate, list[JobPosting]]] = []
    verification_errors: list[str] = []

    for candidate in candidates:
        if candidate.connector not in SUPPORTED_CONNECTORS:
            continue
        try:
            postings = fetch_jobs(candidate.connector, candidate.connector_key)
            verified.append((candidate, postings))
        except ConnectorError as exc:
            verification_errors.append(
                f"{candidate.connector}:{candidate.connector_key} ({exc})"
            )

    if len(verified) == 1:
        candidate, postings = verified[0]
        return DiscoveryResult(
            connector=candidate.connector,
            connector_key=candidate.connector_key,
            careers_url=final_url,
            status="verified",
            note=f"Verified {candidate.connector} feed with {len(postings)} current jobs.",
            postings=tuple(postings),
        )
    if len(verified) > 1:
        choices = ", ".join(
            f"{candidate.connector}:{candidate.connector_key}" for candidate, _ in verified
        )
        return DiscoveryResult(
            connector="",
            connector_key="",
            careers_url=final_url,
            status="needs_review",
            note=f"Multiple working job boards found: {choices}",
        )

    unsupported = [candidate for candidate in candidates if candidate.connector not in SUPPORTED_CONNECTORS]
    if len(unsupported) == 1:
        candidate = unsupported[0]
        return DiscoveryResult(
            connector=candidate.connector,
            connector_key=candidate.connector_key,
            careers_url=final_url,
            status="adapter_pending",
            note=f"Detected {candidate.connector}; its fetch adapter is not implemented yet.",
        )
    if len(unsupported) > 1:
        choices = ", ".join(
            f"{candidate.connector}:{candidate.connector_key}" for candidate in unsupported
        )
        return DiscoveryResult(
            connector="",
            connector_key="",
            careers_url=final_url,
            status="needs_review",
            note=f"Multiple unsupported job boards found: {choices}",
        )

    error_suffix = ""
    if verification_errors:
        error_suffix = " Feed checks failed: " + "; ".join(verification_errors)
    return DiscoveryResult(
        connector="custom",
        connector_key=final_url,
        careers_url=final_url,
        status="needs_review",
        note="No supported ATS link was found on the careers page." + error_suffix,
    )
