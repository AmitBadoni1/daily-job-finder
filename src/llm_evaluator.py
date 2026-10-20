from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_REASONING = "medium"
DEFAULT_PROMPT_VERSION = "v1"
DEFAULT_DESCRIPTION_LIMIT = 24_000


class EvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvaluationConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    reasoning_effort: str = DEFAULT_REASONING
    prompt_version: str = DEFAULT_PROMPT_VERSION
    endpoint: str = OPENAI_RESPONSES_URL
    timeout_seconds: int = 90
    max_description_chars: int = DEFAULT_DESCRIPTION_LIMIT

    @classmethod
    def from_environment(cls) -> "EvaluationConfig":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise EvaluationError(
                "OPENAI_API_KEY is not configured for this Windows user"
            )
        return cls(
            api_key=api_key,
            model=os.environ.get("JOB_FINDER_MODEL", DEFAULT_MODEL).strip()
            or DEFAULT_MODEL,
            reasoning_effort=os.environ.get(
                "JOB_FINDER_REASONING", DEFAULT_REASONING
            ).strip()
            or DEFAULT_REASONING,
            prompt_version=os.environ.get(
                "JOB_FINDER_PROMPT_VERSION", DEFAULT_PROMPT_VERSION
            ).strip()
            or DEFAULT_PROMPT_VERSION,
        )

    def with_overrides(
        self,
        *,
        model: str = "",
        reasoning_effort: str = "",
        prompt_version: str = "",
    ) -> "EvaluationConfig":
        return replace(
            self,
            model=model or self.model,
            reasoning_effort=reasoning_effort or self.reasoning_effort,
            prompt_version=prompt_version or self.prompt_version,
        )


EVALUATION_SCHEMA = {
    "type": "object",
    "properties": {
        "minimum_years_required": {
            "anyOf": [
                {"type": "number", "minimum": 0, "maximum": 50},
                {"type": "null"},
            ]
        },
        "experience_evidence": {"type": "string"},
        "experience_pass": {"type": "boolean"},
        "sponsorship_status": {
            "type": "string",
            "enum": [
                "explicit_available",
                "explicit_unavailable",
                "not_mentioned",
                "unclear",
            ],
        },
        "sponsorship_evidence": {
            "anyOf": [{"type": "string"}, {"type": "null"}]
        },
        "sponsorship_pass": {"type": "boolean"},
        "role_fit": {"type": "string", "enum": ["high", "medium", "low"]},
        "relevance_score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },
        "recommended_accept": {"type": "boolean"},
        "decision_reason": {"type": "string"},
        "matching_evidence": {
            "type": "array",
            "items": {"type": "string"},
        },
        "gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "minimum_years_required",
        "experience_evidence",
        "experience_pass",
        "sponsorship_status",
        "sponsorship_evidence",
        "sponsorship_pass",
        "role_fit",
        "relevance_score",
        "recommended_accept",
        "decision_reason",
        "matching_evidence",
        "gaps",
    ],
    "additionalProperties": False,
}


Transport = Callable[[str, dict, str, int], dict]


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_prompt(prompt_directory: str | Path, version: str) -> str:
    path = Path(prompt_directory) / f"evaluation_prompt_{version}.txt"
    if not path.exists():
        raise EvaluationError(f"Evaluation prompt file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def _bounded_description(description: str, limit: int) -> str:
    description = description.strip()
    if len(description) <= limit:
        return description
    half = max(1, limit // 2)
    return (
        description[:half]
        + "\n\n[Middle of description omitted to control API usage]\n\n"
        + description[-half:]
    )


def _post_response(url: str, payload: dict, api_key: str, timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("error", {}).get("message", body)
            except json.JSONDecodeError:
                detail = body
            last_error = EvaluationError(f"OpenAI API returned HTTP {exc.code}: {detail}")
            if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                break
            retry_after = exc.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 2**attempt
            except ValueError:
                delay = 2**attempt
            time.sleep(min(30.0, max(1.0, delay)))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    raise EvaluationError(f"OpenAI request failed: {last_error}")


def _extract_output_text(response: dict) -> str:
    if response.get("status") != "completed":
        error = response.get("error") or response.get("incomplete_details")
        raise EvaluationError(f"OpenAI response was not completed: {error}")
    texts: list[str] = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "refusal":
                raise EvaluationError(
                    f"OpenAI refused the evaluation: {content.get('refusal', '')}"
                )
            if content.get("type") == "output_text" and content.get("text"):
                texts.append(str(content["text"]))
    if not texts:
        raise EvaluationError("OpenAI response did not contain structured output text")
    return "\n".join(texts)


def _validate_evaluation(value: dict) -> dict:
    required = set(EVALUATION_SCHEMA["required"])
    if set(value) != required:
        missing = sorted(required - set(value))
        extra = sorted(set(value) - required)
        raise EvaluationError(
            f"Evaluation fields did not match the schema; missing={missing}, extra={extra}"
        )
    years = value["minimum_years_required"]
    if years is not None and (
        not isinstance(years, (int, float))
        or isinstance(years, bool)
        or years < 0
        or years > 50
    ):
        raise EvaluationError("minimum_years_required must be null or a valid number")
    status = value["sponsorship_status"]
    if status not in {
        "explicit_available",
        "explicit_unavailable",
        "not_mentioned",
        "unclear",
    }:
        raise EvaluationError(f"Invalid sponsorship_status: {status}")
    if value["role_fit"] not in {"high", "medium", "low"}:
        raise EvaluationError(f"Invalid role_fit: {value['role_fit']}")
    score = value["relevance_score"]
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        raise EvaluationError("relevance_score must be an integer from 0 to 100")
    for field in ("matching_evidence", "gaps"):
        if not isinstance(value[field], list) or not all(
            isinstance(item, str) for item in value[field]
        ):
            raise EvaluationError(f"{field} must be a list of strings")

    normalized = dict(value)
    normalized["experience_pass"] = years is None or years <= 3
    normalized["sponsorship_pass"] = status != "explicit_unavailable"
    if not normalized["experience_pass"] or not normalized["sponsorship_pass"]:
        normalized["recommended_accept"] = False
    return normalized


def evaluate_job(
    *,
    job: dict,
    description: str,
    candidate_profile: dict,
    search_policy: dict,
    prompt_directory: str | Path,
    config: EvaluationConfig,
    transport: Transport | None = None,
) -> dict:
    prompt = load_prompt(prompt_directory, config.prompt_version)
    input_payload = {
        "candidate_profile": candidate_profile,
        "search_policy": search_policy,
        "job": {
            "company": job.get("company", ""),
            "title": job.get("title", ""),
            "location": job.get("location", ""),
            "work_arrangement": job.get("work_arrangement", ""),
            "employment_type": job.get("employment_type", ""),
            "description": _bounded_description(
                description, config.max_description_chars
            ),
        },
    }
    request_payload = {
        "model": config.model,
        "instructions": prompt,
        "input": json.dumps(input_payload, ensure_ascii=False),
        "reasoning": {"effort": config.reasoning_effort},
        "max_output_tokens": 2_000,
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "job_evaluation",
                "strict": True,
                "schema": EVALUATION_SCHEMA,
            }
        },
    }
    sender = transport or _post_response
    response = sender(
        config.endpoint,
        request_payload,
        config.api_key,
        config.timeout_seconds,
    )
    try:
        parsed = json.loads(_extract_output_text(response))
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"OpenAI returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise EvaluationError("OpenAI evaluation was not a JSON object")
    evaluation = _validate_evaluation(parsed)
    usage = response.get("usage")
    if isinstance(usage, dict):
        evaluation["api_usage"] = {
            key: usage.get(key)
            for key in ("input_tokens", "output_tokens", "total_tokens")
            if usage.get(key) is not None
        }
    return evaluation
