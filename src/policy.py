from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path


def load_policy(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def normalize_text(value: str) -> str:
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[/_\-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _contains_alias(text: str, alias: str) -> bool:
    alias = normalize_text(alias)
    if len(alias) <= 3:
        return bool(re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", text))
    return alias in text


def location_is_allowed(location: str, location_type: str, policy: dict) -> bool:
    text = normalize_text(location)
    type_text = normalize_text(location_type)
    physical_match = any(
        _contains_alias(text, alias)
        for region in policy["locations"].values()
        for alias in region["aliases"]
    )
    remote_only = "remote" in type_text and "hybrid" not in type_text
    remote_only = remote_only or ("remote" in text and not physical_match)
    if remote_only and not policy["work_arrangements"]["allow_remote_only"]:
        return False
    return physical_match


def employment_type_prefilter(employment_type: str, policy: dict) -> tuple[bool, str]:
    text = normalize_text(employment_type)
    rules = policy["employment_types"]
    for phrase in rules["rejected"]:
        if normalize_text(phrase) in text:
            return False, f"excluded employment type: {phrase}"
    for phrase in rules["allowed"]:
        if normalize_text(phrase) in text:
            return True, f"allowed employment type: {phrase}"
    return True, "employment type not stated"


class _DescriptionTextParser(HTMLParser):
    BLOCK_TAGS = {"br", "div", "h1", "h2", "h3", "h4", "li", "p", "section"}
    IGNORED_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.IGNORED_TAGS:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.IGNORED_TAGS and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def clean_job_description(description: str) -> str:
    parser = _DescriptionTextParser()
    parser.feed(description)
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def title_prefilter(title: str, policy: dict) -> tuple[bool, str]:
    text = normalize_text(title)
    rules = policy["title_screen"]
    for phrase in rules["always_reject_phrases"]:
        if normalize_text(phrase) in text:
            return False, f"excluded title phrase: {phrase}"

    has_engineer = "engineer" in text or "engineering" in text
    families = {
        "data engineering": "data" in text and has_engineer,
        "analytics engineering": "analytics" in text and has_engineer,
        "ai/ml engineering": has_engineer
        and any(term in text for term in (" ai ", "ai ", " ml ", "ml ", "machine learning", "llm")),
        "product management": "product" in text and "manager" in text,
        "forward deployed engineering": "forward deployed" in text and has_engineer,
        "solutions engineering": "solutions" in text and has_engineer,
        "software engineering - data": "software" in text and has_engineer and "data" in text,
    }
    matched_families = [name for name, matched in families.items() if matched]
    if matched_families:
        return True, matched_families[0]

    concept_aliases = {
        "artificial intelligence": "ai",
        "machine learning": "ml",
        "engineering": "engineer",
    }
    matched_keywords = {
        concept_aliases.get(normalize_text(keyword), normalize_text(keyword))
        for keyword in rules["relevant_keywords"]
        if _contains_alias(text, keyword)
    }
    if "software" in text and has_engineer and "data" not in text:
        return False, "software engineer title lacks data"
    if len(matched_keywords) >= rules["minimum_relevant_keyword_matches"]:
        return True, f"broad keyword match: {', '.join(sorted(matched_keywords))}"
    return False, "insufficient relevant title signal"


def explicit_no_sponsorship(description: str, policy: dict) -> bool:
    text = normalize_text(description)
    return any(normalize_text(phrase) in text for phrase in policy["sponsorship"]["negative_phrases"])


def required_minimum_years(description: str) -> int | None:
    text = normalize_text(description)
    patterns = [
        r"(?:minimum of|at least|minimum|required)\s+(\d{1,2})\+?\s+years?",
        r"(\d{1,2})\+\s+years?\s+(?:of\s+)?experience",
        r"(\d{1,2})\s+(?:to\s+)?\d{1,2}\s+years?\s+(?:of\s+)?experience",
    ]
    matches: list[int] = []
    for pattern in patterns:
        matches.extend(int(value) for value in re.findall(pattern, text))
    return min(matches) if matches else None
