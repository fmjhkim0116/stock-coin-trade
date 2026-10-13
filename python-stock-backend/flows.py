"""Parse investor net-buy CSV exports (KRX / broker HTS) into flow rows."""
import csv
import io
import re
from datetime import datetime

_ALIASES = {
    "date": ("date", "일자", "날짜", "trade_date", "기준일"),
    "foreign": ("foreign", "외국인", "외국인합계", "foreign_net"),
    "institution": ("institution", "기관", "기관합계", "institution_net", "inst"),
    "individual": ("individual", "개인", "individual_net"),
    "symbol": ("symbol", "종목코드", "code", "단축코드"),
    "name": ("name", "종목명", "company"),
}
_DATE_FORMATS = ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d", "%Y.%m.%d", "%Y.%m.%d.")


def _norm(name):
    return re.sub(r"[\s_]+", "", name.strip().lstrip("﻿").lower())


def _parse_date(text):
    text = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {text!r}")


def _parse_text(text):
    return text.strip()[:100]


def _parse_symbol(text):
    code = text.strip().upper().lstrip("A")  # some exports prefix KRX codes with 'A'
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"invalid symbol: {text!r}")
    return code


def _parse_number(text):
    cleaned = text.strip().replace(",", "").replace("+", "")
    if cleaned in ("", "-"):
        raise ValueError("empty number")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    return float(cleaned)


def parse_flows_csv(text, max_rows=20000, multi_symbol=False):
    """Return rows sorted by date: ``{date, foreign, institution, individual?}``.

    With ``multi_symbol=True`` a ``symbol`` (6-digit code) column is required, an optional ``name``
    column is kept, and rows are keyed and sorted by (symbol, date).

    Raises ``ValueError`` with a line number on malformed input.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("empty CSV")
    columns = {}
    for idx, name in enumerate(header):
        for key, aliases in _ALIASES.items():
            if _norm(name) in {_norm(a) for a in aliases}:
                columns.setdefault(key, idx)
    required = ("date", "foreign", "institution") + (("symbol",) if multi_symbol else ())
    missing = [k for k in required if k not in columns]
    if missing:
        raise ValueError(f"missing columns: {', '.join(missing)}")
    rows = {}
    for line_no, record in enumerate(reader, start=2):
        if not any(cell.strip() for cell in record):
            continue
        if len(rows) >= max_rows:
            raise ValueError(f"too many rows (max {max_rows})")
        try:
            row = {key: _parse_text(record[idx]) if key == "name" else _parse_symbol(record[idx]) if key == "symbol"
                   else (_parse_date if key == "date" else _parse_number)(record[idx])
                   for key, idx in columns.items() if multi_symbol or key not in ("symbol", "name")}
        except (ValueError, IndexError) as exc:
            raise ValueError(f"line {line_no}: {exc}") from exc
        rows[(row.get("symbol", ""), row["date"])] = row  # later duplicate wins
    return [rows[k] for k in sorted(rows)]
