"""월별 NCP/카카오 API 호출량을 구글시트에 기록하고, 관리자 화면에서 확인한다.

- 구글시트가 설정되어 있지 않으면 아무 것도 하지 않고 조용히 넘어간다(앱 동작에 영향 없음).
- 호출마다 시트에 쓰면 느리고 구글시트 자체 호출 한도에도 걸리므로, 일정 건수(USAGE_FLUSH_EVERY)만큼
  모았다가 한 번에 합산해서 기록한다. 그만큼 실제 합계보다 최대 USAGE_FLUSH_EVERY-1건 정도 늦게 반영될 수 있다.
"""
import datetime
import threading

import streamlit as st

from paseru.settings import (
    MONTHLY_API_LIMIT,
    USAGE_FLUSH_EVERY,
    gsheet_service_account_info,
    gsheet_spreadsheet_id,
)

WORKSHEET_NAME = "usage"
_LOCK = threading.Lock()
_PENDING = {"n": 0}


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
            ws = sh.add_worksheet(title=WORKSHEET_NAME, rows=200, cols=2)
            ws.append_row(["월", "호출수"])
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


def _write_to_sheet(n):
    ws = _worksheet()
    if not ws or n <= 0:
        return
    try:
        month = _this_month()
        row = _find_row(ws, month)
        if row is None:
            ws.append_row([month, n])
        else:
            current = int((ws.cell(row, 2).value or "0").replace(",", ""))
            ws.update_cell(row, 2, current + n)
    except Exception:
        pass  # 기록 실패가 앱 동작(노선 생성 등)을 막아서는 안 된다


def add_calls(n: int = 1):
    """실제 NCP/카카오 API 호출이 발생했을 때마다 부른다. 내부에서 모았다가 한 번에 반영."""
    if n <= 0 or not is_configured():
        return
    with _LOCK:
        _PENDING["n"] += n
        if _PENDING["n"] < USAGE_FLUSH_EVERY:
            return
        flush_n = _PENDING["n"]
        _PENDING["n"] = 0
    _write_to_sheet(flush_n)


def flush():
    """남아 있던 호출 수를 강제로 반영(작업 종료 시점 등에 호출)."""
    with _LOCK:
        flush_n = _PENDING["n"]
        _PENDING["n"] = 0
    if flush_n:
        _write_to_sheet(flush_n)


def current_month_usage():
    """(이번달 사용량, 시트 연결 여부)."""
    if not is_configured():
        return 0, False
    ws = _worksheet()
    if not ws:
        return 0, False
    row = _find_row(ws, _this_month())
    if row is None:
        return 0, True
    try:
        return int((ws.cell(row, 2).value or "0").replace(",", "")), True
    except Exception:
        return 0, True


def monthly_usage_all():
    """[(월, 호출수), ...] 최신순. 관리자 화면용."""
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
            try:
                parsed.append((r[0], int((r[1] or "0").replace(",", ""))))
            except Exception:
                continue
        parsed.sort(key=lambda x: x[0], reverse=True)
        return parsed
    except Exception:
        return []


def monthly_limit_reached():
    """이번 달 자체 상한(MONTHLY_API_LIMIT)에 도달했는지. 시트 미설정이면 항상 False."""
    used, connected = current_month_usage()
    if not connected:
        return False
    return used >= MONTHLY_API_LIMIT
