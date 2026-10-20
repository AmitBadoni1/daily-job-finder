from __future__ import annotations

import json
import math
import http.cookiejar
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


USER_AGENT = "DailyJobFinder/1.0 (+local job search)"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)
SUPPORTED_CONNECTORS = {
    "adobe",
    "apple",
    "ashby",
    "greenhouse",
    "lever",
    "meta",
    "microsoft",
    "salesforce",
    "smartrecruiters",
    "tesla",
}
TESLA_BOARD_HELPER = Path(__file__).with_name("tesla_browser_fetch.mjs")
META_BOARD_HELPER = Path(__file__).with_name("meta_browser_fetch.mjs")
BROWSER_DETAIL_HELPER = Path(__file__).with_name("browser_detail_fetch.mjs")
APPLE_SEARCH_URL = "https://jobs.apple.com/api/v1/search"
APPLE_PAGE_SIZE = 20
MICROSOFT_SEARCH_URL = "https://apply.careers.microsoft.com/api/pcsx/search"
MICROSOFT_PAGE_SIZE = 10
MICROSOFT_PAGE_DELAY_SECONDS = 2.0
ADOBE_SEARCH_URL = "https://careers.adobe.com/widgets"
ADOBE_PAGE_SIZE = 10
SALESFORCE_JOBS_URL = (
    "https://a.sfdcstatic.com/digital/xsf/careers/{environment}/jobs_1.json"
)
SMARTRECRUITERS_POSTINGS_URL = (
    "https://api.smartrecruiters.com/v1/companies/{company}/postings"
)
SMARTRECRUITERS_PAGE_SIZE = 100
_SALESFORCE_DETAIL_CACHE: dict[str, dict[str, dict[str, Any]]] = {}


class ConnectorError(RuntimeError):
    pass


class UnsupportedConnectorError(ConnectorError):
    pass


@dataclass(frozen=True)
class JobPosting:
    source_job_id: str
    title: str
    location: str
    job_url: str
    description: str = ""
    department: str = ""
    employment_type: str = ""
    location_type: str = ""
    published_at: str = ""
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class JobDetail:
    description: str
    employment_type: str = ""
    location_type: str = ""
    raw: dict[str, Any] | None = None


class _JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._capturing = False
        self._parts: list[str] = []
        self.documents: list[Any] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("type") == "application/ld+json":
            self._capturing = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or not self._capturing:
            return
        self._capturing = False
        try:
            self.documents.append(json.loads("".join(self._parts)))
        except json.JSONDecodeError:
            pass
        self._parts = []


def _get_json(
    url: str,
    *,
    retries: int = 3,
    timeout: int = 30,
    user_agent: str = USER_AGENT,
    opener: Any | None = None,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            open_url = opener.open if opener is not None else urllib.request.urlopen
            with open_url(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if attempt + 1 < retries:
                if exc.code == 429:
                    retry_after = exc.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 0.0
                    except ValueError:
                        delay = 0.0
                    time.sleep(max(delay, min(60.0, 5.0 * (2**attempt))))
                else:
                    time.sleep(2**attempt)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise ConnectorError(f"Failed to fetch {url}: {last_error}")


def _get_text(url: str, *, retries: int = 3, timeout: int = 30) -> str:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise ConnectorError(f"Failed to fetch {url}: {last_error}")


def _fetch_browser_detail(
    posting: JobPosting,
    selector: str,
    *,
    minimum_length: int,
    connector_name: str,
) -> JobDetail:
    node = shutil.which("node")
    if not node:
        raise ConnectorError(f"{connector_name} detail retrieval requires Node.js on PATH")
    if not BROWSER_DETAIL_HELPER.exists():
        raise ConnectorError(f"Browser detail helper not found: {BROWSER_DETAIL_HELPER}")
    if not posting.job_url:
        raise ConnectorError(f"{connector_name} job {posting.source_job_id} has no URL")
    try:
        result = subprocess.run(
            [
                node,
                str(BROWSER_DETAIL_HELPER),
                posting.job_url,
                selector,
                str(minimum_length),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConnectorError(
            f"{connector_name} detail fetch timed out after 120 seconds"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown browser-helper error"
        raise ConnectorError(f"{connector_name} detail fetch failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConnectorError(
            f"{connector_name} detail helper returned invalid JSON"
        ) from exc
    description = payload.get("description") if isinstance(payload, dict) else None
    if not isinstance(description, str) or len(description.strip()) < minimum_length:
        raise ConnectorError(
            f"{connector_name} job {posting.source_job_id} is missing its description"
        )
    return JobDetail(
        description=description.strip(),
        employment_type=posting.employment_type,
        location_type=posting.location_type,
        raw=payload,
    )


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    retries: int = 3,
    timeout: int = 30,
    opener: urllib.request.OpenerDirector | None = None,
) -> Any:
    last_error: Exception | None = None
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            open_request = opener.open if opener is not None else urllib.request.urlopen
            with open_request(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise ConnectorError(f"Failed to fetch {url}: {last_error}")


def fetch_greenhouse(board_token: str) -> list[JobPosting]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    payload = _get_json(url)
    postings: list[JobPosting] = []
    for job in payload.get("jobs", []):
        location = (job.get("location") or {}).get("name") or ""
        departments = job.get("departments") or []
        department = ", ".join(item.get("name", "") for item in departments if item.get("name"))
        postings.append(
            JobPosting(
                source_job_id=str(job["id"]),
                title=job.get("title") or "",
                location=location,
                job_url=job.get("absolute_url") or "",
                description=job.get("content") or "",
                department=department,
                published_at=job.get("updated_at") or "",
                raw=job,
            )
        )
    return postings


def fetch_ashby(board_name: str) -> list[JobPosting]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board_name}"
    payload = _get_json(url)
    postings: list[JobPosting] = []
    for job in payload.get("jobs", []):
        postings.append(
            JobPosting(
                source_job_id=str(job["id"]),
                title=job.get("title") or "",
                location=job.get("location") or "",
                job_url=job.get("jobUrl") or job.get("applyUrl") or "",
                description=job.get("descriptionPlain") or job.get("descriptionHtml") or "",
                department=job.get("department") or job.get("team") or "",
                employment_type=job.get("employmentType") or "",
                location_type=job.get("locationType") or "",
                published_at=job.get("publishedAt") or "",
                raw=job,
            )
        )
    return postings


def fetch_lever(site_name: str) -> list[JobPosting]:
    url = f"https://api.lever.co/v0/postings/{site_name}?mode=json"
    payload = _get_json(url)
    if not isinstance(payload, list):
        raise ConnectorError(f"Unexpected Lever response for {site_name}")
    postings: list[JobPosting] = []
    for job in payload:
        categories = job.get("categories") or {}
        description_parts = [
            job.get("descriptionPlain") or job.get("description") or "",
            job.get("additionalPlain") or job.get("additional") or "",
        ]
        postings.append(
            JobPosting(
                source_job_id=str(job["id"]),
                title=job.get("text") or "",
                location=categories.get("location") or "",
                job_url=job.get("hostedUrl") or job.get("applyUrl") or "",
                description="\n".join(part for part in description_parts if part),
                department=categories.get("department") or categories.get("team") or "",
                employment_type=categories.get("commitment") or "",
                location_type=categories.get("workplaceType") or "",
                published_at=str(job.get("createdAt") or ""),
                raw=job,
            )
        )
    return postings


def _fetch_smartrecruiters_page(
    company_identifier: str, offset: int
) -> tuple[int, list[dict]]:
    company = urllib.parse.quote(company_identifier, safe="")
    query = urllib.parse.urlencode(
        {
            "limit": SMARTRECRUITERS_PAGE_SIZE,
            "offset": offset,
            "destination": "PUBLIC",
        }
    )
    payload = _get_json(
        f"{SMARTRECRUITERS_POSTINGS_URL.format(company=company)}?{query}"
    )
    total = payload.get("totalFound") if isinstance(payload, dict) else None
    postings = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(total, int) or not isinstance(postings, list):
        raise ConnectorError(
            f"SmartRecruiters returned an unexpected response for {company_identifier}"
        )
    return total, postings


def fetch_smartrecruiters(company_identifier: str) -> list[JobPosting]:
    company_identifier = company_identifier.strip()
    if not company_identifier:
        raise ConnectorError("SmartRecruiters connector requires a company identifier")

    total, first_page = _fetch_smartrecruiters_page(company_identifier, 0)
    jobs = list(first_page)
    offsets = list(
        range(SMARTRECRUITERS_PAGE_SIZE, total, SMARTRECRUITERS_PAGE_SIZE)
    )
    if offsets:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = executor.map(
                lambda offset: _fetch_smartrecruiters_page(
                    company_identifier, offset
                ),
                offsets,
            )
            for offset, (page_total, page_jobs) in zip(offsets, results):
                if not page_jobs and offset < page_total:
                    raise ConnectorError(
                        f"SmartRecruiters returned an empty page at offset {offset}"
                    )
                jobs.extend(page_jobs)

    postings_by_id: dict[str, JobPosting] = {}
    for job in jobs:
        job_id = job.get("id")
        if job_id in (None, ""):
            raise ConnectorError("SmartRecruiters contains a listing without an ID")
        job_id = str(job_id)
        location_data = job.get("location") or {}
        location = location_data.get("fullLocation") or ", ".join(
            str(value)
            for value in (
                location_data.get("city"),
                location_data.get("region"),
                location_data.get("country"),
            )
            if value
        )
        if location_data.get("hybrid") is True:
            location_type = "Hybrid"
        elif location_data.get("remote") is True:
            location_type = "Remote"
        else:
            location_type = ""
        department = (job.get("department") or {}).get("label") or (
            job.get("function") or {}
        ).get("label", "")
        postings_by_id[job_id] = JobPosting(
            source_job_id=job_id,
            title=job.get("name") or "",
            location=location,
            job_url=(
                f"https://jobs.smartrecruiters.com/"
                f"{urllib.parse.quote(company_identifier, safe='')}/{job_id}"
            ),
            department=department,
            employment_type=(job.get("typeOfEmployment") or {}).get("label") or "",
            location_type=location_type,
            published_at=job.get("releasedDate") or "",
            raw=job,
        )
    return list(postings_by_id.values())


def fetch_smartrecruiters_detail(
    company_identifier: str, posting: JobPosting
) -> JobDetail:
    company = urllib.parse.quote(company_identifier.strip(), safe="")
    if not company:
        raise ConnectorError("SmartRecruiters connector requires a company identifier")
    job_id = urllib.parse.quote(posting.source_job_id, safe="")
    payload = _get_json(
        f"{SMARTRECRUITERS_POSTINGS_URL.format(company=company)}/{job_id}"
    )
    sections = ((payload.get("jobAd") or {}).get("sections") or {}) if isinstance(payload, dict) else {}
    parts: list[str] = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        section = sections.get(key) if isinstance(sections, dict) else None
        if not isinstance(section, dict):
            continue
        text = section.get("text")
        if isinstance(text, str) and text.strip():
            title = section.get("title") or key
            parts.append(f"{title}\n{text.strip()}")
    description = "\n\n".join(parts)
    if not description:
        raise ConnectorError(
            f"SmartRecruiters job {posting.source_job_id} is missing its description"
        )
    employment = (payload.get("typeOfEmployment") or {}) if isinstance(payload, dict) else {}
    location = (payload.get("location") or {}) if isinstance(payload, dict) else {}
    if location.get("hybrid") is True:
        location_type = "Hybrid"
    elif location.get("remote") is True:
        location_type = "Remote"
    else:
        location_type = posting.location_type
    return JobDetail(
        description=description,
        employment_type=employment.get("label") or posting.employment_type,
        location_type=location_type,
        raw=payload,
    )


def _fetch_apple_page(location_ids: list[str], page: int) -> tuple[int, list[dict]]:
    for attempt in range(5):
        payload = _post_json(
            APPLE_SEARCH_URL,
            {
                "query": "",
                "filters": {"locations": location_ids},
                "page": page,
                "locale": "en-us",
                "sort": "",
                "format": {
                    "longDate": "MMMM D, YYYY",
                    "mediumDate": "MMM D, YYYY",
                },
            },
        )
        response = payload.get("res") if isinstance(payload, dict) else None
        if not isinstance(response, dict):
            raise ConnectorError(f"Apple search page {page} is missing response data")
        listings = response.get("searchResults")
        total_records = response.get("totalRecords")
        if not isinstance(listings, list) or not isinstance(total_records, int):
            raise ConnectorError(f"Apple search page {page} has an unexpected response")
        if total_records or listings:
            return total_records, listings
        if attempt < 4:
            time.sleep(0.5 * (attempt + 1))
    return 0, []


def fetch_apple(connector_key: str) -> list[JobPosting]:
    location_ids = [item.strip() for item in connector_key.split(",") if item.strip()]
    if not location_ids:
        raise ConnectorError("Apple connector requires at least one location ID")

    total_records, first_page = _fetch_apple_page(location_ids, 1)
    if not first_page and total_records == 0:
        raise ConnectorError("Apple returned an empty search response after retries")
    page_count = math.ceil(total_records / APPLE_PAGE_SIZE)
    pages: list[list[dict]] = [first_page]
    highest_total = total_records
    if page_count > 1:
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = executor.map(
                lambda page: _fetch_apple_page(location_ids, page),
                range(2, page_count + 1),
            )
            for page_number, (page_total, listings) in enumerate(results, start=2):
                highest_total = max(highest_total, page_total)
                if not listings and (
                    page_total == 0
                    or page_number <= math.ceil(page_total / APPLE_PAGE_SIZE)
                ):
                    raise ConnectorError(
                        f"Apple search page {page_number} was unexpectedly empty"
                    )
                pages.append(listings)

    # Apple updates the total while the board is being paginated. If it grew enough
    # to add pages, collect those too; a later daily run will reconcile any listing
    # that moved between pages during this snapshot.
    while len(pages) < math.ceil(highest_total / APPLE_PAGE_SIZE):
        page_number = len(pages) + 1
        page_total, listings = _fetch_apple_page(location_ids, page_number)
        highest_total = max(highest_total, page_total)
        if not listings and (
            page_total == 0
            or page_number <= math.ceil(page_total / APPLE_PAGE_SIZE)
        ):
            raise ConnectorError(f"Apple search page {page_number} was unexpectedly empty")
        pages.append(listings)

    postings_by_id: dict[str, JobPosting] = {}
    for job in (job for page in pages for job in page):
        job_id = job.get("id") or job.get("reqId") or job.get("positionId")
        if job_id in (None, ""):
            raise ConnectorError("Apple search contains a listing without an ID")
        job_id = str(job_id)
        position_id = str(job.get("positionId") or job_id).removeprefix("PIPE-")
        slug = str(job.get("transformedPostingTitle") or "").strip("/")
        job_url = f"https://jobs.apple.com/en-us/details/{position_id}"
        if slug:
            job_url = f"{job_url}/{slug}"
        locations = job.get("locations") or []
        location = "; ".join(
            item.get("name")
            or ", ".join(
                part
                for part in (
                    item.get("city"),
                    item.get("stateProvince"),
                    item.get("countryName"),
                )
                if part
            )
            for item in locations
            if isinstance(item, dict)
        )
        raw = {key: value for key, value in job.items() if key != "jobSummary"}
        postings_by_id[job_id] = JobPosting(
            source_job_id=job_id,
            title=job.get("postingTitle") or "",
            location=location,
            job_url=job_url,
            department=(job.get("team") or {}).get("teamName") or "",
            location_type="Remote" if job.get("homeOffice") is True else "",
            published_at=job.get("postDateInGMT") or job.get("postingDate") or "",
            raw=raw,
        )
    return list(postings_by_id.values())


def fetch_apple_detail(posting: JobPosting) -> JobDetail:
    return _fetch_browser_detail(
        posting,
        "#jobdetails-wrapper",
        minimum_length=500,
        connector_name="Apple",
    )


def _fetch_microsoft_page(
    location: str,
    start: int,
    *,
    opener: Any | None = None,
) -> tuple[int, list[dict]]:
    query = urllib.parse.urlencode(
        {
            "domain": "microsoft.com",
            "query": "",
            "location": location,
            "start": start,
        }
    )
    payload = _get_json(
        f"{MICROSOFT_SEARCH_URL}?{query}",
        retries=6,
        user_agent=BROWSER_USER_AGENT,
        opener=opener,
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("status") != 200
        or not isinstance(data, dict)
    ):
        raise ConnectorError("Microsoft careers search returned an unexpected response")
    count = data.get("count")
    positions = data.get("positions")
    if not isinstance(count, int) or not isinstance(positions, list):
        raise ConnectorError("Microsoft careers search is missing jobs or count data")
    return count, positions


def _fetch_microsoft_location(
    location: str,
    *,
    opener: Any | None = None,
) -> list[dict]:
    """Fetch one location slowly and reject an internally inconsistent snapshot."""
    last_error: ConnectorError | None = None
    collected_by_id: dict[str, dict] = {}
    stable_count: int | None = None
    for snapshot_attempt in range(3):
        try:
            expected_count, jobs = _fetch_microsoft_page(
                location, 0, opener=opener
            )
            if stable_count != expected_count:
                collected_by_id = {}
                stable_count = expected_count
            jobs = list(jobs)
            for start in range(MICROSOFT_PAGE_SIZE, expected_count, MICROSOFT_PAGE_SIZE):
                time.sleep(MICROSOFT_PAGE_DELAY_SECONDS)
                page_count, positions = _fetch_microsoft_page(
                    location, start, opener=opener
                )
                if page_count != expected_count:
                    raise ConnectorError(
                        f"Microsoft search count changed for {location}: "
                        f"{expected_count} to {page_count}"
                    )
                if not positions:
                    raise ConnectorError(
                        f"Microsoft search for {location} returned an empty page at {start}"
                    )
                jobs.extend(positions)
            if len(jobs) != expected_count:
                raise ConnectorError(
                    f"Microsoft search for {location} returned {len(jobs)} jobs "
                    f"but reported {expected_count}"
                )
            snapshot_by_id = {
                str(job.get("id") or job.get("displayJobId") or job.get("atsJobId")): job
                for job in jobs
                if job.get("id") not in (None, "")
                or job.get("displayJobId") not in (None, "")
                or job.get("atsJobId") not in (None, "")
            }
            collected_by_id.update(snapshot_by_id)
            if len(collected_by_id) < expected_count:
                raise ConnectorError(
                    f"Microsoft search for {location} returned {len(collected_by_id)} "
                    f"unique records but reported {expected_count}"
                )
            if len(snapshot_by_id) == expected_count:
                return list(snapshot_by_id.values())
            missing_count = expected_count - len(snapshot_by_id)
            additions = [
                job
                for record_id, job in collected_by_id.items()
                if record_id not in snapshot_by_id
            ][:missing_count]
            return list(snapshot_by_id.values()) + additions
        except ConnectorError as exc:
            last_error = exc
            if snapshot_attempt < 2:
                time.sleep(5)
    raise last_error or ConnectorError(f"Microsoft search failed for {location}")


def fetch_microsoft(connector_key: str) -> list[JobPosting]:
    search_locations = [
        item.strip() for item in connector_key.split("|") if item.strip()
    ]
    if not search_locations:
        raise ConnectorError("Microsoft connector requires one or more location searches")

    jobs: list[dict] = []
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    for location in search_locations:
        jobs.extend(_fetch_microsoft_location(location, opener=opener))

    postings_by_id: dict[str, JobPosting] = {}
    for job in jobs:
        job_id = job.get("displayJobId") or job.get("atsJobId") or job.get("id")
        if job_id in (None, ""):
            raise ConnectorError("Microsoft search contains a listing without an ID")
        job_locations = job.get("locations") or job.get("standardizedLocations") or []
        internal_id = job.get("id")
        position_path = job.get("positionUrl") or (
            f"/careers/job/{internal_id}" if internal_id not in (None, "") else ""
        )
        postings_by_id[str(job_id)] = JobPosting(
            source_job_id=str(job_id),
            title=job.get("name") or "",
            location="; ".join(str(item) for item in job_locations if item),
            job_url=(
                urllib.parse.urljoin("https://apply.careers.microsoft.com", position_path)
                if position_path
                else ""
            ),
            department=job.get("department") or "",
            location_type=job.get("workLocationOption") or job.get("locationFlexibility") or "",
            published_at=str(job.get("postedTs") or job.get("creationTs") or ""),
            raw=job,
        )
    return list(postings_by_id.values())


def _find_json_ld_job_posting(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        item_type = value.get("@type")
        types = item_type if isinstance(item_type, list) else [item_type]
        if "JobPosting" in types:
            return value
        for child in value.values():
            found = _find_json_ld_job_posting(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_json_ld_job_posting(child)
            if found is not None:
                return found
    return None


def fetch_microsoft_detail(posting: JobPosting) -> JobDetail:
    if not posting.job_url:
        raise ConnectorError(f"Microsoft job {posting.source_job_id} has no URL")
    parser = _JsonLdParser()
    parser.feed(_get_text(posting.job_url))
    job = _find_json_ld_job_posting(parser.documents)
    description = job.get("description") if job else None
    if not isinstance(description, str) or not description.strip():
        raise ConnectorError(
            f"Microsoft job {posting.source_job_id} is missing its description"
        )
    return JobDetail(
        description=description.strip(),
        employment_type=(job.get("employmentType") or posting.employment_type),
        location_type=posting.location_type,
        raw=job,
    )


def fetch_meta() -> list[JobPosting]:
    node = shutil.which("node")
    if not node:
        raise ConnectorError("Meta connector requires Node.js on PATH")
    if not META_BOARD_HELPER.exists():
        raise ConnectorError(f"Meta browser helper not found: {META_BOARD_HELPER}")

    try:
        result = subprocess.run(
            [node, str(META_BOARD_HELPER)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConnectorError("Meta board fetch timed out after 120 seconds") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown browser-helper error"
        raise ConnectorError(f"Meta board fetch failed: {detail}")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConnectorError("Meta browser helper returned invalid JSON") from exc
    listings = payload.get("listings") if isinstance(payload, dict) else None
    expected_count = payload.get("job_count") if isinstance(payload, dict) else None
    if not isinstance(listings, list) or not isinstance(expected_count, int):
        raise ConnectorError("Meta board response is missing listings or count data")
    if len(listings) != expected_count:
        raise ConnectorError(
            f"Meta board returned {len(listings)} jobs but reported {expected_count}"
        )

    postings: list[JobPosting] = []
    for job in listings:
        job_id = job.get("id")
        if job_id in (None, ""):
            raise ConnectorError("Meta board contains a listing without an ID")
        locations = [str(item) for item in (job.get("locations") or []) if item]
        postings.append(
            JobPosting(
                source_job_id=str(job_id),
                title=job.get("title") or "",
                location="; ".join(locations),
                job_url=f"https://www.metacareers.com/profile/job_details/{job_id}/",
                department=", ".join(job.get("teams") or []),
                location_type=(
                    "Remote"
                    if locations and all("remote" in item.lower() for item in locations)
                    else ""
                ),
                raw=job,
            )
        )
    return postings


def fetch_meta_detail(posting: JobPosting) -> JobDetail:
    return _fetch_browser_detail(
        posting,
        "body",
        minimum_length=2000,
        connector_name="Meta",
    )


def _fetch_adobe_page(
    offset: int,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, list[dict]]:
    payload = _post_json(
        ADOBE_SEARCH_URL,
        {
            "lang": "en_us",
            "deviceType": "desktop",
            "country": "us",
            "pageName": "search-results",
            "ddoKey": "refineSearch",
            "sortBy": "Most recent",
            "subsearch": "",
            "from": offset,
            "irs": False,
            "jobs": True,
            "counts": True,
            "all_fields": [
                "remote",
                "country",
                "state",
                "city",
                "experienceLevel",
                "category",
                "profession",
                "employmentType",
                "jobLevel",
            ],
            "size": ADOBE_PAGE_SIZE,
            "clearAll": False,
            "jdsource": "facets",
            "isSliderEnable": False,
            "pageId": "page15-ds",
            "siteType": "external",
            "keywords": "",
            "global": True,
            "selected_fields": {},
            "sort": {"order": "desc", "field": "postedDate"},
            "locationData": {},
        },
        opener=opener,
    )
    search = payload.get("refineSearch") if isinstance(payload, dict) else None
    total = search.get("totalHits") if isinstance(search, dict) else None
    jobs = (search.get("data") or {}).get("jobs") if isinstance(search, dict) else None
    if (
        not isinstance(search, dict)
        or search.get("status") != 200
        or not isinstance(total, int)
        or not isinstance(jobs, list)
    ):
        raise ConnectorError(f"Adobe search page at offset {offset} is invalid")
    return total, jobs


def fetch_adobe(_connector_key: str) -> list[JobPosting]:
    jobs: list[dict] = []
    total = 0
    for snapshot_attempt in range(3):
        cookie_jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar)
        )
        total, first_page = _fetch_adobe_page(0, opener)
        jobs = list(first_page)
        offsets = list(range(ADOBE_PAGE_SIZE, total, ADOBE_PAGE_SIZE))
        page_totals = [total]
        for offset in offsets:
            page_total, page_jobs = _fetch_adobe_page(offset, opener)
            if not page_jobs and offset < page_total:
                raise ConnectorError(
                    f"Adobe search returned an empty page at offset {offset}"
                )
            page_totals.append(page_total)
            jobs.extend(page_jobs)

        unique_ids = {
            str(job.get("reqId") or job.get("jobId"))
            for job in jobs
            if job.get("reqId") not in (None, "") or job.get("jobId") not in (None, "")
        }
        if all(page_total == total for page_total in page_totals) and len(unique_ids) == total:
            break
        if snapshot_attempt < 2:
            time.sleep(2**snapshot_attempt)
    else:
        raise ConnectorError(
            f"Adobe search returned {len(unique_ids)} unique jobs but reported {total}"
        )

    postings_by_id: dict[str, JobPosting] = {}
    for job in jobs:
        job_id = job.get("reqId") or job.get("jobId")
        if job_id in (None, ""):
            raise ConnectorError("Adobe search contains a listing without an ID")
        locations = job.get("multi_location") or []
        location = "; ".join(str(item) for item in locations if item)
        if not location:
            location = job.get("location") or job.get("cityStateCountry") or ""
        categories = job.get("multi_category") or []
        raw = {
            key: value
            for key, value in job.items()
            if key not in {"descriptionTeaser", "ml_job_parser", "ml_skills"}
        }
        postings_by_id[str(job_id)] = JobPosting(
            source_job_id=str(job_id),
            title=job.get("title") or "",
            location=location,
            job_url=f"https://careers.adobe.com/us/en/job/{job_id}/",
            department=", ".join(categories) or job.get("category") or "",
            employment_type=job.get("type") or "",
            location_type="Remote" if job.get("remote") is True else "",
            published_at=job.get("postedDate") or job.get("dateCreated") or "",
            raw=raw,
        )
    return list(postings_by_id.values())


def fetch_adobe_detail(posting: JobPosting) -> JobDetail:
    return _fetch_browser_detail(
        posting,
        ".job-desc",
        minimum_length=1200,
        connector_name="Adobe",
    )


def _fetch_salesforce_feed(environment: str) -> list[dict[str, Any]]:
    payload = _get_json(SALESFORCE_JOBS_URL.format(environment=environment))
    jobs = payload.get("Report_Entry") if isinstance(payload, dict) else None
    expected_count = payload.get("Count") if isinstance(payload, dict) else None
    if not isinstance(jobs, list) or not isinstance(expected_count, int):
        raise ConnectorError("Salesforce job feed is missing listings or count data")
    if len(jobs) != expected_count:
        raise ConnectorError(
            f"Salesforce feed returned {len(jobs)} jobs but reported {expected_count}"
        )
    cache: dict[str, dict[str, Any]] = {}
    for job in jobs:
        job_id = job.get("Job_Requisition_Ref_ID")
        if job_id in (None, ""):
            raise ConnectorError("Salesforce feed contains a listing without an ID")
        cache[str(job_id)] = job
    _SALESFORCE_DETAIL_CACHE[environment] = cache
    return jobs


def fetch_salesforce(connector_key: str) -> list[JobPosting]:
    environment = connector_key.strip().lower()
    if environment not in {"prod", "qa", "uat"}:
        raise ConnectorError("Salesforce connector key must be prod, qa, or uat")
    jobs = _fetch_salesforce_feed(environment)

    postings: list[JobPosting] = []
    for job in jobs:
        job_id = job.get("Job_Requisition_Ref_ID")
        location_parts = [
            job.get("Job_Requisition_Primary_Location") or "",
            job.get("Job_Requisition_Additional_Locations") or "",
        ]
        location = "; ".join(part for part in location_parts if part)
        raw = {
            key: value
            for key, value in job.items()
            if key != "Job_Description"
            and not key.startswith("Pay_Transparency")
            and not key.startswith("US_Compensation")
        }
        postings.append(
            JobPosting(
                source_job_id=str(job_id),
                title=job.get("Job_Posting_Title") or "",
                location=location,
                job_url=f"https://careers.salesforce.com/en/jobs/{str(job_id).lower()}/",
                department=job.get("Job_Family_Group") or "",
                employment_type=job.get("Time_Type") or job.get("Employee_Type") or "",
                location_type="Remote" if location and "remote" in location.lower() else "",
                published_at=job.get("External_Job_Posting_Start_Date") or "",
                raw=raw,
            )
        )
    return postings


def _salesforce_workday_api_url(external_url: str) -> str:
    parsed = urllib.parse.urlparse(external_url)
    hostname = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or not hostname.endswith(".myworkdayjobs.com")
        or len(path_parts) < 3
        or path_parts[1] != "job"
    ):
        raise ConnectorError("Salesforce listing has no usable Workday detail URL")
    tenant = hostname.split(".", 1)[0]
    api_path = f"/wday/cxs/{tenant}/{parsed.path.lstrip('/')}"
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, api_path, "", "", "")
    )


def fetch_salesforce_detail(connector_key: str, posting: JobPosting) -> JobDetail:
    """Return one Salesforce description while keeping it out of baseline rows."""
    environment = connector_key.strip().lower()
    if environment not in {"prod", "qa", "uat"}:
        raise ConnectorError("Salesforce connector key must be prod, qa, or uat")
    cached_jobs = _SALESFORCE_DETAIL_CACHE.get(environment)
    if cached_jobs is None or posting.source_job_id not in cached_jobs:
        _fetch_salesforce_feed(environment)
        cached_jobs = _SALESFORCE_DETAIL_CACHE[environment]
    job = cached_jobs.get(posting.source_job_id)
    if job is None:
        raise ConnectorError(
            f"Salesforce job {posting.source_job_id} was not found in the current feed"
        )
    description = job.get("Job_Description")
    detail_raw: dict[str, Any] = job
    if not isinstance(description, str) or not description.strip():
        external_url = job.get("External_Job_Posting_Site")
        if not isinstance(external_url, str) or not external_url.strip():
            external_url = (posting.raw or {}).get("External_Job_Posting_Site")
        workday_url = _salesforce_workday_api_url(str(external_url or ""))
        workday_payload = _get_json(
            workday_url,
            retries=4,
            user_agent=BROWSER_USER_AGENT,
        )
        posting_info = (
            workday_payload.get("jobPostingInfo")
            if isinstance(workday_payload, dict)
            else None
        )
        description = (
            posting_info.get("jobDescription")
            if isinstance(posting_info, dict)
            else None
        )
        if isinstance(workday_payload, dict):
            detail_raw = workday_payload
    if not isinstance(description, str) or not description.strip():
        raise ConnectorError(
            f"Salesforce job {posting.source_job_id} is missing its description"
        )
    return JobDetail(
        description=description.strip(),
        employment_type=(
            job.get("Time_Type") or job.get("Employee_Type") or posting.employment_type
        ),
        location_type=posting.location_type,
        raw=detail_raw,
    )


def fetch_tesla() -> list[JobPosting]:
    """Fetch Tesla's board and convert it to the common posting format."""
    node = shutil.which("node")
    if not node:
        raise ConnectorError("Tesla connector requires Node.js on PATH")
    if not TESLA_BOARD_HELPER.exists():
        raise ConnectorError(f"Tesla browser helper not found: {TESLA_BOARD_HELPER}")

    try:
        result = subprocess.run(
            [node, str(TESLA_BOARD_HELPER), "board"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConnectorError("Tesla board fetch timed out after 120 seconds") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown browser-helper error"
        raise ConnectorError(f"Tesla board fetch failed: {detail}")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConnectorError("Tesla board helper returned invalid JSON") from exc

    listings = payload.get("listings") if isinstance(payload, dict) else None
    lookup = payload.get("lookup") if isinstance(payload, dict) else None
    if not isinstance(listings, list) or not isinstance(lookup, dict):
        raise ConnectorError("Tesla board response is missing listings or lookup data")

    locations = lookup.get("locations") or {}
    departments = lookup.get("departments") or {}
    employment_types = lookup.get("types") or {}
    postings: list[JobPosting] = []
    for listing in listings:
        job_id = listing.get("id")
        if job_id in (None, ""):
            raise ConnectorError("Tesla board contains a listing without an ID")
        job_id = str(job_id)
        postings.append(
            JobPosting(
                source_job_id=job_id,
                title=listing.get("t") or "",
                location=locations.get(str(listing.get("l")), ""),
                job_url=f"https://www.tesla.com/careers/search/job/{job_id}",
                department=departments.get(str(listing.get("dp")), ""),
                employment_type=employment_types.get(str(listing.get("y")), ""),
                raw=listing,
            )
        )
    return postings


def fetch_tesla_detail(posting: JobPosting) -> JobDetail:
    """Fetch and combine Tesla's structured detail sections for one job."""
    node = shutil.which("node")
    if not node:
        raise ConnectorError("Tesla connector requires Node.js on PATH")
    if not TESLA_BOARD_HELPER.exists():
        raise ConnectorError(f"Tesla browser helper not found: {TESLA_BOARD_HELPER}")

    try:
        result = subprocess.run(
            [node, str(TESLA_BOARD_HELPER), "detail", posting.source_job_id],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConnectorError("Tesla detail fetch timed out after 120 seconds") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown browser-helper error"
        raise ConnectorError(f"Tesla detail fetch failed: {detail}")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConnectorError("Tesla detail helper returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ConnectorError("Tesla detail response has an unexpected format")

    sections = []
    for label, key in (
        ("Description", "description"),
        ("Job description", "jobDescription"),
        ("Responsibilities", "jobResponsibilities"),
        ("Requirements", "jobRequirements"),
        ("Compensation and benefits", "jobCompensationAndBenefits"),
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            sections.append(f"{label}\n{value.strip()}")
    description = "\n\n".join(sections)
    if not description:
        raise ConnectorError(
            f"Tesla job {posting.source_job_id} is missing its description"
        )

    return JobDetail(
        description=description,
        employment_type=payload.get("timeType") or posting.employment_type,
        location_type=posting.location_type,
        raw=payload,
    )


def fetch_jobs(connector: str, connector_key: str) -> list[JobPosting]:
    normalized = connector.strip().lower()
    if normalized == "adobe":
        return fetch_adobe(connector_key)
    if normalized == "greenhouse":
        return fetch_greenhouse(connector_key)
    if normalized == "ashby":
        return fetch_ashby(connector_key)
    if normalized == "lever":
        return fetch_lever(connector_key)
    if normalized == "tesla":
        return fetch_tesla()
    if normalized == "apple":
        return fetch_apple(connector_key)
    if normalized == "meta":
        return fetch_meta()
    if normalized == "microsoft":
        return fetch_microsoft(connector_key)
    if normalized == "salesforce":
        return fetch_salesforce(connector_key)
    if normalized == "smartrecruiters":
        return fetch_smartrecruiters(connector_key)
    raise UnsupportedConnectorError(f"Connector '{connector}' is not implemented yet")


def fetch_job_detail(
    connector: str, connector_key: str, posting: JobPosting
) -> JobDetail:
    """Return full detail for one posting without changing the board baseline."""
    normalized = connector.strip().lower()
    if normalized == "greenhouse":
        description = posting.description.strip()
        if not description:
            raise ConnectorError(
                f"Greenhouse job {posting.source_job_id} is missing its description"
            )
        return JobDetail(
            description=description,
            employment_type=posting.employment_type,
            location_type=posting.location_type,
            raw=posting.raw,
        )
    if normalized == "tesla":
        return fetch_tesla_detail(posting)
    if normalized == "salesforce":
        return fetch_salesforce_detail(connector_key, posting)
    if normalized == "smartrecruiters":
        return fetch_smartrecruiters_detail(connector_key, posting)
    if normalized == "apple":
        return fetch_apple_detail(posting)
    if normalized == "microsoft":
        return fetch_microsoft_detail(posting)
    if normalized == "meta":
        return fetch_meta_detail(posting)
    if normalized == "adobe":
        return fetch_adobe_detail(posting)
    raise UnsupportedConnectorError(
        f"Job-detail retrieval for connector '{connector}' is not implemented yet"
    )
