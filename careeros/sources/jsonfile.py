"""File-backed job source.

Phase 1's working connector: reads postings from a JSON file or a directory of
them. It is what the test suite and the demo run on, and it doubles as the
import path for postings exported from a board or pasted by the user.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from careeros.sources.base import JobSource, RawJob, registry


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def _relative_date(record: dict[str, Any], key: str, sign: int, today: date | None = None) -> date | None:
    """Support `posted_days_ago` / `deadline_in_days` in fixture files.

    Sample and test data stay meaningful whenever they are run -- a fixture
    with a hard-coded deadline silently becomes an "expired" job tomorrow.
    """
    if key not in record or record[key] is None:
        return None
    try:
        offset = int(record[key])
    except (TypeError, ValueError):
        return None
    return (today or date.today()) + timedelta(days=sign * offset)


class JsonFileSource(JobSource):
    id = "jsonfile"

    def __init__(self, path: str | Path, source_id: str | None = None) -> None:
        self.path = Path(path)
        if source_id:
            self.id = source_id

    def _files(self) -> list[Path]:
        if self.path.is_dir():
            return sorted(self.path.glob("*.json"))
        return [self.path] if self.path.exists() else []

    def healthcheck(self) -> tuple[bool, str]:
        files = self._files()
        if not files:
            return False, f"No JSON files at {self.path}"
        return True, f"{len(files)} file(s)"

    def fetch(self, limit: int | None = None, **_kwargs: Any) -> Iterator[RawJob]:
        count = 0
        for path in self._files():
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            records = payload if isinstance(payload, list) else payload.get("jobs", [])
            for rec in records:
                if limit is not None and count >= limit:
                    return
                yield RawJob(
                    source=rec.get("source", self.id),
                    external_id=str(rec.get("id") or rec.get("external_id") or f"{path.stem}-{count}"),
                    title=rec.get("title", ""),
                    company=rec.get("company", ""),
                    description=rec.get("description", ""),
                    url=rec.get("url"),
                    location=rec.get("location"),
                    country=rec.get("country"),
                    city=rec.get("city"),
                    region=rec.get("region") or rec.get("state"),
                    employment_type=rec.get("employment_type"),
                    salary_raw=rec.get("salary") or rec.get("salary_raw"),
                    posted_on=_relative_date(rec, "posted_days_ago", -1)
                    or _parse_date(rec.get("posted_on") or rec.get("posted")),
                    deadline_on=_relative_date(rec, "deadline_in_days", 1)
                    or _parse_date(rec.get("deadline_on") or rec.get("deadline")),
                    applicant_count=rec.get("applicant_count") or rec.get("applicants"),
                    raw=rec,
                )
                count += 1


def register_default(path: str | Path) -> JsonFileSource:
    return registry.register(JsonFileSource(path))  # type: ignore[return-value]
