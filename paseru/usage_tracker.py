"""월별 NCP/카카오 API 호출량을 구글시트에 기록하고, 관리자 화면에서 확인한다.

지도 API는 2종류이고 NCP 콘솔의 월 무료 한도가 서로 달라 따로 센다.
  - geocode:    지오코딩 주소검색 (콘솔 한도 월 300만 건)
  - directions: 길찾기(Directions5) — 노선 생성에서 훨씬 많이 쓰임 (콘솔 한도 월 6만 건)
  - place:      카카오 장소검색(무료, 참고용으로만 집계)

- 구글시트가 설정되어 있지 않으면 아무 것도 하지 않고 조용히 넘어간다(앱 동작에 영향 없음).
- 호출마다 시트에 쓰면 느리고 구글시트 자체 호출 한도에도 걸리므로, API 종류별로 일정 건수
  (USAGE_FLUSH_EVERY)만큼 모았다가 한 번에 합산해서 기록한다. 그만큼 실제 합계보다 최대
  USAGE_FLUSH_EVERY-1건 정도 늦게 반영될 수 있다.
"""
import datetime
import threading

import streamlit as st

from paseru.settings import (
    MONTHLY_LIMITS,
    USAGE_FLUSH_EVERY,
    gsheet_service_account_info,
    gsheet_spreadsheet_id,
)

WORKSHEET_NAME = "usage"
API_TYPES = ["geocode", "directions", "place"]
_COLUMN_OF = {"geocode": 2, "directions": 3, "place": 4}
_HEADER = ["월", "geocode", "directions", "place"]

_LOCK = threading.Lock()
_PENDING = {k: 0 for k in API_TYPES}


@st.cache_resource(show_spinner=False)
def _client():
    info = gsheet_service_account_info()
    if not info:
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return gspread.authorize(creds)
    except Exception:
        return None


def is_configured():
    return bool(gsheet_service_account_info()) and bool(gsheet_spreadsheet_id())


def _worksheet():
    gc = _client()
    sheet_id = gsheet_spreadsheet_id()
    if not gc or not sheet_id:
        return None
    try:
        sh = gc.open_by_key(sheet_id)
        try:
            return sh.worksheet(WORKSHEET_NAME)
        except Exception:
            ws = sh.add_worksheet(title=WORKSHEET_NAME, rows=200, cols=len(_HEADER))
            ws.append_row(_HEADER)
            return ws
    except Exception:
        return None


def _this_month():
    return datetime.date.today().strftime("%Y-%m")


def _find_row(ws, month):
    try:
        for i, v in enumerate(ws.col_values(1), start=1):
            if v == month:
                return i
    except Exception:
        pass
    return None


def _to_int(v):
    try:
        return int(str(v or "0").replace(",", ""))
    except Exception:
        return 0


def _write_to_sheet(api_type, n):
    ws = _worksheet()
    if not ws or n <= 0:
        return
    col = _COLUMN_OF.get(api_type)
    if not col:
        return
    try:
        month = _this_month()
        row = _find_row(ws, month)
        if row is None:
            new_row = ["0"] * len(_HEADER)
            new_row[0] = month
            new_row[col - 1] = str(n)
            ws.append_row(new_row)
        else:
            current = _to_int(ws.cell(row, col).value)
            ws.update_cell(row, col, current + n)
    except Exception:
        pass  # 기록 실패가 앱 동작(노선 생성 등)을 막아서는 안 된다


def add_calls(api_type: str, n: int = 1):
    """실제 NCP/카카오 API 호출이 발생했을 때마다 부른다. 내부에서 모았다가 한 번에 반영."""
    if n <= 0 or api_type not in API_TYPES or not is_configured():
        return
    with _LOCK:
        _PENDING[api_type] += n
        if _PENDING[api_type] < USAGE_FLUSH_EVERY:
            return
        flush_n = _PENDING[api_type]
        _PENDING[api_type] = 0
    _write_to_sheet(api_type, flush_n)


def flush():
    """남아 있던 호출 수를 강제로 반영(작업 종료 시점 등에 호출)."""
    with _LOCK:
        pending = dict(_PENDING)
        for k in _PENDING:
            _PENDING[k] = 0
    for api_type, n in pending.items():
        if n:
            _write_to_sheet(api_type, n)


def current_month_usage(api_type: str):
    """(이번달 사용량, 시트 연결 여부)."""
    if not is_configured() or api_type not in API_TYPES:
        return 0, False
    ws = _worksheet()
    if not ws:
        return 0, False
    row = _find_row(ws, _this_month())
    if row is None:
        return 0, True
    col = _COLUMN_OF[api_type]
    return _to_int(ws.cell(row, col).value), True


def monthly_usage_all():
    """[(월, {"geocode":.., "directions":.., "place":..}), ...] 최신순. 관리자 화면용."""
    if not is_configured():
        return []
    ws = _worksheet()
    if not ws:
        return []
    try:
        rows = ws.get_all_values()[1:]
        parsed = []
        for r in rows:
            if not r or not r[0]:
                continue
            usage = {k: _to_int(r[c - 1]) if len(r) >= c else 0 for k, c in _COLUMN_OF.items()}
            parsed.append((r[0], usage))
        parsed.sort(key=lambda x: x[0], reverse=True)
        return parsed
    except Exception:
        return []


def monthly_limit_reached(api_type: str):
    """해당 API의 이번 달 자체 상한 도달 여부. 시트 미설정이면 항상 False."""
    limit = MONTHLY_LIMITS.get(api_type)
    if not limit:
        return False
    used, connected = current_month_usage(api_type)
    if not connected:
        return False
    return used >= limit
