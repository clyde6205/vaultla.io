"""B2B bulk provisioning: validate a CSV of up to 50,000 sub-capsules BEFORE creating anything.

Expected columns (header row required, case-insensitive):
    email, name, unlock_date            required
    group, message                      optional (group = cohort/class year/department)

Safety: strict size and row caps, UTF-8 only, per-row error report (row numbers, 1-based incl.
header), duplicate detection, RFC-lite email validation, and CSV/formula-injection neutralisation
so exported error reports and dashboards can be opened safely in Excel.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable

MAX_ROWS = 50_000
MAX_BYTES = 25 * 1024 * 1024
REQUIRED = ("email", "name", "unlock_date")
OPTIONAL = ("group", "message")
_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}$")
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True, slots=True)
class RowError:
    row: int
    field: str
    problem: str


@dataclass(frozen=True, slots=True)
class CapsuleRow:
    email: str
    name: str
    unlock_at: datetime
    group: str | None
    message: str | None


@dataclass(slots=True)
class ParseResult:
    rows: list[CapsuleRow] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    fatal: str | None = None

    @property
    def ok(self) -> bool:
        return self.fatal is None and not self.errors


def neutralise(cell: str) -> str:
    """Defuse spreadsheet formulas: prefix a single quote (OWASP CSV-injection guidance)."""
    return "'" + cell if cell.startswith(_FORMULA) else cell


def parse_csv(data: bytes, *, today: date, capacity_remaining: int = MAX_ROWS,
              max_unlock_years: int = 150) -> ParseResult:
    res = ParseResult()
    if len(data) > MAX_BYTES:
        res.fatal = f"File exceeds {MAX_BYTES // (1024 * 1024)} MB."
        return res
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        res.fatal = "File must be UTF-8 encoded."
        return res
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        res.fatal = "File is empty."
        return res
    header = [h.strip().lower() for h in reader.fieldnames]
    reader.fieldnames = header
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        res.fatal = f"Missing required column(s): {', '.join(missing)}."
        return res

    limit = min(MAX_ROWS, capacity_remaining)
    seen: dict[str, int] = {}
    for i, raw in enumerate(reader, start=2):        # row 1 is the header
        if len(res.errors) >= 1000:
            res.fatal = "Too many errors; fix the first 1,000 and re-upload."
            return res
        if i - 1 > limit:
            res.fatal = f"Row limit exceeded: your plan allows {limit} capsules per upload."
            res.rows.clear()
            return res
        row_errs = _validate(raw, i, today, max_unlock_years, seen)
        if row_errs:
            res.errors.extend(row_errs)
            continue
        res.rows.append(CapsuleRow(
            email=raw["email"].strip().lower(), name=neutralise(raw["name"].strip()),
            unlock_at=_to_dt(raw["unlock_date"].strip()),
            group=neutralise(raw["group"].strip()) if raw.get("group") and raw["group"].strip() else None,
            message=neutralise(raw["message"].strip()) if raw.get("message") and raw["message"].strip() else None))
    if not res.rows and not res.errors and res.fatal is None:
        res.fatal = "No data rows found."
    return res


def _to_dt(s: str) -> datetime:
    y, m, d = (int(p) for p in s.split("-"))
    return datetime(y, m, d, tzinfo=timezone.utc)


def _validate(raw: dict, row: int, today: date, max_years: int, seen: dict[str, int]) -> list[RowError]:
    errs: list[RowError] = []
    email = (raw.get("email") or "").strip().lower()
    if not _EMAIL.match(email):
        errs.append(RowError(row, "email", "not a valid email address"))
    elif email in seen:
        errs.append(RowError(row, "email", f"duplicate of row {seen[email]}"))
    else:
        seen[email] = row
    name = (raw.get("name") or "").strip()
    if not (1 <= len(name) <= 200):
        errs.append(RowError(row, "name", "required, up to 200 characters"))
    ud = (raw.get("unlock_date") or "").strip()
    try:
        d = date.fromisoformat(ud) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ud) else None
        if d is None:
            raise ValueError
        if d <= today:
            errs.append(RowError(row, "unlock_date", "must be in the future"))
        elif d.year - today.year > max_years:
            errs.append(RowError(row, "unlock_date", f"more than {max_years} years ahead"))
    except ValueError:
        errs.append(RowError(row, "unlock_date", "use YYYY-MM-DD"))
    for col, cap in (("group", 100), ("message", 500)):
        if raw.get(col) and len(raw[col]) > cap:
            errs.append(RowError(row, col, f"up to {cap} characters"))
    if None in raw:                                   # more cells than headers
        errs.append(RowError(row, "row", "has more columns than the header"))
    return errs
