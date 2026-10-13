"""OpenDART (opendart.fss.or.kr) client for contract-win disclosures and annual fundamentals.

Needs ``DART_API_KEY`` (free key from OpenDART). Network calls live in ``fetch_*`` helpers; the
``parse_*`` / ``analyze_*`` functions are pure so they can be tested without the network.
Without a key every public function raises ``DartNotConfigured`` — callers must show "not
configured", never substitute made-up numbers.
"""
import io
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta

import requests

BASE = "https://opendart.fss.or.kr/api"
CONTRACT_KEYWORDS = ("단일판매", "공급계약")
_corp_cache = {"ts": 0.0, "data": {}}
_corp_lock = threading.Lock()


class DartError(RuntimeError):
    pass


class DartNotConfigured(DartError):
    pass


def api_key():
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        raise DartNotConfigured("DART_API_KEY가 설정되지 않았습니다.")
    return key


def _get_json(path, params):
    response = requests.get(f"{BASE}/{path}", params={"crtfc_key": api_key(), **params}, timeout=12)
    response.raise_for_status()
    body = response.json()
    status = str(body.get("status", ""))
    if status == "013":  # no data for the query is a normal empty result
        return {"status": status, "list": []}
    if status != "000":
        raise DartError(f"DART 응답 오류 {status}: {body.get('message', '')}")
    return body


def to_number(text):
    """'1,234' -> 1234.0, '(1,234)' / '-1,234' -> negative, '-' / '' -> None."""
    cleaned = str(text if text is not None else "").strip().replace(",", "")
    if cleaned in ("", "-"):
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    try:
        value = float(cleaned.strip("()"))
    except ValueError:
        return None
    return -value if negative else value


# ── contract-win disclosures ──────────────────────────────────────────────────
def parse_contract_disclosures(rows):
    out = []
    for row in rows or []:
        title = str(row.get("report_nm", "")).strip()
        if not any(keyword in title for keyword in CONTRACT_KEYWORDS):
            continue
        rcept_no = str(row.get("rcept_no", "")).strip()
        out.append({
            "company": row.get("corp_name", ""), "symbol": row.get("stock_code", "") or "",
            "market": {"Y": "KOSPI", "K": "KOSDAQ", "N": "KONEX"}.get(row.get("corp_cls", ""), ""),
            "title": title, "date": _iso(row.get("rcept_dt", "")), "receiptNo": rcept_no,
            "correction": "정정" in title, "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}" if rcept_no else "",
        })
    return out


def _iso(yyyymmdd):
    text = str(yyyymmdd)
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if re.fullmatch(r"\d{8}", text) else text


def fetch_contract_disclosures(days=7, max_pages=3, today=None):
    today = today or date.today()
    rows = []
    for page in range(1, max_pages + 1):
        body = _get_json("list.json", {
            "bgn_de": (today - timedelta(days=days)).strftime("%Y%m%d"), "end_de": today.strftime("%Y%m%d"),
            "pblntf_ty": "I", "page_no": page, "page_count": 100})
        rows += body.get("list", [])
        if page >= int(body.get("total_page", 1) or 1):
            break
    return parse_contract_disclosures(rows)


# ── corp code map ─────────────────────────────────────────────────────────────
def parse_corp_codes(xml_bytes):
    """CORPCODE.xml -> {stock_code: corp_code} for listed companies only."""
    out = {}
    for item in ET.fromstring(xml_bytes).iter("list"):
        stock, corp = (item.findtext("stock_code") or "").strip(), (item.findtext("corp_code") or "").strip()
        if stock and corp:
            out[stock] = corp
    return out


def corp_code_map(ttl=86_400):
    now = time.time()
    with _corp_lock:
        if _corp_cache["data"] and now - _corp_cache["ts"] < ttl:
            return _corp_cache["data"]
        response = requests.get(f"{BASE}/corpCode.xml", params={"crtfc_key": api_key()}, timeout=30)
        response.raise_for_status()
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                data = parse_corp_codes(archive.read(archive.namelist()[0]))
        except zipfile.BadZipFile as exc:  # DART returns an XML error body, not a zip, on bad keys
            raise DartError("고유번호 파일을 받지 못했습니다. API 키를 확인하세요.") from exc
        _corp_cache.update({"ts": now, "data": data})
        return data


# ── annual financials ─────────────────────────────────────────────────────────
_ACCOUNTS = {"revenue": ("매출액", "수익(매출액)", "영업수익"), "operating_income": ("영업이익", "영업이익(손실)"),
             "net_income": ("당기순이익", "당기순이익(손실)", "연결당기순이익", "당기순손익"),
             "equity": ("자본총계",), "liabilities": ("부채총계",), "assets": ("자산총계",)}


def parse_financials(rows):
    """fnlttSinglAcnt rows -> {field: {"current": x, "previous": y}}; prefers consolidated (CFS)."""
    best = {}
    for row in rows or []:
        name = str(row.get("account_nm", "")).replace(" ", "")
        for field, names in _ACCOUNTS.items():
            if name in {n.replace(" ", "") for n in names}:
                rank = 0 if row.get("fs_div") == "CFS" else 1
                if field not in best or rank < best[field][0]:
                    best[field] = (rank, {"current": to_number(row.get("thstrm_amount")),
                                          "previous": to_number(row.get("frmtrm_amount"))})
    return {field: value for field, (_, value) in best.items()}


def _ratio(a, b):
    return None if a is None or b in (None, 0) else a / b


def analyze_fundamentals(fin):
    """Derive the screening metrics. Anything not computable from the filing stays ``None``."""
    def pair(field):
        item = fin.get(field, {})
        return item.get("current"), item.get("previous")
    rev, rev_prev = pair("revenue")
    op, op_prev = pair("operating_income")
    net, _ = pair("net_income")
    equity, _ = pair("equity")
    liabilities, _ = pair("liabilities")
    if op is None or op_prev is None:
        status = "판단 불가"
    elif op_prev < 0 < op:
        status = "흑자전환"
    elif op_prev < 0 and op < 0:
        status = "적자 축소" if op > op_prev else "적자 지속"
    elif op_prev > 0 and op < 0:
        status = "적자전환"
    elif op_prev > 0 and op > 0:
        status = "영업이익 증가" if op > op_prev else "영업이익 감소"
    else:
        status = "판단 불가"
    return {
        "status": status, "turnaround": status == "흑자전환",
        "revenue": rev, "revenueGrowth": _ratio(None if rev is None or not rev_prev else rev - rev_prev, abs(rev_prev) if rev_prev else None),
        "operatingIncome": op, "operatingIncomePrev": op_prev,
        "operatingMargin": _ratio(op, rev), "netIncome": net,
        "roe": _ratio(net, equity), "debtRatio": _ratio(liabilities, equity),
    }


def fetch_financials(corp_code, year, report_code="11011"):
    body = _get_json("fnlttSinglAcnt.json", {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": report_code})
    return parse_financials(body.get("list", []))


def default_business_year(today=None):
    today = today or date.today()
    return today.year - 1 if today.month >= 4 else today.year - 2  # annual reports land by end of March
