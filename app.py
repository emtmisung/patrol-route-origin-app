import base64
import calendar
import hashlib
import hmac
import html
import io
import json
import math
import re
import secrets
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, time as dtime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import folium
import pandas as pd
import qrcode
import requests
import streamlit as st
import streamlit.components.v1 as components
from branca.element import Element
from cryptography.fernet import Fernet, InvalidToken
from streamlit_local_storage import LocalStorage
from streamlit_folium import st_folium

# ----------------------------------------------------------------------------
# 기본 설정
# ----------------------------------------------------------------------------
st.set_page_config(page_title="파세루 오리진", page_icon="🚒", layout="wide")

GEOLOCATION_COMPONENT_DIR = Path(__file__).parent / "geolocation_component"
geolocation_component = components.declare_component(
    "paseru_geolocation",
    path=str(GEOLOCATION_COMPONENT_DIR),
)

GEOCODE_URL = "https://maps.apigw.ntruss.com/map-geocode/v2/geocode"
DIRECTIONS_URL = "https://maps.apigw.ntruss.com/map-direction/v1/driving"
KAKAO_PLACE_SEARCH_URL = "https://search.map.kakao.com/mapsearch/map.daum"
NCP_KEY_ID = st.secrets.get("NCP_CLIENT_ID", "")
NCP_KEY = st.secrets.get("NCP_CLIENT_SECRET", "")
APP_PASSWORD = st.secrets.get("APP_PASSWORD", "")
MOBILE_TRANSFER_SECRET = str(
    st.secrets.get("MOBILE_TRANSFER_SECRET", NCP_KEY or APP_PASSWORD)
)
APP_PUBLIC_URL = str(
    st.secrets.get("APP_PUBLIC_URL", "https://faseru-origin.streamlit.app/")
).rstrip("/")

AVG_SPEED_KMH = 35.0      # NCP 호출 실패 시에만 쓰는 비상 대체값(직선거리 보정)
ROAD_FACTOR = 1.3         # NCP 호출 실패 시에만 쓰는 비상 대체 보정계수
API_CALL_LIMIT = 3000     # NCP 일일 조회 기준 참고 한도(과도한 연속 호출 방지용)

SAMPLE_XLSX = "seongju_patrol_coordinates_20.xlsx"

SAMPLE_TARGETS = [
    ("차동골 마을회관", "경상북도 성주군 성주읍 성산1리 1805"),
    ("모산 마을회관", "경상북도 성주군 성주읍 삼산리 245"),
    ("유월2리 마을회관", "경상북도 성주군 월항면 유월2리 96-1"),
    ("백인 마을회관", "경상북도 성주군 월항면 안포1리 540-1"),
    ("안포4리 마을회관", "경상북도 성주군 월항면 안포4리 445"),
    ("댓기 마을회관", "경상북도 성주군 성주읍 학산1리 525-3"),
    ("연산 마을회관", "경상북도 성주군 성주읍 금산1리 912-3"),
    ("종로 마을회관", "경상북도 성주군 성주읍 경산8리 12-1"),
    ("말배미 마을회관", "경상북도 성주군 성주읍 학산2리 297-1"),
    ("작은배리 마을회관", "경상북도 성주군 성주읍 경산5리 761-37"),
    ("교촌 마을회관", "경상북도 성주군 성주읍 예산2리 215-1"),
    ("목우물 마을회관", "경상북도 성주군 성주읍 백전1리 241"),
    ("모방 마을회관", "경상북도 성주군 월항면 지방리 156-1"),
    ("원동경로당", "경상북도 성주군 월항면 칠선1길 51"),
    ("예동 마을회관", "경상북도 성주군 성주읍 예산리 393-6"),
    ("용산2리 마을회관", "경상북도 성주군 성주읍 용산2리 1058-1"),
    ("시뫼실 마을회관", "경상북도 성주군 성주읍 성산2리 1174-1"),
    ("부인 마을회관", "경상북도 성주군 월항면 인촌리 299"),
]
BROWSER_DRAFT_LEGACY_KEY = "paseru_last_work_v1"
BROWSER_DRAFT_KEY_PREFIX = "paseru_saved_work_v1_"
BROWSER_DRAFT_DAYS = 7
BROWSER_DRAFT_MAX_ITEMS = 3
MOBILE_TRANSFER_TTL_SECONDS = 10 * 60
MOBILE_TRANSFER_MAX_BYTES = 3_500_000
MOBILE_TRANSFER_DIR = Path(tempfile.gettempdir()) / "paseru_mobile_transfers_v1"


def ncp_headers():
    return {
        "x-ncp-apigw-api-key-id": NCP_KEY_ID,
        "x-ncp-apigw-api-key": NCP_KEY,
        "Accept": "application/json",
    }


def has_keys():
    return bool(NCP_KEY_ID) and bool(NCP_KEY)


def is_mobile_request():
    """현재 접속 브라우저가 휴대폰·태블릿인지 User-Agent로 구분한다."""
    try:
        headers = st.context.headers
        user_agent = str(
            headers.get("User-Agent", "") or headers.get("user-agent", "")
        ).lower()
    except (AttributeError, RuntimeError):
        user_agent = ""
    return bool(re.search(r"android|iphone|ipad|ipod|mobile|tablet", user_agent))


IS_MOBILE_DEVICE = is_mobile_request()


def dataframe_to_draft(df):
    """DataFrame을 브라우저 저장용 JSON 문자열로 바꾼다."""
    if df is None:
        return None
    return df.to_json(orient="split", force_ascii=False, date_format="iso")


def dataframe_from_draft(value):
    """브라우저에 저장된 JSON을 안전한 크기의 DataFrame으로 복원한다."""
    if not isinstance(value, str) or not value or len(value) > 4_000_000:
        return None
    restored = pd.read_json(io.StringIO(value), orient="split")
    if len(restored) > 10_000 or len(restored.columns) > 100:
        return None
    return restored


def decode_browser_draft(raw_value):
    """7일 만료 여부와 기본 구조를 확인한 뒤 저장자료를 반환한다."""
    if not raw_value:
        return None, "empty"
    try:
        payload = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return None, "invalid"
        expires_at = float(payload.get("expires_at", 0))
        if expires_at <= datetime.now().timestamp():
            return None, "expired"
        targets = dataframe_from_draft(payload.get("targets"))
        if targets is None or targets.empty:
            return None, "invalid"
        coords = dataframe_from_draft(payload.get("coords"))
        payload["targets_df"] = targets
        payload["coords_df"] = coords
        return payload, "ok"
    except (TypeError, ValueError, KeyError):
        return None, "invalid"


def browser_work_key(source_name, targets_df):
    """파일명과 대상목록으로 같은 작업을 계속 갱신할 저장 키를 만든다."""
    identity = f"{source_name or '업로드 자료'}\n{dataframe_to_draft(targets_df) or ''}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"{BROWSER_DRAFT_KEY_PREFIX}{digest}"


def browser_draft_content(source_name, patrol_title, station_query, station_result, targets_df,
                          coords_df, coord_api_calls):
    """변경 감지와 브라우저 저장에 사용할 최소 작업자료를 만든다."""
    return {
        "version": 1,
        "source_name": str(source_name or "업로드 자료"),
        "target_count": int(len(targets_df)) if targets_df is not None else 0,
        "patrol_title": str(patrol_title or ""),
        "station_query": str(station_query or ""),
        "station_result": station_result if isinstance(station_result, dict) else None,
        "targets": dataframe_to_draft(targets_df),
        "coords": dataframe_to_draft(coords_df),
        "coord_api_calls": int(coord_api_calls or 0),
    }


def browser_draft_label(payload):
    """저장된 작업 선택 목록에 표시할 한 줄 설명을 만든다."""
    source_name = payload.get("source_name") or payload.get("patrol_title") or "이전 저장자료"
    target_count = int(payload.get("target_count") or len(payload.get("targets_df", [])))
    try:
        saved_at = datetime.fromtimestamp(
            float(payload.get("saved_at", 0)), timezone.utc,
        ).astimezone(timezone(timedelta(hours=9)))
        saved_text = saved_at.strftime("%m월 %d일 %H:%M")
    except (TypeError, ValueError, OSError):
        saved_text = "저장시각 없음"
    return f"{source_name} · 대상 {target_count}건 · {saved_text}"


def minimum_transfer_targets(targets_df):
    """휴대폰 전달에는 대상명과 주소 열만 포함해 불필요한 원본 열을 제외한다."""
    columns = list(targets_df.columns)
    name_index = find_name_column_index(columns)
    address_index = find_address_column_index(columns, name_index)
    return pd.DataFrame({
        "대상명": targets_df.iloc[:, name_index].copy(),
        "주소": targets_df.iloc[:, address_index].copy(),
    })


def mobile_transfer_cipher():
    """앱 비밀값으로 일회용 전달자료의 서버 임시파일을 암호화한다."""
    if not MOBILE_TRANSFER_SECRET:
        return None
    key_material = hashlib.sha256(
        f"paseru-mobile-transfer-v1\n{MOBILE_TRANSFER_SECRET}".encode("utf-8"),
    ).digest()
    return Fernet(base64.urlsafe_b64encode(key_material))


def cleanup_mobile_transfers(now_timestamp=None):
    """10분이 지난 일회용 전달파일과 중단된 수신파일을 정리한다."""
    now_timestamp = float(now_timestamp or datetime.now().timestamp())
    MOBILE_TRANSFER_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for transfer_path in list(MOBILE_TRANSFER_DIR.glob("*.transfer")) + list(
        MOBILE_TRANSFER_DIR.glob("*.claim")
    ):
        try:
            if transfer_path.stat().st_mtime < now_timestamp - MOBILE_TRANSFER_TTL_SECONDS - 60:
                transfer_path.unlink(missing_ok=True)
                continue
            if transfer_path.suffix == ".transfer":
                envelope = json.loads(transfer_path.read_text(encoding="utf-8"))
                if float(envelope.get("expires_at", 0)) <= now_timestamp:
                    transfer_path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            transfer_path.unlink(missing_ok=True)


def create_mobile_transfer(draft_payload):
    """작업자료를 암호화해 10분짜리 일회용 전달 토큰으로 만든다."""
    cipher = mobile_transfer_cipher()
    if cipher is None:
        raise ValueError("앱 비밀번호가 설정되지 않아 전달자료를 암호화할 수 없습니다.")
    serialized = json.dumps(draft_payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(serialized) > MOBILE_TRANSFER_MAX_BYTES:
        raise ValueError("작업자료가 휴대폰 일회용 전달 허용 크기를 초과했습니다.")

    cleanup_mobile_transfers()
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now().timestamp() + MOBILE_TRANSFER_TTL_SECONDS
    envelope = {
        "expires_at": expires_at,
        "ciphertext": cipher.encrypt(serialized).decode("ascii"),
    }
    final_path = MOBILE_TRANSFER_DIR / f"{token}.transfer"
    temporary_path = MOBILE_TRANSFER_DIR / f"{token}.{secrets.token_hex(6)}.tmp"
    temporary_path.write_text(json.dumps(envelope), encoding="utf-8")
    temporary_path.chmod(0o600)
    temporary_path.replace(final_path)
    return token, expires_at


def consume_mobile_transfer(token):
    """일회용 토큰의 작업을 한 번만 복원하고 임시파일을 즉시 삭제한다."""
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,60}", token):
        return None, "invalid"
    cipher = mobile_transfer_cipher()
    if cipher is None:
        return None, "unavailable"

    cleanup_mobile_transfers()
    transfer_path = MOBILE_TRANSFER_DIR / f"{token}.transfer"
    claim_path = MOBILE_TRANSFER_DIR / f"{token}.{secrets.token_hex(6)}.claim"
    try:
        transfer_path.replace(claim_path)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "busy"

    try:
        envelope = json.loads(claim_path.read_text(encoding="utf-8"))
        if float(envelope.get("expires_at", 0)) <= datetime.now().timestamp():
            return None, "expired"
        decrypted = cipher.decrypt(envelope["ciphertext"].encode("ascii"))
        payload = json.loads(decrypted.decode("utf-8"))
        draft, draft_status = decode_browser_draft(payload)
        if draft_status != "ok":
            return None, "invalid"
        return draft, "ok"
    except (InvalidToken, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid"
    finally:
        claim_path.unlink(missing_ok=True)


def apply_browser_draft(payload, storage_key):
    """선택한 브라우저 저장 작업을 현재 세션으로 안전하게 전환한다."""
    restored_targets = payload["targets_df"].copy()
    st.session_state["browser_restored_df"] = restored_targets
    st.session_state["patrol_title"] = payload.get("patrol_title") or ""
    st.session_state["station_query"] = payload.get("station_query") or ""

    restored_station = payload.get("station_result")
    if isinstance(restored_station, dict):
        st.session_state["station_search_result"] = restored_station
    else:
        st.session_state.pop("station_search_result", None)

    restored_coords = payload.get("coords_df")
    if restored_coords is not None:
        st.session_state["coords_df"] = restored_coords.copy()
    else:
        st.session_state.pop("coords_df", None)
    st.session_state.pop("coord_future", None)
    st.session_state["coord_api_calls"] = int(payload.get("coord_api_calls", 0))

    restored_columns = list(restored_targets.columns)
    restored_name_idx = find_name_column_index(restored_columns)
    restored_addr_idx = find_address_column_index(restored_columns, restored_name_idx)
    st.session_state["coord_signature"] = tuple(
        (str(row[restored_columns[restored_name_idx]]),
         str(row[restored_columns[restored_addr_idx]]))
        for _, row in restored_targets.iterrows()
    )
    for stale_key in ("station", "route_results", "far_points", "meta"):
        st.session_state.pop(stale_key, None)

    st.session_state["active_browser_draft_key"] = storage_key
    st.session_state["browser_source_name"] = (
        payload.get("source_name") or payload.get("patrol_title") or "이전 저장자료"
    )
    st.session_state["browser_draft_saving_enabled"] = True
    st.session_state.pop("browser_draft_fingerprint", None)
    st.session_state.pop("browser_upload_signature", None)
    st.session_state["file_uploader_generation"] = (
        int(st.session_state.get("file_uploader_generation", 0)) + 1
    )


# ----------------------------------------------------------------------------
# NCP API 호출
# ----------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def geocode_once(address: str):
    """주소 -> (lat, lng, 상태). 상태: "ok" | "not_found" | "error:..." """
    try:
        r = requests.get(
            GEOCODE_URL, params={"query": address}, headers=ncp_headers(), timeout=10
        )
        if r.status_code != 200:
            return None, None, f"error:HTTP {r.status_code}"
        data = r.json()
        addrs = data.get("addresses") or []
        if addrs:
            a = addrs[0]
            return float(a["y"]), float(a["x"]), "ok"
        return None, None, "not_found"
    except Exception as e:
        return None, None, f"error:{type(e).__name__}"


def geocode_address(address: str):
    """기존 호출부 호환용 — (lat, lng)만 반환."""
    lat, lng, _ = geocode_once(address)
    return lat, lng


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def search_departure_department(query: str):
    """출발부서명을 장소검색하고, 실패하면 주소검색으로 다시 확인한다."""
    query = (query or "").strip()
    if not query:
        return None, None, None, None, "출발부서 이름을 입력하세요."

    # 1) 카카오 장소검색: '선남119안전센터'처럼 기관명으로 주소·좌표 확인
    try:
        place_response = requests.get(
            KAKAO_PLACE_SEARCH_URL,
            params={"q": query, "msFlag": "A", "sort": "0"},
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Faseru-Origin/1.0)",
                "Referer": "https://map.kakao.com/",
                "Accept": "application/json, text/plain, */*",
            },
            timeout=10,
        )
        if place_response.status_code == 200:
            places = place_response.json().get("place") or []
            if places:
                normalized = re.sub(r"\\s+", "", query).lower()
                item = next(
                    (p for p in places
                     if re.sub(r"\\s+", "", str(p.get("name", ""))).lower() == normalized),
                    places[0],
                )
                address = item.get("new_address") or item.get("address")
                lat, lng = item.get("lat"), item.get("lon")
                if address and lat is not None and lng is not None:
                    return item.get("name") or query, address, float(lat), float(lng), "ok"
    except Exception:
        pass

    # 2) 기관명 검색 결과가 없으면 입력값을 주소로 보고 NCP 지오코딩 시도
    try:
        r = requests.get(
            GEOCODE_URL, params={"query": query}, headers=ncp_headers(), timeout=10
        )
        if r.status_code != 200:
            detail = ""
            try:
                detail = (r.json().get("error") or {}).get("message", "")
            except Exception:
                pass
            message = f"주소검색 연결 오류(HTTP {r.status_code})"
            if detail:
                message += f": {detail}"
            return None, None, None, None, message
        addresses = r.json().get("addresses") or []
        if not addresses:
            return None, None, None, None, "출발부서를 찾지 못했습니다. 정확한 부서명 또는 도로명주소를 입력해 주세요."
        item = addresses[0]
        address = item.get("roadAddress") or item.get("jibunAddress") or query
        return query, address, float(item["y"]), float(item["x"]), "ok"
    except Exception as exc:
        return None, None, None, None, f"검색 중 오류가 발생했습니다({type(exc).__name__})."


def address_variants(address: str, name: str = ""):
    """지오코딩이 실패했을 때 순서대로 다시 시도할 주소 후보들을 만든다.

    행정리('성산1리')는 지오코딩이 인식하지 못하는 경우가 많아
    법정리('성산리')로 바꾸는 것이 가장 중요한 보정이다.
    """
    address = (address or "").strip()
    cands = []

    def add(v, why):
        v = re.sub(r"\s+", " ", (v or "")).strip()
        if v and all(v != c[0] for c in cands):
            cands.append((v, why))

    add(address, "원본 주소")

    # 1) 행정리 번호 제거: 성산1리 → 성산리, 경산8리 → 경산리
    v1 = re.sub(r"([가-힣]+?)\d+리(?=\s|$)", r"\1리", address)
    add(v1, "행정리→법정리 (성산1리→성산리)")

    # 2) 괄호와 그 안의 내용 제거
    v2 = re.sub(r"\([^)]*\)", " ", v1)
    add(v2, "괄호 제거")

    # 3) 지번의 부번 제거: 540-1 → 540
    v3 = re.sub(r"(\d+)-\d+(?=\s|$)", r"\1", v2)
    add(v3, "지번 부번 제거 (540-1→540)")

    # 4) 번지 자체를 떼고 리(동) 중심으로: … 성산리 1805 → … 성산리
    v4 = re.sub(r"\s+\d+(-\d+)?\s*$", "", v3)
    add(v4, "번지 제외 (리·동 중심 좌표)")

    # 5) 대상명 안 괄호에 들어 있는 주소를 활용: 차동골 마을회관 (성주읍 성산1리 1805)
    m = re.search(r"\(([^)]*)\)", name or "")
    if m:
        inner = re.sub(r"([가-힣]+?)\d+리(?=\s|$)", r"\1리", m.group(1))
        add(f"경상북도 성주군 {inner}", "대상명 속 주소 사용")
        add(re.sub(r"\s+\d+(-\d+)?\s*$", "", f"경상북도 성주군 {inner}"), "대상명 속 주소(번지 제외)")

    return cands


def geocode_with_fallback(address: str, name: str = "", on_call=None, should_stop=None):
    """여러 주소 형태로 순차 시도. 반환: (lat, lng, 성공에 쓴 주소, 방법, 시도내역)"""
    tried = []
    for query, why in address_variants(address, name):
        if should_stop and should_stop():
            tried.append("API 호출 한도 도달")
            break
        lat, lng, status = geocode_once(query)
        if on_call:
            on_call()
        tried.append(f"{why}: {query} → {status}")
        if status == "ok":
            return lat, lng, query, why, tried
    return None, None, None, None, tried


def geocode_failure_reason(tried):
    """좌표 검색 시도내역을 사용자가 이해하기 쉬운 실패 사유로 바꾼다."""
    details = " / ".join(tried or [])
    if "API 호출 한도 도달" in details:
        return "API 호출 한도에 도달해 검색이 중단되었습니다."
    if "HTTP 429" in details:
        return "지도 API의 일시적인 호출 제한에 도달했습니다."
    if "error:" in details:
        return "지도 API 연결 중 오류가 발생했습니다. 잠시 후 다시 검색해 주세요."
    if "not_found" in details:
        return "입력 주소와 보정 주소를 지도에서 찾지 못했습니다. 주소를 확인해 주세요."
    return "주소를 확인하지 못했습니다. 주소를 수정해 다시 검색해 주세요."


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def road_route(o_lat, o_lng, d_lat, d_lng):
    """실도로 거리(km)/시간(분)/경로좌표 반환. 실패 시 None 튜플."""
    try:
        r = requests.get(
            DIRECTIONS_URL,
            params={
                "start": f"{o_lng},{o_lat}",
                "goal": f"{d_lng},{d_lat}",
                "option": "trafast",
            },
            headers=ncp_headers(),
            timeout=10,
        )
        data = r.json()
        route = data.get("route", {})
        for key in ("trafast", "traoptimal", "tracomfort"):
            if key in route and route[key]:
                summ = route[key][0]["summary"]
                path = route[key][0].get("path", [])
                return (
                    summ["distance"] / 1000.0,
                    summ["duration"] / 60000.0,
                    [(p[1], p[0]) for p in path],
                )
        return None, None, None
    except Exception:
        return None, None, None


def haversine_km(a, b):
    lat1, lng1 = a
    lat2, lng2 = b
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    x = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))


# ----------------------------------------------------------------------------
# 한글(hwpx) 표 파싱 — 데모(웹 프로토타입)와 동일한 방식
# ----------------------------------------------------------------------------
HEADER_WORDS = re.compile(r"^(연번|no\.?|번호|구분|이름|명칭|대상명|대상명주소|주소|정제_주소|비고)$", re.I)

NAME_HEADER_WORDS = ("대상물명", "대상명", "시설명", "명칭", "이름")
ADDRESS_HEADER_WORDS = ("주소", "주소지", "소재지")
SERIAL_HEADER_WORDS = ("연번", "순번", "번호", "no")


def _header_text(value):
    """헤더 비교용 문자열. 공백·줄바꿈·밑줄 차이는 무시한다."""
    if pd.isna(value):
        return ""
    return re.sub(r"[\s_]", "", str(value)).lower()


def _find_header_row(raw_df, scan_rows=20):
    """제목행이 위에 있어도 대상명·주소가 있는 실제 헤더행을 찾는다."""
    for row_idx in range(min(scan_rows, len(raw_df))):
        tokens = [_header_text(v) for v in raw_df.iloc[row_idx].tolist()]
        tokens = [v for v in tokens if v]
        has_name = any(any(word in token for word in NAME_HEADER_WORDS) for token in tokens)
        has_address = any(any(word in token for word in ADDRESS_HEADER_WORDS) for token in tokens)
        has_serial = any(token in SERIAL_HEADER_WORDS for token in tokens)
        if has_address and (has_name or has_serial):
            return row_idx
    return None


def _looks_like_headerless_data(raw_df):
    """항목명까지 지운 파일에서 첫 실제 대상을 헤더로 잃지 않도록 판별한다."""
    if raw_df is None or raw_df.empty:
        return False
    values = [v for v in raw_df.iloc[0].tolist() if not pd.isna(v) and str(v).strip()]
    if len(values) < 2:
        return False
    first = str(values[0]).strip()
    if re.fullmatch(r"\d+(?:\.0)?", first):
        return True
    return any(re.search(r"(?:시|군|구|읍|면|동|리)\s*\S*\d", str(v)) for v in values)


def _make_unique_headers(values):
    headers, seen = [], {}
    for idx, value in enumerate(values, start=1):
        base = str(value).strip() if not pd.isna(value) and str(value).strip() else f"열{idx}"
        seen[base] = seen.get(base, 0) + 1
        headers.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return headers


def normalize_uploaded_table(raw_df):
    """제목행/헤더행/헤더 없는 목록을 모두 실제 대상 행 기준으로 정리한다."""
    if raw_df is None or raw_df.empty:
        return raw_df

    raw_df = raw_df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    header_row = _find_header_row(raw_df)
    if header_row is not None:
        df = raw_df.iloc[header_row + 1:].copy()
        df.columns = _make_unique_headers(raw_df.iloc[header_row].tolist())
    elif _looks_like_headerless_data(raw_df):
        df = raw_df.copy()
        width = len(df.columns)
        first_value = str(df.iloc[0, 0]).strip() if width else ""
        if width >= 3 and re.fullmatch(r"\d+(?:\.0)?", first_value):
            defaults = ["연번", "대상명", "주소", "비고", "위도", "경도"]
        else:
            defaults = ["대상명", "주소", "비고", "위도", "경도"]
        df.columns = defaults[:width] + [f"열{i}" for i in range(len(defaults) + 1, width + 1)]
    else:
        # 기존 방식과의 호환: 첫 행을 일반적인 열 이름으로 사용한다.
        df = raw_df.iloc[1:].copy()
        df.columns = _make_unique_headers(raw_df.iloc[0].tolist())

    df = df.dropna(axis=1, how="all").dropna(how="all").reset_index(drop=True)
    # 파일 중간에 항목명이 반복된 경우 대상 건수에서 제외한다.
    repeated_headers = df.apply(
        lambda row: _find_header_row(pd.DataFrame([row.tolist()]), scan_rows=1) == 0,
        axis=1,
    )
    return df.loc[~repeated_headers].reset_index(drop=True)


def read_uploaded_table(file_bytes, file_name):
    """CSV/엑셀을 헤더 지정 없이 먼저 읽은 뒤 실제 헤더행을 자동 판별한다."""
    source = io.BytesIO(file_bytes)
    if file_name.lower().endswith(".csv"):
        try:
            raw_df = pd.read_csv(source, header=None)
        except UnicodeDecodeError:
            source.seek(0)
            raw_df = pd.read_csv(source, header=None, encoding="cp949")
    else:
        raw_df = pd.read_excel(source, header=None)
    return normalize_uploaded_table(raw_df)


def load_sample_targets():
    """예시 원본에서 업무 구분과 출발부서를 제외한 평가용 대상 18곳만 만든다."""
    return pd.DataFrame(SAMPLE_TARGETS, columns=["대상명", "주소"])


def find_name_column_index(columns):
    """순번·연번 대신 실제 대상물명 열을 우선 선택한다."""
    normalized = [_header_text(column) for column in columns]
    for preferred in NAME_HEADER_WORDS:
        for idx, token in enumerate(normalized):
            if preferred in token:
                return idx

    excluded_words = ADDRESS_HEADER_WORDS + ("비고", "조별", "위도", "경도", "lat", "lng", "lon")
    for idx, token in enumerate(normalized):
        if token in SERIAL_HEADER_WORDS:
            continue
        if not any(word in token for word in excluded_words):
            return idx
    return 1 if len(columns) > 1 else 0


def find_address_column_index(columns, name_index):
    """대상명 열과 겹치지 않는 주소 열을 선택한다."""
    normalized = [_header_text(column) for column in columns]
    corrected = next((idx for idx, token in enumerate(normalized) if "정제" in token), None)
    if corrected is not None and corrected != name_index:
        return corrected
    address = next(
        (idx for idx, token in enumerate(normalized)
         if idx != name_index and any(word in token for word in ADDRESS_HEADER_WORDS)),
        None,
    )
    return address if address is not None else min(3, len(columns) - 1)


def _clean_xml_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
             .replace("&quot;", '"').replace("&apos;", "'").strip())


def parse_hwpx(file_bytes: bytes):
    """hwpx 안의 표(또는 문단)를 읽어 DataFrame으로 반환."""
    rows = []
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        for name in zf.namelist():
            if not re.search(r"Contents/section\d*\.xml$", name, re.I):
                continue
            xml = zf.read(name).decode("utf-8", errors="ignore")
            table_rows = re.findall(r"<hp:tr[\s>][\s\S]*?</hp:tr>", xml)
            if table_rows:
                for row_xml in table_rows:
                    cells = re.findall(r"<hp:tc[\s>][\s\S]*?</hp:tc>", row_xml)
                    cols = [_clean_xml_text("".join(re.findall(r"<hp:t[^>]*>([\s\S]*?)</hp:t>", c)))
                            for c in cells]
                    if any(cols):
                        rows.append(cols)
            else:
                for chunk in xml.split("<hp:p")[1:]:
                    text = _clean_xml_text("".join(re.findall(r"<hp:t[^>]*>([\s\S]*?)</hp:t>", chunk)))
                    if text:
                        rows.append([text])

    rows = [r for r in rows if r and not HEADER_WORDS.match((r[0] or "").strip())]
    if not rows:
        return None

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    default_names = ["연번", "주소지", "비고", "정제_주소", "위도(Latitude)", "경도(Longitude)"]
    cols = default_names[:width] + [f"열{i}" for i in range(len(default_names) + 1, width + 1)]
    return pd.DataFrame(rows, columns=cols[:width])


# ----------------------------------------------------------------------------
# 경로 편성 알고리즘 (최근접 이웃 기반, 소방서 출발/복귀)
# 거리·시간 판단은 전부 NCP Directions5의 실제 도로거리를 사용한다.
# (직선거리는 API 호출이 실패했을 때만 비상 대체값으로 쓰인다)
# ----------------------------------------------------------------------------
def real_leg(a, b, on_call=None):
    """a, b: dict(lat, lng). 실도로 거리(km)/시간(분) 반환 (실패 시 직선거리 보정값)."""
    km, mins, _ = road_route(a["lat"], a["lng"], b["lat"], b["lng"])
    if on_call:
        on_call()
    if km is None:
        km = haversine_km((a["lat"], a["lng"]), (b["lat"], b["lng"])) * ROAD_FACTOR
        mins = km / AVG_SPEED_KMH * 60
    return km, mins


def nearest_by_straight_line(cur, candidates, k):
    """직선거리로 가까운 순 k개만 추린다. (실제 API 호출 횟수를 줄이기 위한 1차 필터)

    도로망은 직선거리와 순서가 크게 다르지 않으므로, 가까운 후보 몇 개만
    실제 도로거리로 확인해도 결과는 거의 동일하면서 API 호출은 크게 줄어든다.
    """
    if k <= 0 or k >= len(candidates):
        return candidates
    ranked = sorted(
        candidates,
        key=lambda p: haversine_km((cur["lat"], cur["lng"]), (p["lat"], p["lng"])),
    )
    return ranked[:k]


def build_routes(points, station, mode, max_per_route, seg_max_km, seg_max_min,
                 target_min_high, max_routes_cap, basis="distance", on_call=None,
                 candidate_k=5, should_stop=None, service_min_per_stop=0,
                 strict_route_cap=False, route_variant=0):
    """points: list of dict(name, address, lat, lng)
    반환: routes(list of list of point dict), unassigned(장거리/미배정)

    mode:
      "segment"     — 구간당 거리·시간 제한
      "target_time" — 노선 전체 왕복 목표시간 제한
      "fixed"       — 노선당 구간 수(max_per_route)를 그대로 채움 (노선 수 = 상한까지)
    basis: "distance"(거리 기준) | "time"(소요시간 기준)
    candidate_k: 다음 지점 후보를 직선거리로 몇 개까지 좁혀서 실제 API로 확인할지 (0=전수)
    should_stop: 호출 한도 초과 등으로 중단해야 하는지 판단하는 함수.
                 중단되면 그때까지 편성된 노선만 반환한다(진행분 보존).
    strict_route_cap: 노선 수 상한에 도달했을 때 남은 대상을 마지막 노선에
                      합치지 않고 미배정으로 반환한다.
    route_variant: 같은 조건에서 다른 후보 순서로 재탐색할 때 쓰는 번호.
    """
    remaining = points[:]
    routes = []

    guard = 0
    while remaining and guard < 500:
        if should_stop and should_stop():
            break
        guard += 1
        cur = station
        route = []
        acc_min = 0.0

        while remaining:
            if should_stop and should_stop():
                break
            # 1차: 직선거리로 후보 좁히기 → 2차: 좁혀진 후보만 실도로 거리/시간 확인
            candidates = nearest_by_straight_line(cur, remaining, candidate_k)
            legs = [(p, *real_leg(cur, p, on_call)) for p in candidates]
            legs.sort(key=(lambda t: t[2]) if basis == "time" else (lambda t: t[1]))
            if route_variant and len(legs) > 1:
                variant_window = min(len(legs), max(2, min(candidate_k or len(legs), 4)))
                offset = (int(route_variant) + guard + len(route)) % variant_window
                legs = legs[offset:variant_window] + legs[:offset] + legs[variant_window:]
            nxt, leg_km, leg_min = legs[0]

            # 노선의 첫 지점은 제한값을 적용하지 않는다.
            # (소방서에서 가장 가까운 대상까지의 거리가 이미 제한값보다 크면
            #  어떤 노선도 못 만들고 전부 '장거리'로 빠지는 문제를 막기 위함)
            first_stop = not route

            if mode == "fixed":
                if len(route) >= max_per_route:
                    break
            elif mode == "segment":
                if not first_stop and (leg_km > seg_max_km or leg_min > seg_max_min):
                    break
                if len(route) >= max_per_route:
                    break
            else:  # target_time
                back_km, back_min = real_leg(nxt, station, on_call)
                projected = acc_min + leg_min + service_min_per_stop + back_min
                if not first_stop and projected > target_min_high:
                    break
                if len(route) >= max_per_route:
                    break

            route.append(nxt)
            acc_min += leg_min + service_min_per_stop
            cur = nxt
            remaining.remove(nxt)

        if not route:
            # 어떤 조건도 만족 못하는 경우(예: 첫 지점부터 원거리) -> 강제 배정 방지, 장거리로 이관
            break
        routes.append(route)

        if max_routes_cap and len(routes) >= max_routes_cap and remaining:
            if not strict_route_cap:
                # 일반 순찰은 기존 동작 유지: 남은 지점을 마지막 노선에 이어붙인다.
                for p in remaining[:]:
                    route.append(p)
                    remaining.remove(p)
            break

    return routes, remaining


def allocate_hydrants_to_members(points, station, members):
    """개인별 개수 차이를 1개 이하로 유지하면서 인접 구역으로 배정한다.

    같은 차량의 팀원을 연속 배치한 뒤 센터 기준 방위각으로 정렬한 소화전을
    연속 구간으로 나눠, 같은 차량 팀원들의 담당 구역도 서로 가깝게 만든다.
    """
    if not points or not members:
        return points

    ordered_members = sorted(members, key=lambda m: (m["vehicle_no"], m["order"]))
    ordered_points = sorted(
        points,
        key=lambda p: (
            math.atan2(p["lat"] - station["lat"], p["lng"] - station["lng"]),
            haversine_km((station["lat"], station["lng"]), (p["lat"], p["lng"])),
        ),
    )
    base, extra = divmod(len(ordered_points), len(ordered_members))
    assigned = []
    cursor = 0
    for index, member in enumerate(ordered_members):
        count = base + (1 if index < extra else 0)
        for point in ordered_points[cursor:cursor + count]:
            assigned.append({
                **point,
                "assigned_to": member["name"],
                "vehicle_no": member["vehicle_no"],
            })
        cursor += count
    return assigned


def allocate_hydrants_by_distribution(points, station, vehicle_count, mode):
    """지리조사 대상을 사용자가 고른 기준으로 차량/팀에 먼저 배정한다."""
    vehicle_count = max(int(vehicle_count or 1), 1)
    if not points:
        return []
    if vehicle_count == 1:
        return [{**p, "vehicle_no": 1, "assigned_to": "1팀"} for p in points]

    enriched = []
    for point in points:
        straight_km = haversine_km((station["lat"], station["lng"]), (point["lat"], point["lng"]))
        est_km = straight_km * ROAD_FACTOR
        est_min = est_km / AVG_SPEED_KMH * 60
        angle = math.atan2(point["lat"] - station["lat"], point["lng"] - station["lng"])
        enriched.append({
            **point,
            "_straight_km": straight_km,
            "_est_km": est_km,
            "_est_min": est_min,
            "_angle": angle,
        })

    assigned = []
    if mode == "전체 개수 균등":
        ordered = sorted(enriched, key=lambda p: (p["_angle"], p["_straight_km"]))
        base, extra = divmod(len(ordered), vehicle_count)
        cursor = 0
        for vehicle_no in range(1, vehicle_count + 1):
            count = base + (1 if vehicle_no <= extra else 0)
            for point in ordered[cursor:cursor + count]:
                assigned.append({**point, "vehicle_no": vehicle_no, "assigned_to": f"{vehicle_no}팀"})
            cursor += count
    else:
        if mode == "거리 km 균등":
            weight_key = "_est_km"
        elif mode == "센터 가까운 곳 많이, 먼 곳 적게":
            weight_key = "_est_km"
        else:
            weight_key = "_est_min"

        buckets = [{"load": 0.0, "count": 0, "points": []} for _ in range(vehicle_count)]
        for point in sorted(enriched, key=lambda p: p[weight_key], reverse=True):
            bucket_index = min(
                range(vehicle_count),
                key=lambda idx: (buckets[idx]["load"], buckets[idx]["count"], idx),
            )
            buckets[bucket_index]["points"].append(point)
            buckets[bucket_index]["load"] += max(point[weight_key], 0.1)
            buckets[bucket_index]["count"] += 1

        for bucket_index, bucket in enumerate(buckets, start=1):
            for point in sorted(bucket["points"], key=lambda p: (p["_angle"], p["_straight_km"])):
                assigned.append({**point, "vehicle_no": bucket_index, "assigned_to": f"{bucket_index}팀"})

    for point in assigned:
        for private_key in ("_straight_km", "_est_km", "_est_min", "_angle"):
            point.pop(private_key, None)
    return assigned


def separate_long_distance(points, station, threshold_km, on_call=None, save_calls=True,
                           should_stop=None):
    """소방서에서 실도로거리가 기준을 넘는 대상을 분리한다.

    save_calls=True면 직선거리 추정값이 기준에서 충분히 멀리 떨어진(애매하지 않은)
    대상은 API를 호출하지 않고 추정값으로 판정해 호출 횟수를 줄인다.
    """
    normal, far = [], []
    for p in points:
        straight = haversine_km((station["lat"], station["lng"]), (p["lat"], p["lng"]))
        est = straight * ROAD_FACTOR

        if should_stop and should_stop():
            # 한도 초과 — 남은 대상은 추정값으로 분류하고 API 호출은 더 하지 않는다
            (far if est > threshold_km else normal).append(
                {**p, "도로거리_km": round(est, 1)} if est > threshold_km else p
            )
            continue

        if save_calls and est < threshold_km * 0.7:
            normal.append(p)          # 확실히 가까움 — API 호출 생략
            continue
        if save_calls and est > threshold_km * 1.5:
            far.append({**p, "도로거리_km": round(est, 1)})  # 확실히 멂 — 추정값 사용
            continue

        km, _ = real_leg(station, p, on_call)   # 애매한 구간만 실제 도로거리로 확인
        if km > threshold_km:
            far.append({**p, "도로거리_km": round(km, 1)})
        else:
            normal.append(p)
    return normal, far


def separate_long_time(points, station, threshold_min, delegate_to, on_call=None,
                       should_stop=None):
    """센터 기준 실제 편도시간으로 계절순찰 대상과 원거리 위임 대상을 나눈다."""
    normal, far = [], []
    for point in points:
        if should_stop and should_stop():
            straight_km = haversine_km(
                (station["lat"], station["lng"]), (point["lat"], point["lng"])
            )
            est_km = straight_km * ROAD_FACTOR
            est_min = est_km / AVG_SPEED_KMH * 60
            far.append({
                **point,
                "도로거리_km": round(est_km, 1),
                "편도시간_분": round(est_min),
                "권장수행": f"{delegate_to} (API 한도 도달로 재확인 필요)",
            })
            continue
        km, mins = real_leg(station, point, on_call)
        if mins > threshold_min:
            far.append({
                **point,
                "도로거리_km": round(km, 1),
                "편도시간_분": round(mins),
                "권장수행": delegate_to,
            })
        else:
            normal.append(point)
    return normal, far


# ----------------------------------------------------------------------------
# UI — 파세루 데모(웹 프로토타입)와 같은 카드+칩 스타일
# ----------------------------------------------------------------------------
PASERU_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700;800&family=Noto+Serif+KR:wght@600;700&family=IBM+Plex+Mono:wght@500;700&display=swap');

:root{
  --accent:#a33a3f; --accent-button:#b8464b; --accent-hover:#8f3035; --accent-soft:#f7e9ea;
  --navy:#17263a; --ink:#263442; --muted:#344054;
  --line:#cbd3dd; --divider:#d8dee7; --surface:#ffffff; --bg:#f7f8fa;
  --focus:#d8898d;
  color-scheme: light;   /* 휴대폰 다크모드에서도 밝은 화면으로 고정 */
}
.stApp{ background:var(--bg); color:var(--ink); font-size:14px; line-height:1.55; }

/* 휴대폰 다크모드에서 '흰 배경 + 흰 글씨'가 되는 문제를 막기 위해
   본문 글자색을 어두운 색으로 명시적으로 고정한다. */
.stApp, .stApp p, .stApp span, .stApp label, .stApp li, .stApp div,
.stMarkdown, .stMarkdown *, [data-testid="stWidgetLabel"] *,
[data-testid="stMetricLabel"] *, [data-testid="stMetricValue"],
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *{
  color:var(--ink);
}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *,
[data-testid="stMetricLabel"] *{ color: var(--muted) !important; }

/* 알림 박스(노란색·파란색 등) 안 글씨도 항상 검정 계열로 */
div[data-testid="stAlert"], div[data-testid="stAlert"] *,
div[data-testid="stNotification"], div[data-testid="stNotification"] *{
  color:var(--ink) !important;
}

/* 입력창·표를 밝은 배경 + 어두운 글씨로 고정 */
input, textarea, select,
[data-baseweb="input"] input, [data-baseweb="base-input"] input,
[data-baseweb="select"] div{
  background-color:#ffffff !important; color:var(--ink) !important;
  border-color:#aab4c1 !important;
}
[data-testid="stDataFrame"], [data-testid="stDataEditor"],
[data-testid="stTable"]{ background:#ffffff !important; }

html, body, [class*="css"], .stMarkdown, .stTextInput, .stNumberInput{
  font-family:'Noto Sans KR', -apple-system, 'Malgun Gothic', sans-serif;
}
h1, h2, h3, h4, h5, h6{
  font-family:'Noto Serif KR', serif !important; color:var(--navy) !important; font-weight:700 !important;
}
h1{ font-size:32px !important; }
h2{ font-size:24px !important; }
h3{ font-size:20px !important; }
.block-container{ padding-top: 2.2rem; max-width: 1180px; }

/* ---- 카드 컨테이너(border=True) ---- */
div[data-testid="stVerticalBlockBorderWrapper"]{
  background: var(--surface);
  border-radius: 14px !important;
  border:1px solid var(--line) !important;
  border-left:4px solid var(--accent) !important;
  box-shadow:0 2px 10px rgba(23,38,58,.06);
  padding:12px 18px 16px;
  margin-bottom:8px;
}

/* ---- 카드 제목 + 번호 뱃지 ---- */
.paseru-card-title{
  display:flex; align-items:center; gap:9px;
  font-family:'Noto Serif KR', serif; font-size:18px; font-weight:700; color:var(--navy);
  margin: 2px 0 10px;
}
.paseru-step{
  display:inline-flex; align-items:center; justify-content:center;
  width:23px; height:23px; border-radius:50%;
  background:var(--accent); color:#fff !important;
  font-family:'IBM Plex Mono', monospace; font-size:12px; font-weight:700; flex:none;
}
.paseru-sub{ font-weight:650; font-size:14px; color:#22324a; margin:14px 0 6px; }
.paseru-eyebrow{
  font-family:'IBM Plex Mono', monospace; font-size:12px; letter-spacing:.08em;
  text-transform:uppercase; color:var(--accent); font-weight:700; margin-bottom:2px;
}

/* ---- 선택 칩: 미선택도 선명한 테두리, 선택 시 차분한 딥 레드 ---- */
button[data-variant="pills"]{
  border-radius: 999px !important;
  border: 1px solid var(--line) !important;
  background:#f4f6f8 !important;
  color: var(--ink) !important;
  border-color:#aab4c1 !important;
  font-weight:600 !important;
  padding: 0.42em 1.05em !important;
  transition: background .12s, border-color .12s, color .12s;
}
button[data-variant="pills"]:hover{ background:#f7e9ea !important; border-color:var(--accent) !important; }
button[data-variant="pills"][data-selected="true"],
button[data-variant="pills"][aria-checked="true"],
button[data-variant="pills"][aria-pressed="true"]{
  background: var(--accent) !important;
  border-color: var(--accent) !important;
  color: #ffffff !important;
  font-weight: 700 !important;
  box-shadow:0 4px 12px -7px rgba(163,58,63,.42);
}
button[data-variant="pills"][data-selected="true"] *,
button[data-variant="pills"][aria-checked="true"] *,
button[data-variant="pills"][aria-pressed="true"] *{ color:#ffffff !important; }
div[data-testid="stButtonGroup"]{ gap: 8px !important; }

/* 인증 카드 제목은 모바일에서도 한 줄로 유지 */
.paseru-auth-title{
  margin:0 0 .55rem;
  color:var(--navy) !important;
  font-family:'Noto Serif KR', serif !important;
  font-size:clamp(1.16rem, 5.2vw, 1.9rem);
  font-weight:700;
  line-height:1.25;
  letter-spacing:-0.12em;
  white-space:nowrap;
}

/* ---- 버튼 ---- */
div.stButton > button, .stDownloadButton > button, div.stFormSubmitter > button{
  background-color:var(--accent-button) !important;
  color:#fff !important; border:none !important; border-radius:10px !important;
  font-weight:700 !important; padding:0.98em 1.1em !important;
  box-shadow:0 5px 14px -8px rgba(143,48,53,.48);
}
div.stButton > button:hover, .stDownloadButton > button:hover{ background-color: var(--accent-hover) !important; }
/* 폼 제출 전에도 입력 여부에 따라 즉시 반응하며 hover에서도 색을 유지한다. */
[data-testid="stForm"]:has(input[placeholder="비밀번호를 입력하세요"]) button[kind],
[data-testid="stForm"]:has(input[placeholder="비밀번호를 입력하세요"]) button[kind]:is(:hover, :focus, :active){
  background:#b8464b !important;
  border-color:#b8464b !important;
}
[data-testid="stForm"]:has(input[placeholder="비밀번호를 입력하세요"]:not(:placeholder-shown)) button[kind],
[data-testid="stForm"]:has(input[placeholder="비밀번호를 입력하세요"]:not(:placeholder-shown)) button[kind]:is(:hover, :focus, :active){
  background:#238553 !important;
  border-color:#238553 !important;
}
[data-testid="stForm"]:has(input[placeholder="비밀번호를 입력하세요"]) button[kind],
[data-testid="stForm"]:has(input[placeholder="비밀번호를 입력하세요"]) button[kind] *{
  color:#ffffff !important;
  -webkit-text-fill-color:#ffffff !important;
}
/* 전역 글자색 규칙보다 우선해 주요 빨간 버튼의 글자를 항상 흰색으로 표시 */
div.stButton > button:not([kind="secondary"]),
div.stButton > button:not([kind="secondary"]) *,
.stDownloadButton > button, .stDownloadButton > button *,
div.stFormSubmitter > button, div.stFormSubmitter > button *{
  color:#ffffff !important;
  -webkit-text-fill-color:#ffffff !important;
  opacity:1 !important;
}
div.stButton > button[kind="secondary"]{
  background:#ffffff !important; color:var(--ink) !important;
  border:1px solid #aab4c1 !important; box-shadow:none !important;
}
button:focus-visible, input:focus-visible, textarea:focus-visible,
[tabindex]:focus-visible{ outline:2px solid var(--focus) !important; outline-offset:2px; }

/* ---- 결과 영역 ---- */
div[data-testid="stExpander"]{
  border:1px solid var(--line) !important; border-radius:12px !important;
  background:var(--surface); overflow:hidden;
  box-shadow:0 1px 6px rgba(23,38,58,.04);
}
/* 긴 화면에서도 단계 탭을 잃지 않도록 상단에 고정 */
div[data-testid="stTabs"] [data-baseweb="tab-list"]{
  position:sticky; top:.25rem; z-index:50;
  background:rgba(255,255,255,.96); backdrop-filter:blur(8px);
  padding-top:.25rem;
}

/* ---- 대비 강화: 지표·표·라벨이 흐리게 보이지 않도록 ---- */
div[data-testid="stMetricValue"]{
  font-family:'IBM Plex Mono', monospace;
  color:#315d78 !important; font-weight:700 !important;
}
div[data-testid="stMetricLabel"], div[data-testid="stMetricLabel"] *{
  color:var(--ink) !important; font-weight:600 !important;
}
[data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label{
  color:var(--ink) !important; font-weight:600 !important;
}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *{
  color:var(--muted) !important;
  -webkit-text-fill-color:var(--muted) !important;
  opacity:1 !important;
  font-weight:500 !important;
}
/* 안내문·업로더 보조문이 연한 회색으로 흐려지지 않도록 대비 확보 */
.stApp small, .stApp small *,
[data-testid="stFileUploader"] small,
[data-testid="stFileUploader"] small *,
[data-testid="stFileUploaderDropzoneInstructions"],
[data-testid="stFileUploaderDropzoneInstructions"] *{
  color:var(--muted) !important;
  -webkit-text-fill-color:var(--muted) !important;
  opacity:1 !important;
}
/* 표(데이터프레임·편집표) 글씨와 테두리를 진하게 */
[data-testid="stDataFrame"] *, [data-testid="stDataEditor"] *{
  color:var(--ink) !important;
}
[data-testid="stDataFrame"], [data-testid="stDataEditor"]{
  border:1px solid var(--line) !important; border-radius:8px;
}
[data-testid="stDataFrame"] [role="columnheader"],
[data-testid="stDataEditor"] [role="columnheader"]{
  background:#eef2f6 !important; color:var(--navy) !important; font-weight:700 !important;
}
/* 알림 박스에 색 띠를 넣어 눈에 잘 띄게 */
div[data-testid="stAlert"]{
  border:1px solid #a9bfce !important;
  border-left:4px solid #557f99 !important; border-radius:10px !important;
  background:#eef4f8 !important;
}
/* 재난대응 활용범위 경고: 일반 안내와 혼동되지 않도록 전용 주황색 사용 */
.paseru-safety-warning{
  display:flex; align-items:flex-start; gap:14px;
  margin:.65rem 0 .8rem; padding:15px 17px;
  border:1.5px solid #e28713; border-left:7px solid #d96f00;
  border-radius:11px; background:#fff1d6;
  box-shadow:0 3px 10px rgba(217,111,0,.12);
  color:#4a2b00 !important;
}
.paseru-safety-warning .warning-icon{
  flex:none; font-size:34px; line-height:1; color:#d96f00 !important;
  margin-top:1px;
}
.paseru-safety-warning .warning-body,
.paseru-safety-warning .warning-body *{ color:#4a2b00 !important; }
.paseru-safety-warning .warning-title{
  display:block; margin-bottom:3px; font-size:16px; font-weight:800;
  color:#9a4700 !important;
}
.paseru-capacity-warning{
  border:2px solid #e05a16; border-left:9px solid #c93f12;
  background:#fff0dc;
  box-shadow:0 4px 12px rgba(201,63,18,.18);
}
.paseru-capacity-warning .warning-icon{
  color:#c93f12 !important; font-size:38px;
}
.paseru-capacity-warning .warning-title{
  color:#a52d0b !important; font-size:17px;
}
.paseru-capacity-warning .warning-count{
  color:#a52d0b !important; font-size:18px; font-weight:900;
}
.paseru-sub{ color:#22324a !important; }

/* 완료된 핵심 작업은 기존 실행 버튼 자리에 초록색 상태 버튼처럼 표시 */
.paseru-complete-action{
  width:100%; padding:.72rem 1rem; border-radius:10px;
  background:#2f6b49; border:1px solid #24563b;
  color:#ffffff !important; text-align:center; font-weight:750;
  box-shadow:0 4px 12px -8px rgba(36,86,59,.55);
}
.paseru-complete-action *{ color:#ffffff !important; }

/* ---- 내비게이션 버튼: 링크 기본색(파랑)에 밀리지 않도록 클래스로 고정 ---- */
a.paseru-navbtn, a.paseru-navbtn:link, a.paseru-navbtn:visited,
a.paseru-navbtn:hover, a.paseru-navbtn:active,
a.paseru-navbtn *{
  color:#ffffff !important;
  text-decoration:none !important;
}
a.paseru-navbtn{
  display:inline-block; padding:12px 16px; border-radius:10px;
  font-weight:700 !important; font-size:17px; margin:4px 8px 4px 0;
  box-shadow:0 3px 10px -4px rgba(0,0,0,.35);
}
a.paseru-navbtn.nav-and{ background:#03C75A !important; }   /* 안드로이드 (네이버 초록) */
a.paseru-navbtn.nav-ios{ background:#0a8f45 !important; }   /* 아이폰 */
a.paseru-navbtn.nav-pc{  background:#2563eb !important; }   /* PC 웹 (밝은 파랑) */

/* ---- 전체 글자 크기 약 20% 확대 ---- */
.stApp{ font-size:16.8px; }
h1{ font-size:38px !important; }
h2{ font-size:29px !important; }
h3{ font-size:24px !important; }
.paseru-card-title{ font-size:22px; }
.paseru-sub{ font-size:17px; }
.paseru-eyebrow, .paseru-step{ font-size:14px; }
.stApp p, .stApp label, .stApp li,
[data-testid="stWidgetLabel"] p,
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *,
div[data-testid="stMetricLabel"] *, div[data-testid="stMetricValue"],
div[data-testid="stExpander"] summary p,
[data-baseweb="tab"]{
  font-size:1rem !important;
}
button, input, textarea, select,
[data-baseweb="select"] div,
div.stButton > button, .stDownloadButton > button,
div.stFormSubmitter > button, [data-testid="stLinkButton"] a{
  font-size:1rem !important;
}
</style>
"""
st.markdown(PASERU_CSS, unsafe_allow_html=True)


def stop_label(name, address):
    """대상명 + 주소 표기. 이름 안에 이미 주소(또는 번지)가 들어 있으면 중복 표기하지 않는다."""
    name = (name or "").strip()
    address = (address or "").strip()
    if not address or address == name:
        return name
    # "경상북도 성주군 월항면 인촌1리 606-1" -> 뒤쪽 핵심부("인촌1리 606-1")가 이름에 있으면 생략
    tail = " ".join(address.split()[-2:])
    if tail and tail in name:
        return name
    if address in name:
        return name
    return f"{name} ({address})"


def build_distribution_map(coords_df, station=None):
    """좌표검색에 성공한 전체 대상을 한 화면에 보여주는 분포지도."""
    valid = coords_df.dropna(subset=["위도", "경도"]).copy()
    if valid.empty:
        return None

    center_lat = float(valid["위도"].astype(float).mean())
    center_lng = float(valid["경도"].astype(float).mean())
    distribution_map = folium.Map(location=[center_lat, center_lng], zoom_start=12)
    bounds = []

    for row_index, row in valid.iterrows():
        lat, lng = float(row["위도"]), float(row["경도"])
        visit_no = int(row_index) + 1 if isinstance(row_index, (int, float)) else len(bounds) + 1
        name = str(row.get("대상명", "")).strip()
        address = str(row.get("주소", "")).strip()
        bounds.append([lat, lng])
        folium.Marker(
            [lat, lng],
            tooltip=f"{visit_no}. {name}",
            popup=folium.Popup(html.escape(stop_label(name, address)), max_width=360),
            icon=folium.DivIcon(
                icon_size=(30, 30), icon_anchor=(15, 15),
                html=(
                    '<div style="background:#1f6fb2;color:#ffffff;'
                    'width:26px;height:26px;border-radius:50%;border:2px solid #ffffff;'
                    'box-shadow:0 1px 5px rgba(0,0,0,.45);display:flex;align-items:center;'
                    'justify-content:center;font-family:sans-serif;font-weight:700;font-size:12px;'
                    f'line-height:1;">{visit_no}</div>'
                ),
            ),
        ).add_to(distribution_map)

    if station and station.get("lat") is not None and station.get("lng") is not None:
        station_lat, station_lng = float(station["lat"]), float(station["lng"])
        bounds.append([station_lat, station_lng])
        folium.Marker(
            [station_lat, station_lng],
            tooltip=f"출발지: {station.get('name', '')}",
            icon=folium.DivIcon(
                icon_size=(66, 28), icon_anchor=(33, 14),
                html=(
                    '<div style="background:#a33a3f;color:#ffffff;padding:4px 9px;'
                    'border-radius:14px;border:2px solid #ffffff;box-shadow:0 1px 5px rgba(0,0,0,.45);'
                    'text-align:center;font-family:sans-serif;font-weight:700;font-size:12px;'
                    'line-height:1.2;white-space:nowrap;">🚒 출발</div>'
                ),
            ),
        ).add_to(distribution_map)

    if len(bounds) > 1:
        distribution_map.fit_bounds(bounds, padding=(30, 30))
    return distribution_map


def manual_map_start(coords_df, failed_address, station_lat=None, station_lng=None):
    """실패 주소의 도로·읍면동·시군구와 같은 확인 좌표를 찾아 지도 시작점을 정한다."""
    valid = coords_df.dropna(subset=["위도", "경도"]).copy()
    normalized_address = re.sub(r"\s+", " ", str(failed_address or "")).strip()
    address_tokens = normalized_address.split()
    road_tokens = list(dict.fromkeys(
        [token for token in address_tokens if token.endswith(("로", "길"))]
        + re.findall(r"[가-힣0-9]+(?:로|길)(?=\s*\d|\s|$)", normalized_address)
    ))
    search_levels = [
        ("도로명", road_tokens, 15),
        ("읍·면·동", [token for token in address_tokens if token.endswith(("읍", "면", "동", "리"))], 14),
        ("시·군·구", [token for token in address_tokens if token.endswith(("시", "군", "구"))], 12),
    ]
    broad_search_queries = []
    for level_name, tokens, zoom in search_levels:
        for token in reversed(tokens):
            token_index = normalized_address.find(token)
            if token_index >= 0:
                query = normalized_address[:token_index + len(token)]
            else:
                query = " ".join(address_tokens[: address_tokens.index(token) + 1]) if token in address_tokens else token
            query = re.sub(r"\s+", " ", query).strip()
            if query and all(query != existing[0] for existing in broad_search_queries):
                broad_search_queries.append((query, token, level_name, zoom))

    if not valid.empty:
        valid_addresses = valid["주소"].fillna("").astype(str)
        for level_name, tokens, zoom in search_levels:
            for token in reversed(tokens):
                matched = valid[valid_addresses.str.contains(re.escape(token), regex=True)]
                if not matched.empty:
                    return (
                        float(matched["위도"].astype(float).mean()),
                        float(matched["경도"].astype(float).mean()),
                        zoom,
                        f"{token} 주변({level_name} 일치 대상 기준)",
                    )

        return (
            float(valid["위도"].astype(float).mean()),
            float(valid["경도"].astype(float).mean()),
            12,
            "좌표 확인 대상의 전체 분포 중심",
        )

    for query, token, level_name, zoom in broad_search_queries:
        lat, lng, status = geocode_once(query)
        if status == "ok":
            return lat, lng, zoom, f"{token} 주변({level_name} 주소 기준)"

    if station_lat is not None and station_lng is not None:
        return float(station_lat), float(station_lng), 12, "출발지 주변"
    return 36.0, 128.0, 7, "대한민국 중심"


def add_manual_location_layer_buttons(map_obj, satellite_layer, normal_layer, road_layer, label_layer):
    """위성찾기 지도에서 현장 사용자가 보기 방식을 크게 바꿀 수 있게 한다."""
    map_name = map_obj.get_name()
    satellite_name = satellite_layer.get_name()
    normal_name = normal_layer.get_name()
    road_name = road_layer.get_name()
    label_name = label_layer.get_name()
    control_style = """
    <style>
      .manual-map-switch {
        background: rgba(255,255,255,.96);
        border: 1px solid #9aa7b3;
        border-radius: 10px;
        box-shadow: 0 2px 10px rgba(0,0,0,.22);
        padding: 7px;
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .manual-map-switch button {
        appearance: none;
        border: 1px solid #6f7d8a;
        border-radius: 8px;
        background: #ffffff;
        color: #1f2d3d;
        font-family: Arial, 'Noto Sans KR', sans-serif;
        font-size: 14px;
        font-weight: 800;
        line-height: 1.2;
        padding: 9px 10px;
        min-width: 118px;
        cursor: pointer;
      }
      .manual-map-switch button.active {
        background: #1f6fb2;
        border-color: #18598f;
        color: #ffffff;
      }
    </style>
    """
    control_script = f"""
    <script>
      (function() {{
        var map = {map_name};
        var satellite = {satellite_name};
        var normal = {normal_name};
        var roads = {road_name};
        var labels = {label_name};
        var buttons = {{}};

        function setActive(mode) {{
          Object.keys(buttons).forEach(function(key) {{
            buttons[key].classList.toggle('active', key === mode);
          }});
        }}

        function setManualMapMode(mode) {{
          if (map.hasLayer(normal)) map.removeLayer(normal);
          if (map.hasLayer(satellite)) map.removeLayer(satellite);
          if (map.hasLayer(roads)) map.removeLayer(roads);
          if (map.hasLayer(labels)) map.removeLayer(labels);

          if (mode === 'normal') {{
            map.addLayer(normal);
          }} else {{
            map.addLayer(satellite);
            if (mode === 'hybrid') {{
              map.addLayer(roads);
              map.addLayer(labels);
            }}
          }}
          setActive(mode);
        }}

        var Control = L.Control.extend({{
          options: {{ position: 'topright' }},
          onAdd: function() {{
            var box = L.DomUtil.create('div', 'manual-map-switch');
            L.DomEvent.disableClickPropagation(box);
            [
              ['hybrid', '위성+도로명'],
              ['normal', '일반지도'],
              ['satellite', '위성만 보기']
            ].forEach(function(item) {{
              var button = L.DomUtil.create('button', '', box);
              button.type = 'button';
              button.textContent = item[1];
              buttons[item[0]] = button;
              L.DomEvent.on(button, 'click', function(event) {{
                L.DomEvent.stop(event);
                setManualMapMode(item[0]);
              }});
            }});
            return box;
          }}
        }});
        map.addControl(new Control());
        setManualMapMode('hybrid');
      }})();
    </script>
    """
    map_obj.get_root().html.add_child(Element(control_style))
    map_obj.get_root().script.add_child(Element(control_script))


def kakao_url(name, lat, lng):
    """카카오맵 길안내 링크 (공백·괄호가 있어도 깨지지 않도록 인코딩)."""
    return ("https://map.kakao.com/link/to/"
            f"{quote(str(name), safe='')},{lat},{lng}")


KAKAO_MAX_VIA = 5  # 카카오맵 자동차 길찾기 URL이 지원하는 경유지 최대 개수


def kakao_route_url(origin, destinations):
    """카카오맵 자동차 길찾기 링크를 만든다.

    origin은 출발지, destinations의 마지막 항목은 목적지이며 그 앞 항목은
    경유지로 전달된다. destinations는 최대 6개(경유지 5 + 목적지)다.
    """
    if not destinations:
        return ""

    def place(p):
        name = quote(str(p["name"]), safe="")
        return f"{name},{float(p['lat']):.7f},{float(p['lng']):.7f}"

    points = [origin] + list(destinations)
    return "https://map.kakao.com/link/by/car/" + "/".join(place(p) for p in points)


def kakao_route_links(station, legs):
    """소방서 → 경유지 순서 → 소방서로 돌아오는 카카오맵 링크 목록.

    경유지가 5개를 넘는 긴 노선은 앞 구간의 마지막 목적지를 다음 구간의
    출발지로 이어서 분할한다.
    반환: [(URL, 출발지, 구간 목적지 목록), ...]
    """
    stops = [{"name": lg["to"], "lat": lg["lat"], "lng": lg["lng"]} for lg in legs]
    if not stops:
        return []

    remaining = stops + [station]
    origin = station
    links = []
    max_destinations = KAKAO_MAX_VIA + 1
    while remaining:
        destinations = remaining[:max_destinations]
        links.append((kakao_route_url(origin, destinations), origin, destinations))
        remaining = remaining[max_destinations:]
        if remaining:
            origin = destinations[-1]
    return links


def make_qr_png(data):
    """링크를 휴대폰으로 넘길 수 있는 QR코드 PNG 바이트로 만든다."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def build_qr_zip(station, route_results):
    """모든 노선의 카카오맵 QR PNG와 경로 목록 엑셀을 ZIP으로 묶는다."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for rr in route_results:
            links = kakao_route_links(station, rr["legs"])
            for li, (url, origin, destinations) in enumerate(links, start=1):
                suffix = "" if len(links) == 1 else f"_구간{li}"
                zf.writestr(f"노선_{rr['route_no']}{suffix}_QR.png", make_qr_png(url))
        zf.writestr("노선별_경로와_링크.xlsx", build_route_links_excel(station, route_results))
    return buffer.getvalue()


def build_route_links_excel(station, route_results):
    """노선 순서와 클릭 가능한 카카오맵 링크를 엑셀로 만든다."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "노선별 경로와 링크"
    ws.sheet_view.showGridLines = False

    headers = ["노선", "구간", "출발지", "경유지 및 목적지 순서", "거리(km)", "시간(분)", "카카오맵"]
    ws.append(headers)

    for rr in route_results:
        links = kakao_route_links(station, rr["legs"])
        for li, (url, origin, destinations) in enumerate(links, start=1):
            sequence = " → ".join([origin["name"]] + [p["name"] for p in destinations])
            ws.append([
                rr["route_no"],
                li if len(links) > 1 else 1,
                origin["name"],
                sequence,
                round(rr["total_km"], 1),
                round(rr["total_min"]),
                "카카오맵에서 열기",
            ])
            link_cell = ws.cell(row=ws.max_row, column=7)
            link_cell.hyperlink = url
            link_cell.style = "Hyperlink"

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    thin = Side(style="thin", color="D9D9D9")
    bottom_border = Border(bottom=thin)

    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.border = bottom_border
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        row[0].alignment = Alignment(horizontal="center", vertical="center")
        row[1].alignment = Alignment(horizontal="center", vertical="center")
        row[4].alignment = Alignment(horizontal="right", vertical="center")
        row[5].alignment = Alignment(horizontal="right", vertical="center")
        row[6].alignment = Alignment(horizontal="center", vertical="center")

    widths = {"A": 9, "B": 9, "C": 20, "D": 75, "E": 13, "F": 13, "G": 19}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    ws.row_dimensions[1].height = 26
    for row_no in range(2, ws.max_row + 1):
        ws.row_dimensions[row_no].height = 42
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_upload_template():
    """대상 목록을 일정한 열 이름으로 작성할 수 있는 빈 엑셀 양식."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "대상목록"
    ws.sheet_view.showGridLines = False

    headers = ["연번", "대상명", "주소", "비고", "위도(선택)", "경도(선택)"]
    ws.append(headers)

    header_fill = PatternFill("solid", fgColor="1F4E78")
    required_fill = PatternFill("solid", fgColor="FFF2CC")
    thin = Side(style="thin", color="D9D9D9")
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # 첫 입력행은 비워두되 필수 입력칸을 연한 노랑으로 표시한다.
    for row_no in range(2, 102):
        for col_no in range(1, 7):
            cell = ws.cell(row=row_no, column=col_no)
            cell.border = Border(bottom=thin)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.cell(row=row_no, column=2).fill = required_fill
        ws.cell(row=row_no, column=3).fill = required_fill

    widths = {"A": 9, "B": 28, "C": 52, "D": 28, "E": 16, "F": 16}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    ws.row_dimensions[1].height = 27
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = "A1:F101"

    guide = wb.create_sheet("작성안내")
    guide.sheet_view.showGridLines = False
    guide.append(["항목", "필수 여부", "작성 방법"])
    guide_rows = [
        ["대상명", "필수", "시설명 또는 점검 대상명을 입력합니다."],
        ["주소", "필수", "지오코딩할 도로명주소 또는 지번주소를 입력합니다."],
        ["연번", "선택", "자동으로 표시됩니다. 직접 수정해도 됩니다."],
        ["비고", "선택", "노선 편성에 필요한 일반 참고사항만 입력합니다."],
        ["위도·경도", "선택", "이미 검증한 좌표가 있을 때만 입력합니다. 없으면 비워두세요."],
        ["개인정보", "입력 금지", "성명, 전화번호, 주민등록번호, 검사결과 등은 입력하지 않습니다."],
    ]
    for row in guide_rows:
        guide.append(row)
    for cell in guide[1]:
        cell.fill = header_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in guide.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            cell.border = Border(bottom=thin)
    guide.column_dimensions["A"].width = 18
    guide.column_dimensions["B"].width = 14
    guide.column_dimensions["C"].width = 72
    guide.freeze_panes = "A2"

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_printable_qr_html(station, route_results, meta):
    """브라우저에서 열어 A4로 인쇄할 수 있는 노선별 QR 문서를 만든다."""
    cards = []
    title = html.escape(str(meta.get("title") or "순찰노선"))
    period = html.escape(str(meta.get("period") or ""))

    for rr in route_results:
        team_name = html.escape(st.session_state.get(f"team_name_{rr['route_no']}", ""))
        team_members = html.escape(st.session_state.get(f"team_members_{rr['route_no']}", ""))
        qr_blocks = []
        links = kakao_route_links(station, rr["legs"])
        for li, (url, origin, destinations) in enumerate(links, start=1):
            suffix = "" if len(links) == 1 else f" {li}/{len(links)}구간"
            seq = " → ".join([origin["name"]] + [p["name"] for p in destinations])
            qr_b64 = base64.b64encode(make_qr_png(url)).decode("ascii")
            qr_blocks.append(
                f'<section class="qr-block"><h2>노선 {rr["route_no"]}{suffix}</h2>'
                f'<img src="data:image/png;base64,{qr_b64}" alt="노선 QR코드">'
                f'<p class="scan">휴대폰 카메라로 스캔하면 카카오맵 전체 코스가 열립니다.</p>'
                f'<p class="sequence">{html.escape(seq)}</p></section>'
            )
        people = ""
        if team_name or team_members:
            people = f'<p class="people">담당 조 {team_name or "-"}　 조원 {team_members or "-"}</p>'
        cards.append(
            f'<article class="route"><header><div>{title}</div><strong>노선 {rr["route_no"]}</strong>'
            f'<span>{len(rr["stops"])}개소 · {rr["total_km"]:.1f}km · 약 {rr["total_min"]:.0f}분</span>'
            f'</header>{people}{"".join(qr_blocks)}</article>'
        )

    return f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} QR 인쇄</title>
<style>
@page {{ size: A4; margin: 14mm; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; color: #111827; font-family: "Malgun Gothic", "Apple SD Gothic Neo", sans-serif; }}
.print-button {{ position: fixed; right: 18px; top: 18px; padding: 12px 18px; border: 0; border-radius: 8px;
  background:#a33a3f; color:white; font-size:16px; font-weight:700; cursor:pointer; }}
.route {{ min-height: 267mm; page-break-after: always; text-align: center; padding: 8mm 5mm; }}
.route:last-child {{ page-break-after: auto; }}
header div {{ font-size: 17px; margin-bottom: 8px; }}
header strong {{ display: block; font-size: 28px; margin-bottom: 6px; }}
header span, .people {{ font-size: 14px; color: #4b5563; }}
.people {{ margin: 10px 0; }}
.qr-block {{ margin-top: 18px; }}
.qr-block h2 {{ font-size: 20px; margin: 0 0 8px; }}
.qr-block img {{ width: 88mm; max-width: 82vw; height: auto; }}
.scan {{ font-size: 14px; font-weight: 700; margin: 4px 0 12px; }}
.sequence {{ font-size: 15px; line-height: 1.7; overflow-wrap: anywhere; border-top: 1px solid #d1d5db;
  padding-top: 12px; margin: 0 auto; max-width: 170mm; }}
.period {{ text-align: center; color: #4b5563; margin: 0 0 8px; }}
@media print {{ .print-button {{ display: none; }} }}
</style></head><body>
<button class="print-button" onclick="window.print()">🖨 인쇄하기</button>
<p class="period">{period}</p>{"".join(cards)}
</body></html>'''.encode("utf-8")


def build_center_route_print_html(station, route_results, meta):
    """센터 지리조사용: 지도·방문순서·확인란을 한 문서로 만든다."""
    title = html.escape(str(meta.get("title") or "센터 지리조사 노선 결과"))
    pages, map_scripts = [], []

    for rr in route_results:
        route_no = rr["route_no"]
        map_id = f"route_map_{route_no}"
        team_name = st.session_state.get(f"team_name_{route_no}", "")
        auto_members = ", ".join(rr.get("assigned_members") or [])
        team_members = st.session_state.get(f"team_members_{route_no}", "") or auto_members
        vehicle = f"{rr.get('vehicle_no')}호차" if rr.get("vehicle_no") else ""

        stop_rows = []
        for index, leg in enumerate(rr["legs"], start=1):
            assignee = leg.get("assigned_to") or ""
            stop_rows.append(
                '<li><span class="order">{}</span><div><strong>{}</strong><small>{}{}</small></div>'
                '<span class="check">□</span></li>'.format(
                    index,
                    html.escape(str(leg["to"])),
                    html.escape(str(leg.get("to_address") or "")),
                    f" · 담당 {html.escape(str(assignee))}" if assignee else "",
                )
            )

        people = " · ".join(v for v in (vehicle, team_name, team_members) if v)
        pages.append(f'''
<section class="route-page">
  <header>
    <p class="doc-title">{title}</p>
    <div class="route-title"><strong>노선 {route_no}</strong>
      <span>{len(rr['stops'])}개소 · 총 {rr['total_km']:.1f}km · 약 {rr['total_min']:.0f}분</span>
    </div>
    <p class="team">{html.escape(people)}</p>
  </header>
  <div class="route-body">
    <div id="{map_id}" class="route-map"></div>
    <div class="stops"><h2>방문 순서</h2><ol>{''.join(stop_rows)}</ol></div>
  </div>
  <footer>출발·복귀: {html.escape(station['name'])}　　확인자: ____________________</footer>
</section>''')

        path = rr.get("path") or [[station["lat"], station["lng"]]]
        markers = [{"lat": station["lat"], "lng": station["lng"], "label": "출발·복귀"}]
        markers += [
            {"lat": leg["lat"], "lng": leg["lng"], "label": f"{i}. {leg['to']}"}
            for i, leg in enumerate(rr["legs"], start=1)
        ]
        map_scripts.append(f'''
const map{route_no}=L.map('{map_id}',{{zoomControl:true}});
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'© OpenStreetMap'}}).addTo(map{route_no});
const path{route_no}={json.dumps(path, ensure_ascii=False)};
const line{route_no}=L.polyline(path{route_no},{{color:'#a33a3f',weight:5}}).addTo(map{route_no});
{json.dumps(markers, ensure_ascii=False)}.forEach((p,i)=>L.marker([p.lat,p.lng]).addTo(map{route_no}).bindTooltip(p.label));
map{route_no}.fitBounds(line{route_no}.getBounds(),{{padding:[20,20]}});
''')

    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
@page {{ size:A4 landscape; margin:12mm; }} *{{box-sizing:border-box}}
body{{margin:0;color:#17263a;font-family:"Malgun Gothic",sans-serif;background:#eef1f4}}
.print-button{{position:fixed;right:18px;top:18px;z-index:9999;padding:12px 20px;border:0;border-radius:9px;background:#a33a3f;color:#fff;font-weight:700;cursor:pointer}}
.route-page{{width:273mm;min-height:186mm;margin:10mm auto;padding:8mm;background:#fff;page-break-after:always}}
.route-page:last-of-type{{page-break-after:auto}} .doc-title{{margin:0;text-align:center;font-size:17px}}
.route-title{{display:flex;align-items:baseline;gap:18px;border-bottom:3px solid #a33a3f;padding:5px 0 8px}}
.route-title strong{{font-size:27px}} .route-title span{{font-size:16px;font-weight:700}} .team{{height:20px;margin:7px 0;color:#4b5563}}
.route-body{{display:grid;grid-template-columns:58% 42%;gap:8mm;height:130mm}} .route-map{{width:100%;height:100%;border:1px solid #9aa5b1}}
.stops{{overflow:hidden}} .stops h2{{font-size:18px;margin:0 0 7px}} ol{{list-style:none;padding:0;margin:0}}
li{{display:grid;grid-template-columns:28px 1fr 28px;align-items:center;gap:7px;border-bottom:1px solid #d8dee7;padding:6px 2px}}
.order{{display:flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:50%;background:#315d78;color:#fff;font-weight:700}}
li strong{{display:block;font-size:14px}} li small{{display:block;color:#4b5563;font-size:10px;margin-top:2px}} .check{{font-size:25px;text-align:center}}
footer{{margin-top:7px;padding-top:5px;border-top:1px solid #9aa5b1;font-size:12px;color:#4b5563}}
@media print{{body{{background:#fff}}.print-button{{display:none}}.route-page{{margin:0;box-shadow:none}}}}
</style></head><body><button class="print-button" onclick="window.print()">🖨 인쇄하기</button>
{''.join(pages)}<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>{''.join(map_scripts)}</script>
</body></html>'''.encode("utf-8")


def card_title(step, text):
    st.markdown(
        f'<div class="paseru-card-title"><span class="paseru-step">{step}</span>{text}</div>',
        unsafe_allow_html=True,
    )


def sub_label(text):
    st.markdown(f'<div class="paseru-sub">{text}</div>', unsafe_allow_html=True)


def safety_warning(text, title="주의 · 활용 범위 안내"):
    """재난대응 활용범위를 일반 안내와 구분해 보여주는 전용 경고 상자."""
    st.markdown(
        '<div class="paseru-safety-warning">'
        '<div class="warning-icon" aria-hidden="true">⚠</div>'
        f'<div class="warning-body"><span class="warning-title">{html.escape(title)}</span>'
        f'{html.escape(text)}</div></div>',
        unsafe_allow_html=True,
    )


def inspection_capacity_warning(capacity_formula, total_targets, omitted_targets):
    """예방검사 처리용량 부족을 성공 안내와 확실히 구분해 표시한다."""
    st.markdown(
        '<div class="paseru-safety-warning paseru-capacity-warning">'
        '<div class="warning-icon" aria-hidden="true">⚠</div>'
        '<div class="warning-body"><span class="warning-title">검사기간 내 미완료 예상</span>'
        f'{html.escape(capacity_formula)}<br>'
        f'전체 {int(total_targets)}개소 중 '
        f'<span class="warning-count">{int(omitted_targets)}개소 누락 예정</span>입니다.<br>'
        '검사 대상 수 또는 검사일수를 조정하세요.</div></div>',
        unsafe_allow_html=True,
    )



SOCIAL_PREVIEW_TITLE = "파세루 오리진 (FireSafe Route Origin)"
SOCIAL_PREVIEW_DESCRIPTION = "방문·점검·순찰 주소 목록을 올리면 내 핸드폰 카카오맵으로 온다"
SOCIAL_PREVIEW_IMAGE = "data:image/jpeg;base64,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VERBqnNBfOomwRQEREU1REWQJyROSIIiIHJCl9E00QEUTS3gQVE0uppZBUU0VQEU07E07EBX02U0TRA9NkPpoickF17VB6aJpdNLIGqemyKIL6bJ6bIogvpsnpsmiIHLbyJz+ZTTrVQPTZOzyJomiB6bKbq213UQUbqdaIgpCdaiICvNREBERA5qqIgJzREAqqdqICIiAiIga9qJ2ogaoiICIiAiJzQETYIgIoqeSAoqogJyRXkgKIqgckU5KoIqol/gQETS6mlkFRNE60BE0snNAROSGyAimiaIKoPTRNLppZBdUUTRBR6aJrdRNLoKdvmT02URA9Nk5/MmnWiB6bJ2eRNE0QX02U5beRE0+dBefzKemyCyIHYnWiiCpzRRBbaJ1IogvNOSiIKiiIKUTtRARQIgqiIgInPdEDVERATVCmiD12IvaSb9x+1erpXtdL+4C9piL2km/cftC9XSva6X9wFnOk/X7m7rNqwbus2rGq8g5Ku3UHJV26QZKt2UVbspRV1CzL+v+v8AvyJ5129XULMv6/6/78iedEr5pEREd4kOyIdkZMVj9sslj9sr+Ad14zsvId14zsoMHbLwxPYu8RXmdsvDE9i7xFZQrPBPtQ/787zBfQc18/gn2of9+d5gvoOe6wvZj0ck9N05IsWR6bp6bpdEQv6XS/pdEuUC+vzpdLogX1+dPTdOaXRS+yl/S6t9kVRE5pa6vNAupdW+idSgKclVOSKt060PjUuqhdES6AiJdEOaJdFQROaclATmiICIiAiiqAiKIKoiICIioKqIgKqIoCIiKIiKgiIqgnJE5ICIUQETtRARE7UBERARFLoL6bopfwpz3QOXzonLdO1A9N09N0QoKp6bodEugem6X9LpdL6boF/S6em6X1S5QL7Jf0ul0BQOSX1+dLlLoChKXKvUgKcleanJBbonPdTrQL3Vuh3S6CXREKAnJW6iAic0ugckS6IHJEKa3QEREBERA5oiICIiAiIgIiICIiAiiICKqIKnJE5ICiqiC8kuoiAmyJ2oCInagInaiBdE7UQLol/Cnagem6em6IUBPTdO1RBUTnunagem6npuiXQL+l1b+l1LoCgclb6/Opcq3QS6XS/hS6C31+dS9gl9UuUC6JzTrQL3S6HdOtAul05JzQCdEuimyCqK80ugnJOat0QRFSSl0EREQXkoiIHNERAREQEREHrsRe0k37j9q9XSva6X9wF7TEXtJN+4/aF6ule10v7gLOdJ+v3N3WbVg3dZtWNV5ByVduoOSrt0gyVbsoq3ZSirqFmX9f8AX/fkTzrt6uoWZf1/1/35E86JXzSIiI7xIdkQ7IyYrH7ZZLH7ZX8A7rxnZeQ7rxnZQYO2Xhiexd4ivM7ZeGJ7F3iKyhWeCfad/wB+d5gvoF8/gn2of9+d5gvoOfzLC9mPRZE7E9NliyLJZOzyJ6bIATqT02Ts8iIc05J6bJ2IFlE7EGiottlAnUqoImycleaKllRyROpEROSqiqhTmic0Q0TqSyWQEKIiHnREQETnsnJQES2yc1QROSW1QERRAREQFSoqgiIioFFVEBERRRE5IqgiIVQ7EREBPAnNEBOaIgdidiIgJ2IiAiKIFvAiemyvpsgm6Jy+ZXn8yCbInL5k9NkBLJ2eROzyIFksnZ5E7PIgc/CnLwJ2eROW3kQOfhROzyJ2eRAROzyJ2eRA8yJy2TnsgWURVBOaclexTkgW1RXsUugFERACFEQXzqeZLIgc0REBERAROSW1QEROpARLIgIiICIiAUREBOaIgckREEVUVKB2KKqIKnJCnJAUVUQE7ERATsREBERASyIgJbVPTZOaCbIry+ZPTZBEV6/iTl8yCeHkivNOW3kQRLJ2eROzyIHPwor2eRTs8iAivZ5E7PIgnLwInZ5FezyIImyvLbyKdiAnJOxOSBbVRXsRAKIiAoRZVRBUSyIHNESyAltUUQU2soioQRETqQESyICK81OSAiIgIiIPXYi9pJv3H7V6ule10v7gL2mIvaSb9x+1erpXtdL+4CznSfr9zd1m1YN3WbVjVeQclXbqDkq7dIMlW7KKt2Uoq6hZl/X/AF/35E867erqFmX9f9f9+RPOiV80iIiO8SHZERkxWP2yIr+Ad14zsiKDB2y8MT2LvEURZQrPBPtQ/wC/O8wX0FkRYXtceiyWRFipZLIiJABLIiKWUIRERbJa6IqpbZLIiVCyW1RFFCNCpbZEVYrZSyIilk5oiBZAiIhZLIiARoUtpdEQLaoiIFksiIIrbVEQRERAVREgiHVEWQqiIoG6IiAlkRQERFkCIiAlkRAslkRAslkRAslkRAslroiBZSyIgvRTooiCdFXo6oiCdHROiiIFvElvEiIFt0siIFtUtoiIFtUsiIFtksiIACW1REC2hS2yIgW1KW0REAiylkRAsiIgWQoiC2UtZEQLIRYIiBbbwpZEQEtqiICIiAiIgIiICIiBuiIgbIiIIiIgqIiAoiIKnJEQQhLIiBZLIiBZLIiBbVLIiBa6WRECydHVEQOjonRREDop0URA6KdFEQS3iVtoiIFtVLIiC22QBEQSypCIglkA2REC2pS2iIgttVLboiARqltURAspZEQWyWsiIFksiICWREEIsrbWyIgiIiAiIgW1REQEREBERB67EXtJN+4/avV0r2ul/cBEWc6T9fubus2oixqvIOSrt0RIMlW7IilFXULMv6/6/wC/InnRESvmkRER/9k="


def inject_social_preview_meta():
    """카카오톡 등 링크 공유 미리보기에 쓰일 제목·설명·이미지를 부모 문서에 주입한다."""
    title = json.dumps(SOCIAL_PREVIEW_TITLE, ensure_ascii=False)
    description = json.dumps(SOCIAL_PREVIEW_DESCRIPTION, ensure_ascii=False)
    image_url = json.dumps(SOCIAL_PREVIEW_IMAGE)
    app_url = json.dumps(APP_PUBLIC_URL + "/")
    components.html(
        f"""
<script>
try {{
  const d = window.parent.document;
  const setMeta = (key, value, attr = 'property') => {{
    let el = d.head.querySelector('meta[' + attr + '="' + key + '"]');
    if (!el) {{
      el = d.createElement('meta');
      el.setAttribute(attr, key);
      d.head.appendChild(el);
    }}
    el.setAttribute('content', value);
  }};

  d.title = {title};
  setMeta('description', {description}, 'name');
  setMeta('og:type', 'website');
  setMeta('og:site_name', '파세루 오리진');
  setMeta('og:title', {title});
  setMeta('og:description', {description});
  setMeta('og:url', {app_url});
  setMeta('og:image', {image_url});
  setMeta('og:image:secure_url', {image_url});
  setMeta('og:image:type', 'image/jpeg');
  setMeta('og:image:width', '1536');
  setMeta('og:image:height', '774');
  setMeta('twitter:card', 'summary_large_image', 'name');
  setMeta('twitter:title', {title}, 'name');
  setMeta('twitter:description', {description}, 'name');
  setMeta('twitter:image', {image_url}, 'name');
}} catch (e) {{ /* 공유 메타 주입 실패 시 앱 실행에는 영향 없음 */ }}
</script>
""",
        height=0,
    )


inject_social_preview_meta()


# ---- PWA: 홈 화면에 앱처럼 추가할 수 있도록 매니페스트를 부모 문서에 주입(가능한 환경에서) ----
components.html(
    """
<script>
try {
  const d = window.parent.document;
  if (d && !d.getElementById('paseru-manifest')) {
    const manifest = {
      name: "파세루 오리진 - 순찰노선 설계기",
      short_name: "파세루",
      description: "AI 기반 소방 순찰노선 최적화 서비스",
      start_url: ".", scope: ".", display: "standalone",
      background_color: "#f7f8fa", theme_color: "#a33a3f",
      icons: []
    };
    const link = d.createElement('link');
    link.id = 'paseru-manifest';
    link.rel = 'manifest';
    link.href = 'data:application/manifest+json,' + encodeURIComponent(JSON.stringify(manifest));
    d.head.appendChild(link);
    const meta = d.createElement('meta');
    meta.name = 'apple-mobile-web-app-capable'; meta.content = 'yes';
    d.head.appendChild(meta);
    const theme = d.createElement('meta');
    theme.name = 'theme-color'; theme.content = '#a33a3f';
    d.head.appendChild(theme);
  }
} catch (e) { /* 환경상 주입이 막히면 조용히 무시 */ }
</script>
""",
    height=0,
)

st.markdown(
    """
    <style>
      .paseru-login-hero {
        margin: 0.15rem 0 1.25rem;
        padding: 1.05rem 1.05rem 1.15rem;
        border: 1px solid #d4dbe5;
        border-radius: 14px;
        background:
          radial-gradient(circle at 88% 18%, rgba(239, 78, 78, .13), transparent 30%),
          linear-gradient(135deg, #ffffff 0%, #f8fafc 100%);
        box-shadow: 0 5px 18px rgba(23, 38, 58, .07);
        overflow: hidden;
      }
      .paseru-login-kicker {
        color: #a33a3f;
        font-size: clamp(1.02rem, 3.8vw, 1.18rem);
        font-weight: 750;
        line-height: 1.45;
        word-break: keep-all;
      }
      .paseru-login-title {
        margin-top: .28rem;
        color: #17263a;
        font-family: 'Noto Serif KR', serif;
        font-size: clamp(2.05rem, 9vw, 3.05rem);
        font-weight: 750;
        line-height: 1.05;
        letter-spacing: -0.035em;
      }
      .paseru-login-copy {
        margin-top: .95rem;
        color: #17263a;
        font-size: clamp(1.04rem, 4.1vw, 1.25rem);
        font-weight: 760;
        line-height: 1.6;
        word-break: keep-all;
        overflow-wrap: normal;
      }
      .paseru-login-copy .nowrap { white-space: nowrap; }
      .paseru-flow-card {
        margin-top: 1.05rem;
        padding: .95rem .75rem .85rem;
        border-radius: 13px;
        border: 1px solid #e2e8f0;
        background: rgba(255,255,255,.86);
        box-shadow: inset 0 1px 0 rgba(255,255,255,.75);
      }
      .paseru-flow-steps {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        align-items: start;
        gap: .35rem;
        position: relative;
      }
      .paseru-flow-step {
        position: relative;
        z-index: 1;
        display: grid;
        justify-items: center;
        gap: .42rem;
        min-width: 0;
      }
      .paseru-flow-step:not(:last-child)::after {
        content: "";
        position: absolute;
        top: 28px;
        left: calc(50% + 32px);
        width: calc(100% - 28px);
        border-top: 5px dotted #df6c70;
        opacity: .72;
      }
      .paseru-flow-icon {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 58px;
        height: 58px;
        border-radius: 50%;
        background: #ffffff;
        border: 3px solid #ffffff;
        box-shadow: 0 6px 16px rgba(23,38,58,.15);
        font-size: 1.85rem;
      }
      .paseru-flow-step.file .paseru-flow-icon { background: #fff7ed; }
      .paseru-flow-step.route .paseru-flow-icon { background: #eff6ff; }
      .paseru-flow-step.phone .paseru-flow-icon { background: #f0fdf4; }
      .paseru-flow-step.go .paseru-flow-icon { background: #fff1f2; }
      .paseru-flow-label {
        color: #17263a;
        font-size: .88rem;
        font-weight: 800;
        line-height: 1.25;
        text-align: center;
        word-break: keep-all;
      }
      @media (max-width: 560px) {
        .paseru-login-hero { padding: .95rem .95rem 1.05rem; }
        .paseru-flow-card { padding: .78rem .45rem .72rem; }
        .paseru-flow-steps { gap: .15rem; }
        .paseru-flow-step:not(:last-child)::after {
          top: 24px;
          left: calc(50% + 25px);
          width: calc(100% - 18px);
          border-top-width: 4px;
        }
        .paseru-flow-icon {
          width: 48px;
          height: 48px;
          font-size: 1.52rem;
        }
        .paseru-flow-label { font-size: .75rem; }
      }
    </style>
    <div class="paseru-login-hero">
      <div class="paseru-login-kicker">🚒 소방 현장 노선 편성 자동화의 시작</div>
      <div class="paseru-login-title">FireSafe Route Origin</div>
      <div class="paseru-login-copy">
        방문·점검·순찰 주소 목록을 올리면<br>
        <span class="nowrap">내 핸드폰 카카오맵으로 온다</span>
      </div>
      <div class="paseru-flow-card" aria-label="파세루 이용 흐름">
        <div class="paseru-flow-steps">
          <div class="paseru-flow-step file">
            <div class="paseru-flow-icon">📄</div>
            <div class="paseru-flow-label">주소목록<br>파일</div>
          </div>
          <div class="paseru-flow-step route">
            <div class="paseru-flow-icon">📍</div>
            <div class="paseru-flow-label">자동<br>노선</div>
          </div>
          <div class="paseru-flow-step phone">
            <div class="paseru-flow-icon">📱</div>
            <div class="paseru-flow-label">핸드폰<br>전송</div>
          </div>
          <div class="paseru-flow-step go">
            <div class="paseru-flow-icon">🚒</div>
            <div class="paseru-flow-label">카카오맵<br>출발</div>
          </div>
        </div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# 공개 주소를 통한 무단 API 사용을 막기 위한 앱 입구 인증
if not st.session_state.get("paseru_authenticated", False):
    with st.container(border=True):
        st.markdown(
            '<div class="paseru-auth-title">🔐 파세루 오리진 사용자 인증</div>',
            unsafe_allow_html=True,
        )
        st.caption("이 앱은 승인된 업무 담당자만 이용할 수 있습니다.")
        with st.form("paseru_login_form", clear_on_submit=False):
            entered_password = st.text_input("비밀번호", type="password", placeholder="비밀번호를 입력하세요")
            login_submitted = st.form_submit_button("앱 시작하기", type="primary", use_container_width=True)

        with st.expander("📌 PC·휴대폰에 바로가기 만들기", expanded=False):
            st.markdown(
                """
                **🖥️ Windows PC — Edge**

                1. 브라우저 오른쪽 위 **···**를 누릅니다.
                2. **앱 → 이 사이트를 앱으로 설치**를 선택합니다.
                3. 이름을 `파세루 오리진`으로 확인하고 **설치**를 누르면 바탕화면과 시작 메뉴에서 실행할 수 있습니다.

                **📱 안드로이드 휴대폰 — 네이버 앱**

                1. 아래의 앱 주소를 복사해 **네이버 앱 주소창**에 붙여넣고 접속합니다.
                2. 화면 아래의 **☰ 메뉴(세 줄)**를 누릅니다.
                3. **홈 화면에 추가**를 누릅니다.
                4. 이름을 `파세루 오리진`으로 확인하고 **추가**를 누릅니다.

                **🍎 아이폰 — Safari**

                1. 아래쪽 **공유 버튼(□↑)**을 누릅니다.
                2. 메뉴를 내려 **홈 화면에 추가**를 선택합니다.
                3. 오른쪽 위 **추가**를 누릅니다.

                ※ 바로가기를 만들어도 앱의 업무자료 보호를 위한 비밀번호 인증은 계속 필요합니다.
                """
            )
            st.caption("아래 주소 오른쪽의 복사 버튼을 누른 뒤 네이버 앱 주소창에 붙여넣을 수 있습니다.")
            st.code(APP_PUBLIC_URL.rstrip("/") + "/", language=None)

        st.markdown(
            '<div style="margin-top:0.45rem;text-align:center;color:#344054;font-size:0.88rem;">'
            '비밀번호를 잊으셨나요? '
            '<a href="mailto:emtmisung@gmail.com?subject=%ED%8C%8C%EC%84%B8%EB%A3%A8%20%EC%98%A4%EB%A6%AC%EC%A7%84%20%EB%B9%84%EB%B0%80%EB%B2%88%ED%98%B8%20%EB%AC%B8%EC%9D%98" '
            'style="color:#a33a3f;font-weight:700;text-decoration:none;">관리자에게 문의</a>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div style="margin-top:0.55rem;text-align:center;color:#667085;font-size:0.9rem;'
            'letter-spacing:-0.01em;">파세루 오리진 · 기획 및 제작 <b style="color:#17263a;">임미성</b></div>',
            unsafe_allow_html=True,
        )

        if login_submitted:
            if not APP_PASSWORD:
                st.error("관리자가 Streamlit Secrets에 APP_PASSWORD를 먼저 등록해야 합니다.")
            elif hmac.compare_digest(entered_password, APP_PASSWORD):
                st.session_state["paseru_authenticated"] = True
                st.rerun()
            else:
                st.error("비밀번호가 올바르지 않습니다.")
    st.stop()

# 최근 작업은 서버가 아니라 현재 PC의 브라우저 저장소에만 7일간 보관한다.
browser_storage = LocalStorage(key="paseru_browser_storage")
if not st.session_state.get("browser_draft_loaded", False):
    saved_drafts = []
    storage_items = dict(browser_storage.getAll() or {})
    relevant_items = [
        (storage_key, raw_value)
        for storage_key, raw_value in storage_items.items()
        if storage_key.startswith(BROWSER_DRAFT_KEY_PREFIX)
        or storage_key == BROWSER_DRAFT_LEGACY_KEY
    ]
    for storage_key, raw_value in relevant_items:
        draft, draft_status = decode_browser_draft(raw_value)
        if draft_status in ("expired", "invalid"):
            browser_storage.eraseItem(
                storage_key,
                key=f"erase_{draft_status}_{hashlib.sha256(storage_key.encode()).hexdigest()[:12]}",
            )
            browser_storage.storedItems.pop(storage_key, None)
            continue
        if draft_status != "ok":
            continue

        if storage_key == BROWSER_DRAFT_LEGACY_KEY:
            draft["source_name"] = (
                draft.get("source_name") or draft.get("patrol_title") or "이전 저장자료"
            )
            draft["target_count"] = int(len(draft["targets_df"]))
            storage_key = browser_work_key(draft["source_name"], draft["targets_df"])
            migrated_payload = {
                key: value for key, value in draft.items()
                if key not in ("targets_df", "coords_df")
            }
            browser_storage.setItem(
                storage_key,
                json.dumps(migrated_payload, ensure_ascii=False, default=str),
                key=f"migrate_paseru_draft_{storage_key[-12:]}",
            )
            browser_storage.eraseItem(
                BROWSER_DRAFT_LEGACY_KEY, key="erase_legacy_paseru_draft",
            )
            browser_storage.storedItems.pop(BROWSER_DRAFT_LEGACY_KEY, None)

        draft["storage_key"] = storage_key
        draft["source_name"] = (
            draft.get("source_name") or draft.get("patrol_title") or "이전 저장자료"
        )
        draft["target_count"] = int(draft.get("target_count") or len(draft["targets_df"]))
        saved_drafts.append(draft)

    saved_drafts.sort(key=lambda item: float(item.get("saved_at", 0)), reverse=True)
    for old_draft in saved_drafts[BROWSER_DRAFT_MAX_ITEMS:]:
        old_key = old_draft["storage_key"]
        browser_storage.eraseItem(old_key, key=f"trim_old_paseru_draft_{old_key[-12:]}")
        browser_storage.storedItems.pop(old_key, None)
    st.session_state["browser_saved_drafts"] = saved_drafts[:BROWSER_DRAFT_MAX_ITEMS]
    st.session_state["browser_draft_loaded"] = True
    if saved_drafts:
        newest_draft = saved_drafts[0]
        apply_browser_draft(newest_draft, newest_draft["storage_key"])
        st.session_state["browser_draft_restored_notice"] = True
        st.rerun()

pending_draft_key = st.session_state.pop("pending_browser_draft_key", None)
if pending_draft_key:
    pending_draft = next(
        (
            draft for draft in st.session_state.get("browser_saved_drafts", [])
            if draft.get("storage_key") == pending_draft_key
        ),
        None,
    )
    if pending_draft:
        apply_browser_draft(pending_draft, pending_draft_key)
        st.session_state["browser_draft_restored_notice"] = True
        st.rerun()

mobile_transfer_token = st.query_params.get("transfer")
if (
    mobile_transfer_token
    and st.session_state.get("mobile_transfer_processed_token") != str(mobile_transfer_token)
):
    transferred_draft, transfer_status = consume_mobile_transfer(str(mobile_transfer_token))
    try:
        del st.query_params["transfer"]
    except KeyError:
        pass
    st.session_state["mobile_transfer_processed_token"] = str(mobile_transfer_token)
    if transfer_status == "ok":
        now_timestamp = datetime.now().timestamp()
        transferred_draft["saved_at"] = now_timestamp
        transferred_draft["expires_at"] = (
            now_timestamp + BROWSER_DRAFT_DAYS * 24 * 60 * 60
        )
        transferred_key = browser_work_key(
            transferred_draft.get("source_name"), transferred_draft["targets_df"],
        )
        apply_browser_draft(transferred_draft, transferred_key)
        st.session_state["browser_draft_mobile_imported_notice"] = True
        st.rerun()
    elif transfer_status in ("missing", "expired"):
        st.error("이 휴대폰 전달 QR은 이미 사용했거나 10분의 유효시간이 지났습니다. PC에서 새 QR을 만들어주세요.")
    elif transfer_status == "busy":
        st.warning("다른 기기에서 이 작업을 가져오는 중입니다. PC에서 새 QR을 만들어 다시 시도해주세요.")
    else:
        st.error("휴대폰 전달자료를 확인할 수 없습니다. PC에서 새 QR을 만들어주세요.")

if "browser_draft_saving_enabled" not in st.session_state:
    st.session_state["browser_draft_saving_enabled"] = True

if st.session_state.pop("browser_draft_restored_notice", False):
    st.success(
        "✅ 이 PC에 저장된 작업을 복원했습니다. "
        "대상목록과 기존 좌표검색 결과를 다시 불러오지 않아도 됩니다."
    )

if st.session_state.pop("browser_draft_mobile_imported_notice", False):
    st.success(
        "✅ PC 작업을 이 휴대폰으로 가져왔습니다. "
        "이 휴대폰의 현재 브라우저에 최대 3개 중 하나로 7일간 자동 보관됩니다."
    )

if st.session_state.pop("browser_draft_deleted_notice", False):
    st.success("✅ 선택한 저장 작업을 이 PC에서 삭제했습니다.")

with st.expander("💡 처음 사용하시나요? 사용 순서와 조건을 설정하는 이유", expanded=False):
    st.markdown(
        """
        **파세루 오리진은 다음 순서로 사용합니다.**

        **접속** — 앱 안내와 개인정보 주의사항을 확인하고 비밀번호로 접속합니다.
        1. **기본정보·대상목록** — 순찰 제목·출발 부서를 입력하고 대상명과 주소만 업로드해 좌표를 확인합니다.
        2. **순찰 세부방법** — 순찰 용도·기간·차량·반복 방식과 출동 여건을 설정합니다.
        3. **노선 생성·결과** — 확정된 좌표와 설정 결과를 바탕으로 실제 도로 기준 노선을 계산하고,
           지도·카카오맵·QR·엑셀 결과를 바로 확인·다운로드합니다.
        """
    )
    st.markdown("**업무별로 조건이 다른 이유**")
    guide_cols = st.columns(2)
    with guide_cols[0]:
        st.markdown(
            "- **지휘관 현장방문:** 지휘관 수에 맞춰 전 대상을 권역별로 자동 분할하고 이동시간을 계산합니다.\n"
            "- **특별경계근무:** 명절·선거·축제의 주요 대상을 하루 1~2회 반복할 수 있습니다.\n"
            "- **계절순찰:** 하루 약 1시간씩 나누고 출동차량의 원거리 이동을 제한합니다."
        )
    with guide_cols[1]:
        st.markdown(
            "- **예방검사:** 검사기한·가능일·팀 수를 계산해 하루 권장량을 정합니다.\n"
            "- **지리조사:** 전체 소화전을 인원과 차량에 균등 배정해 월 10회 안에 점검합니다.\n"
            "- **API 호출 제한:** 반복 계산으로 인한 처리 지연과 지도 API 비용 증가를 막습니다.\n"
            "- **대규모 처리 검증:** 217개소급 실증 결과를 반영해 작업당 호출 한도를 "
            "500건에서 **3,000건**으로 상향했습니다."
        )
    st.caption("자동 생성 노선은 참고안입니다. 현장과 출동 여건을 담당자가 검토한 뒤 최종 노선을 결정하세요.")

if not has_keys():
    st.error(
        "NCP(네이버클라우드플랫폼) Client ID/Secret이 설정되지 않았습니다. "
        "`.streamlit/secrets.toml` 또는 Streamlit Cloud의 Secrets 설정에 "
        "NCP_CLIENT_ID / NCP_CLIENT_SECRET 값을 등록해주세요."
    )


def next_tab_button(label, target_index, enabled=True):
    """다음 단계 이동 버튼. 입력 전에는 회색, 완료 후에는 초록색으로 표시한다."""
    target_names = ["1단계 기본정보", "2단계 순찰방법", "3단계 노선 생성 · 결과"]
    target_name = target_names[target_index]
    button_class = "paseru-next ready" if enabled else "paseru-next waiting"
    button_action = 'onclick="goNext()"' if enabled else "disabled"
    button_title = "입력 완료 · 다음 단계로 이동" if enabled else "필수 입력을 완료하면 이동할 수 있습니다"
    components.html(
        f"""
        <style>
          .paseru-next {{
            width:33.333%; min-width:240px; padding:11px 12px 11px 22px;
            border-radius:999px; color:#fff;
            font-family:Arial,'Noto Sans KR',sans-serif; font-size:18px; font-weight:800;
            display:flex; align-items:center; justify-content:space-between; gap:12px;
            transition:transform .16s ease,box-shadow .16s ease,filter .16s ease;
          }}
          .paseru-next.ready {{
            border:2px solid #207447; background:linear-gradient(135deg,#238553,#17663e);
            cursor:pointer; box-shadow:0 8px 20px -9px rgba(23,102,62,.8);
          }}
          .paseru-next.ready:hover {{
            transform:translateY(-2px); filter:brightness(1.06);
            box-shadow:0 12px 24px -10px rgba(23,102,62,.9);
          }}
          .paseru-next.waiting {{
            border:2px solid #aeb6c0; background:#aeb6c0; color:#f8fafc;
            cursor:not-allowed; box-shadow:none;
          }}
          .paseru-next .arrow {{
            width:34px; height:34px; flex:0 0 34px; border-radius:50%;
            display:flex; align-items:center; justify-content:center;
            background:#fff; font-size:20px; font-weight:900;
          }}
          .paseru-next.ready .arrow {{ color:#17663e; }}
          .paseru-next.waiting .arrow {{ color:#7b8490; }}
        </style>
        <button {button_action} class="{button_class}" title="{button_title}">
          <span>{html.escape(label)}</span>
          <span class="arrow">→</span>
        </button>
        <script>
          function goNext() {{
            const doc = window.parent.document;
            const wanted = '{target_name}';
            const buttons = Array.from(doc.querySelectorAll('button'));
            let target = buttons.find(el => (el.innerText || el.textContent || '').trim().includes(wanted));
            if (!target) {{
              const labels = Array.from(doc.querySelectorAll('span, p, div')).filter(
                el => (el.innerText || el.textContent || '').trim() === wanted
              );
              target = labels.length ? labels[0].closest('button,[role="tab"]') : null;
            }}
            if (target) {{
              if (!doc.getElementById('paseru-tab-attention-style')) {{
                const style = doc.createElement('style');
                style.id = 'paseru-tab-attention-style';
                style.textContent = `
                  @keyframes paseruTabPulse {{
                    0%,100% {{background:transparent;box-shadow:0 0 0 0 rgba(35,133,83,0)}}
                    50% {{background:#daf3e5;box-shadow:0 0 0 7px rgba(35,133,83,.24)}}
                  }}
                  .paseru-tab-attention {{border-radius:9px!important;animation:paseruTabPulse .75s ease-in-out 3!important}}
                `;
                doc.head.appendChild(style);
              }}
              target.classList.add('paseru-tab-attention');
              target.click();
              const tabBar = target.closest('[data-baseweb="tab-list"]') || target;
              setTimeout(() => tabBar.scrollIntoView({{behavior:'smooth', block:'start'}}), 80);
            }} else {{
              const btn = document.querySelector('.paseru-next');
              btn.style.background = 'linear-gradient(135deg,#d58a18,#a96109)';
              btn.style.borderColor = '#9b5908';
              btn.querySelector('span:first-child').innerHTML = '상단의 <b>{target_name}</b> 탭을 클릭하세요';
              btn.querySelector('.arrow').textContent = '↑';
              window.parent.scrollTo({{top:0, behavior:'smooth'}});
            }}
          }}
        </script>
        """,
        height=76,
    )

# ----------------------------------------------------------------------------
page_basic, page_details, page_build = st.tabs([
    "1단계 기본정보",
    "2단계 순찰방법",
    "3단계 노선 생성 · 결과",
])

with page_basic:
    # 1 · 기본 정보 / 대상 목록
    # ----------------------------------------------------------------------------
    with st.container(border=True):
        card_title(1, "기본 정보 · 대상 목록")
        if "patrol_title" not in st.session_state:
            st.session_state["patrol_title"] = "예시) 소방안전 순찰노선 - 성주군 일원"
        patrol_title = st.text_input("순찰 제목", key="patrol_title")
        station_search_area, current_location_area = st.columns([3, 1], gap="medium")
        with station_search_area:
            station_input_col, station_search_col = st.columns([3, 1])
            with station_input_col:
                station_query = st.text_input(
                    "출발부서 이름",
                    value=st.session_state.get("station_query", "성주소방서"),
                    placeholder="예: 선남119안전센터",
                    help="소방서·119안전센터·구조구급센터 등 출발할 부서명을 입력하세요.",
                )
            with station_search_col:
                st.markdown("<div style='height:1.72rem'></div>", unsafe_allow_html=True)
                search_station = st.button(
                    "🔎 주소조회", key="search_departure_department_btn",
                    type="primary", use_container_width=True,
                )

        with current_location_area:
            st.markdown(
                "<div style='font-size:.95rem;font-weight:650;margin-bottom:.38rem;'>현 위치 설정(야외용)</div>",
                unsafe_allow_html=True,
            )
            current_location = geolocation_component(
                key="departure_geolocation",
                default=None,
            )
        if station_query != st.session_state.get("station_query"):
            st.session_state["station_query"] = station_query
            st.session_state.pop("station_search_result", None)
            for stale_key in ("station", "route_results", "far_points", "meta"):
                st.session_state.pop(stale_key, None)

        if search_station:
            found_name, found_address, found_lat, found_lng, search_status = search_departure_department(station_query)
            if search_status == "ok":
                st.session_state["station_search_result"] = {
                    "name": found_name, "address": found_address,
                    "lat": found_lat, "lng": found_lng,
                }
                for stale_key in ("station", "route_results", "far_points", "meta"):
                    st.session_state.pop(stale_key, None)
            else:
                st.session_state.pop("station_search_result", None)
                st.warning(search_status)

        if isinstance(current_location, dict):
            location_request_id = current_location.get("request_id")
            if (location_request_id and location_request_id !=
                    st.session_state.get("last_geolocation_request_id")):
                st.session_state["last_geolocation_request_id"] = location_request_id
                if current_location.get("status") == "ok":
                    found_lat = float(current_location["latitude"])
                    found_lng = float(current_location["longitude"])
                    accuracy = current_location.get("accuracy")
                    accuracy_text = (
                        f" · 정확도 약 {float(accuracy):.0f}m" if accuracy is not None else ""
                    )
                    st.session_state["station_search_result"] = {
                        "name": "현 위치",
                        "address": f"휴대폰 GPS로 확인한 현재 위치{accuracy_text}",
                        "lat": found_lat,
                        "lng": found_lng,
                        "accuracy": float(accuracy) if accuracy is not None else None,
                    }
                    for stale_key in ("station", "route_results", "far_points", "meta"):
                        st.session_state.pop(stale_key, None)
                else:
                    st.warning(
                        current_location.get("message") or
                        "현재 위치를 확인하지 못했습니다. 위치 권한을 허용한 뒤 다시 눌러주세요."
                    )

        station_result = st.session_state.get("station_search_result")
        if station_result:
            station_name = station_result["name"]
            station_address = station_result["address"]
            station_lat = station_result["lat"]
            station_lng = station_result["lng"]
            station_accuracy = station_result.get("accuracy")
            connected_label = "출발지가 설정되었습니다" if station_name == "현 위치" else "출발부서 주소와 좌표가 연결되었습니다"
            st.success(f"✅ {connected_label}: {station_name}")
            if station_name == "현 위치" and (station_accuracy is None or station_accuracy > 100):
                st.warning(
                    "PC 또는 실내에서는 Wi-Fi·네트워크 기반으로 위치가 잡혀 실제 위치와 다를 수 있습니다. "
                    "사무실에서는 왼쪽의 출발부서 주소조회를 사용하고, 현 위치 조회는 휴대폰을 이용한 야외 순찰 때 사용하세요."
                )
            result_c1, result_c2, result_c3 = st.columns([2.2, 1, 1])
            result_c1.text_input("출발지 정보", value=station_address, disabled=True)
            result_c2.text_input("위도", value=f"{station_lat:.7f}", disabled=True)
            result_c3.text_input("경도", value=f"{station_lng:.7f}", disabled=True)
        else:
            station_name = ""
            station_address = ""
            station_lat = station_lng = None

        route_prefix = station_name
        restored_df = st.session_state.get("browser_restored_df")

        use_sample = st.checkbox(
            "🧪 기능 확인용 예시 18건 불러오기 (성주군 주요 대상)",
            value=(restored_df is None),
            help="평가관이 별도 엑셀 파일 없이 바로 확인할 수 있도록 오류 1건만 남긴 평가용 목록을 불러옵니다.",
        )
        if use_sample:
            st.caption("평가용 예시: 오류 표시가 과하게 복잡하지 않도록 오류 확인용 1건만 남긴 목록")

        st.markdown("**대상 목록 업로드**")
        saved_drafts = st.session_state.get("browser_saved_drafts", [])
        selected_draft_key = None
        if saved_drafts:
            drafts_by_key = {draft["storage_key"]: draft for draft in saved_drafts}
            st.markdown("**저장된 작업 불러오기**")
            selected_draft_key = st.selectbox(
                "저장된 작업",
                options=list(drafts_by_key),
                format_func=lambda storage_key: browser_draft_label(drafts_by_key[storage_key]),
                label_visibility="collapsed",
                key="saved_browser_draft_selector",
            )
        else:
            st.selectbox(
                "저장된 작업",
                options=["저장된 작업이 없습니다"],
                disabled=True,
                label_visibility="collapsed",
                key="empty_saved_browser_draft_selector",
            )

        st.markdown(
            """
            <style>
              div[data-testid="stTextInput"] input:disabled {
                color:#111827!important; -webkit-text-fill-color:#111827!important;
                opacity:1!important; background:#ffffff!important;
              }
              [class*="st-key-target_file_upload_"] [data-testid="stFileUploaderDropzone"] {
                padding:0!important; min-height:4.5rem!important; border:0!important; background:transparent!important;
              }
              [class*="st-key-target_file_upload_"] [data-testid="stFileUploaderDropzoneInstructions"],
              [class*="st-key-target_file_upload_"] small {display:none!important;}
              [class*="st-key-target_file_upload_"] [data-testid="stFileUploaderDropzone"] button {
                width:100%!important; height:4.5rem!important; min-height:4.5rem!important;
                padding:0!important; margin:0!important;
                border:1px solid #a9343a!important; border-radius:10px!important;
                background:#c2474d!important; color:#fff!important; font-size:0!important;
                font-weight:800!important;
              }
              [class*="st-key-target_file_upload_"] [data-testid="stFileUploaderDropzone"] button > * {
                display:none!important;
              }
              [class*="st-key-target_file_upload_"] [data-testid="stFileUploaderDropzone"] button::after {
                content:"📤 대상 목록 업로드"; font-size:0.96rem!important; color:#fff!important;
              }
              .st-key-download_blank_target_template button {
                height:4.5rem!important; min-height:4.5rem!important; padding:0!important;
                border:1px solid #75b58d!important; background:#e8f5ed!important;
                color:#111827!important; -webkit-text-fill-color:#111827!important;
                font-size:0!important; font-weight:800!important; opacity:1!important;
              }
              .st-key-download_blank_target_template button > * {
                display:none!important;
              }
              .st-key-download_blank_target_template button::after {
                content:"📥 대상 목록 빈 양식(xlsx)"; font-size:0.96rem!important;
                color:#111827!important; -webkit-text-fill-color:#111827!important;
              }
              .st-key-download_blank_target_template button:hover {
                border-color:#4e9a6b!important; background:#d9efe2!important;
                color:#111827!important; -webkit-text-fill-color:#111827!important;
              }
              .st-key-load_selected_browser_draft button,
              .st-key-delete_selected_browser_draft button {
                height:4.5rem!important; min-height:4.5rem!important; padding:0!important; font-weight:750!important;
              }
            </style>
            """,
            unsafe_allow_html=True,
        )
        upload_col, template_col, load_col, delete_col = st.columns([3, 3, 2.5, 1.5], gap="small")
        with upload_col:
            uploaded = st.file_uploader(
                "대상 목록 파일", type=["csv", "xlsx", "xls", "hwpx"],
                label_visibility="collapsed",
                key=f"target_file_upload_{st.session_state.get('file_uploader_generation', 0)}",
            )
        with template_col:
            st.download_button(
                "📥 대상 목록 빈 양식(xlsx)", data=build_upload_template(),
                file_name="파세루_대상목록_빈양식.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key="download_blank_target_template",
            )
        with load_col:
            if st.button(
                "📂 기존 작업 불러오기", key="load_selected_browser_draft",
                use_container_width=True, disabled=selected_draft_key is None,
            ):
                st.session_state["pending_browser_draft_key"] = selected_draft_key
                st.rerun()
        with delete_col:
            if st.button(
                "🗑️ 선택 삭제", key="delete_selected_browser_draft",
                use_container_width=True, disabled=selected_draft_key is None,
            ):
                browser_storage.eraseItem(
                    selected_draft_key,
                    key=f"erase_selected_paseru_draft_{selected_draft_key[-12:]}",
                )
                browser_storage.storedItems.pop(selected_draft_key, None)
                st.session_state["browser_saved_drafts"] = [
                    draft for draft in saved_drafts
                    if draft.get("storage_key") != selected_draft_key
                ]
                if st.session_state.get("active_browser_draft_key") == selected_draft_key:
                    st.session_state.pop("active_browser_draft_key", None)
                    st.session_state["browser_draft_saving_enabled"] = False
                    st.session_state.pop("browser_draft_fingerprint", None)
                    for active_key in (
                        "browser_restored_df", "browser_source_name", "browser_upload_signature",
                        "coords_df", "coord_future", "coord_api_calls", "coord_signature",
                        "mobile_transfer_qr", "station", "route_results", "far_points", "meta",
                    ):
                        st.session_state.pop(active_key, None)
                    st.session_state["sample_mode_active"] = False
                    st.session_state["file_uploader_generation"] = (
                        st.session_state.get("file_uploader_generation", 0) + 1
                    )
                st.session_state["browser_draft_deleted_notice"] = True
                st.rerun()

        notice_privacy, notice_storage, notice_mobile = st.columns(3, gap="small")
        with notice_privacy:
            st.markdown(
                """
                <div style="padding:0.38rem 0.62rem;border:1px solid #d7a54a;border-left:4px solid #b7791f;
                            border-radius:8px;background:#fff7e8;min-height:52px;">
                  <div style="font-size:0.94rem;font-weight:850;color:#744b0f;">⚠️ 개인정보 업로드 금지</div>
                  <div style="font-size:0.76rem;font-weight:650;color:#6b4610;">대상명·주소만 입력</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with notice_storage:
            st.markdown(
                """
                <div style="padding:0.38rem 0.62rem;border:1px solid #76a9cf;border-left:4px solid #2f78a8;
                            border-radius:8px;background:#edf7ff;min-height:52px;">
                  <div style="font-size:0.94rem;font-weight:850;color:#195f8e;">💾 최근 파일 7일 보관</div>
                  <div style="font-size:0.76rem;font-weight:650;color:#234b66;">이 브라우저에 최대 3개</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with notice_mobile:
            transfer_notice_title = "💻 PC 이어하기" if IS_MOBILE_DEVICE else "📱 휴대폰 이어하기"
            transfer_notice_detail = (
                "일회용 링크 · PC 7일 보관"
                if IS_MOBILE_DEVICE
                else "일회용 QR · 휴대폰 7일 보관"
            )
            st.markdown(
                f"""
                <div style="padding:0.38rem 0.62rem;border:1px solid #9b8bd1;border-left:4px solid #6750a4;
                            border-radius:8px;background:#f6f2ff;min-height:52px;">
                  <div style="font-size:0.94rem;font-weight:850;color:#503a8a;">{transfer_notice_title}</div>
                  <div style="font-size:0.76rem;font-weight:650;color:#46366f;">{transfer_notice_detail}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        if uploaded is None and not use_sample and restored_df is not None and len(restored_df):
            st.info(
                f"💾 이 PC에 저장된 대상목록 {len(restored_df)}건을 사용하고 있습니다. "
                "새 파일을 올리면 별도의 최근 작업으로 저장합니다."
            )

    using_sample = uploaded is None and bool(use_sample)
    if using_sample != st.session_state.get("sample_mode_active", False):
        for stale_key in (
            "coords_df", "coord_future", "coord_api_calls", "coord_signature",
            "mobile_transfer_qr", "station", "route_results", "far_points", "meta",
        ):
            st.session_state.pop(stale_key, None)
        st.session_state["sample_mode_active"] = using_sample

    df = None
    if uploaded is not None:
        uploaded_bytes = uploaded.getvalue()
        name_lower = uploaded.name.lower()
        if name_lower.endswith(".hwpx"):
            df = parse_hwpx(uploaded_bytes)
            if df is None:
                st.error("hwpx 파일에서 표나 목록을 찾지 못했습니다. 표 형식인지 확인해주세요.")
        else:
            df = read_uploaded_table(uploaded_bytes, uploaded.name)
        if df is not None:
            st.session_state["browser_restored_df"] = df.copy()
            st.session_state["browser_draft_saving_enabled"] = True
            upload_signature = hashlib.sha256(
                uploaded.name.encode("utf-8") + b"\0" + uploaded_bytes,
            ).hexdigest()
            if upload_signature != st.session_state.get("browser_upload_signature"):
                st.session_state["browser_upload_signature"] = upload_signature
                st.session_state["browser_source_name"] = uploaded.name
                st.session_state["active_browser_draft_key"] = browser_work_key(uploaded.name, df)
                st.session_state.pop("browser_draft_fingerprint", None)
                st.session_state.pop("coords_df", None)
                st.session_state.pop("coord_future", None)
                st.session_state.pop("coord_api_calls", None)
                st.session_state.pop("coord_signature", None)
                for stale_key in ("station", "route_results", "far_points", "meta"):
                    st.session_state.pop(stale_key, None)
    elif using_sample:
        df = load_sample_targets()
    elif restored_df is not None and len(restored_df):
        df = restored_df.copy()

    coordinate_panel = st
    if df is not None and len(df):
        coordinate_panel, mobile_panel = st.columns(2, gap="medium")
        with mobile_panel.container(border=True):
            if IS_MOBILE_DEVICE:
                st.markdown("### 💻 PC로 이어하기")
                st.caption("대상목록·출발지·좌표를 일회용 링크로 PC에 전달합니다.")
            else:
                st.markdown("### 📱 휴대폰으로 이어하기")
                st.caption("대상목록·출발지·좌표를 일회용 QR로 휴대폰에 전달합니다.")
            coords_ready_for_transfer = st.session_state.get("coords_df") is not None
            st.caption("좌표 검색 완료 후 사용할 수 있습니다.")
            if st.button(
                (
                    "💻 PC로 이어하기 링크 만들기"
                    if IS_MOBILE_DEVICE
                    else "📲 휴대폰으로 이어하기 QR 만들기"
                ),
                type="primary",
                use_container_width=True,
                key="create_mobile_transfer_qr",
                disabled=not coords_ready_for_transfer,
            ):
                now_timestamp = datetime.now().timestamp()
                transfer_targets = minimum_transfer_targets(df)
                transfer_content = browser_draft_content(
                    (
                        "기능 확인용 예시 18건"
                        if using_sample
                        else st.session_state.get("browser_source_name") or "업로드 자료"
                    ),
                    patrol_title,
                    station_query,
                    station_result,
                    transfer_targets,
                    st.session_state.get("coords_df"),
                    st.session_state.get("coord_api_calls", 0),
                )
                transfer_payload = {
                    **transfer_content,
                    "saved_at": now_timestamp,
                    "expires_at": now_timestamp + BROWSER_DRAFT_DAYS * 24 * 60 * 60,
                }
                try:
                    transfer_token, transfer_expires_at = create_mobile_transfer(transfer_payload)
                    transfer_url = f"{APP_PUBLIC_URL}/?transfer={quote(transfer_token, safe='')}"
                    st.session_state["mobile_transfer_qr"] = {
                        "url": transfer_url,
                        "png": make_qr_png(transfer_url),
                        "expires_at": transfer_expires_at,
                    }
                except (OSError, ValueError) as exc:
                    st.session_state.pop("mobile_transfer_qr", None)
                    st.error(str(exc))

            mobile_transfer_qr = st.session_state.get("mobile_transfer_qr")
            if mobile_transfer_qr:
                remaining_seconds = int(
                    float(mobile_transfer_qr["expires_at"]) - datetime.now().timestamp()
                )
                if remaining_seconds > 0:
                    if IS_MOBILE_DEVICE:
                        st.success("PC로 보낼 일회용 링크가 준비되었습니다.")
                        st.caption("아래 링크 오른쪽의 복사 버튼을 누르세요.")
                        st.code(mobile_transfer_qr["url"], language=None)
                        st.markdown(
                            "1. 링크 복사 · 카카오톡 ‘나와의 채팅’이나 메일로 보내기  \n"
                            "2. PC에서 링크 열기 · 앱 비밀번호 입력  \n"
                            "3. 현재 작업 자동 가져오기"
                        )
                        st.warning(
                            "이 링크는 만든 뒤 10분 이내에 한 번만 사용할 수 있습니다. "
                            "반드시 작업을 이어갈 PC에서 열어주세요."
                        )
                    else:
                        st.success("QR이 준비되었습니다. 지금 휴대폰으로 촬영하세요.")
                        st.image(
                            mobile_transfer_qr["png"],
                            caption="휴대폰 카메라로 QR 촬영",
                            width=300,
                        )
                        st.markdown(
                            "1. QR 촬영 · 파세루 앱 열기  \n"
                            "2. 앱 비밀번호 입력 · 작업 자동 가져오기"
                        )
                        st.warning(
                            "이 QR은 만든 뒤 10분 이내에 한 번만 사용할 수 있습니다. "
                            "가져온 뒤에는 휴대폰 브라우저에 7일간 보관됩니다."
                        )
                else:
                    st.session_state.pop("mobile_transfer_qr", None)
                    st.warning("QR 유효시간 10분이 지났습니다. 새 QR을 만들어주세요.")


    @st.cache_resource
    def coordinate_executor():
        return ThreadPoolExecutor(max_workers=2, thread_name_prefix="paseru-coordinates")


    def search_coordinates_in_background(records, name_key, address_key, lat_key=None, lng_key=None):
        rows = []
        api_calls = 0

        def count_call():
            nonlocal api_calls
            api_calls += 1

        for record in records:
            nm, ad = str(record.get(name_key, "")), str(record.get(address_key, ""))
            file_lat = file_lng = None
            if lat_key and lng_key:
                try:
                    file_lat, file_lng = float(record.get(lat_key)), float(record.get(lng_key))
                    if math.isnan(file_lat) or math.isnan(file_lng):
                        file_lat = file_lng = None
                except (TypeError, ValueError):
                    file_lat = file_lng = None
            if file_lat is not None:
                rows.append({"대상명": nm, "주소": ad, "위도": file_lat, "경도": file_lng,
                             "상태": "파일 좌표", "비고": "파일에 있던 좌표를 사용"})
                continue
            lat, lng, used_q, used_why, tried = geocode_with_fallback(
                ad, nm, on_call=count_call,
                should_stop=lambda: api_calls >= API_CALL_LIMIT,
            )
            if lat is None:
                rows.append({"대상명": nm, "주소": ad, "위도": None, "경도": None,
                             "상태": "❌ 실패",
                             "비고": geocode_failure_reason(tried) + " | 시도: " + " / ".join(tried)})
            else:
                rows.append({"대상명": nm, "주소": ad, "위도": lat, "경도": lng,
                             "상태": "✅ 확인" if used_why == "원본 주소" else "🔧 주소 보정 후 확인",
                             "비고": "" if used_why == "원본 주소" else f"{used_why} → {used_q}"})
        result = pd.DataFrame(rows)
        result.attrs["api_calls_used"] = api_calls
        return result


    if df is not None and len(df):
        pre_cols = list(df.columns)
        pre_name_idx = find_name_column_index(pre_cols)
        pre_addr_idx = find_address_column_index(pre_cols, pre_name_idx)
        pre_lat = next((c for c in pre_cols if "위도" in str(c) or str(c).lower() == "lat"), None)
        pre_lng = next((c for c in pre_cols if "경도" in str(c) or str(c).lower() in ("lng", "lon")), None)
        coord_signature = tuple((str(row[pre_cols[pre_name_idx]]), str(row[pre_cols[pre_addr_idx]]))
                                for _, row in df.iterrows())
        if st.session_state.get("coord_signature") != coord_signature:
            st.session_state["coord_signature"] = coord_signature
            st.session_state.pop("coords_df", None)
            st.session_state.pop("coord_future", None)
            st.session_state.pop("coord_api_calls", None)

        with coordinate_panel.container(border=True):
            st.markdown("### 🔎 대상 좌표 우선 확인")
            st.caption("업로드한 대상의 주소를 지도 좌표로 확인합니다.")
            st.caption("노선 생성 전 좌표 검색을 먼저 완료하세요.")
            coord_future = st.session_state.get("coord_future")
            saved_early = st.session_state.get("coords_df")
            if coord_future is None and saved_early is None:
                if st.button("🔴 좌표 검색 시작", type="primary", use_container_width=True,
                             disabled=not has_keys()):
                    st.session_state["coord_future"] = coordinate_executor().submit(
                        search_coordinates_in_background, df.to_dict("records"),
                        pre_cols[pre_name_idx], pre_cols[pre_addr_idx], pre_lat, pre_lng,
                    )
                    st.rerun()
            elif coord_future is not None and not coord_future.done():
                @st.fragment(run_every="1s")
                def poll_coordinate_search():
                    running = st.session_state.get("coord_future")
                    if running is not None and running.done():
                        try:
                            coord_result = running.result()
                            st.session_state["coords_df"] = coord_result
                            st.session_state["coord_api_calls"] = int(coord_result.attrs.get("api_calls_used", 0))
                            st.session_state.pop("coord_future", None)
                            st.rerun()
                        except Exception as exc:
                            st.session_state.pop("coord_future", None)
                            st.error(f"⚠️ 좌표 검색 중 오류가 발생했습니다: {type(exc).__name__}")
                    else:
                        st.markdown(
                            """
                            <style>
                            @keyframes paseru-search-spin {
                                0% { transform: rotate(-18deg) scale(1); }
                                50% { transform: rotate(22deg) scale(1.12); }
                                100% { transform: rotate(-18deg) scale(1); }
                            }
                            @keyframes paseru-search-slide {
                                0% { left: -38%; }
                                100% { left: 100%; }
                            }
                            @keyframes paseru-search-pulse {
                                0%, 100% { opacity: .45; transform: scale(.8); }
                                50% { opacity: 1; transform: scale(1.15); }
                            }
                            .paseru-search-card {
                                padding: 1.05rem 1rem .95rem;
                                border: 2px solid #238553;
                                border-radius: 14px;
                                background: #ecf8f1;
                                box-shadow: 0 4px 14px rgba(35,133,83,.16);
                                text-align: center;
                            }
                            .paseru-search-icon {
                                display: inline-block;
                                margin-bottom: .2rem;
                                font-size: 2.35rem;
                                animation: paseru-search-spin 1.05s ease-in-out infinite;
                            }
                            .paseru-search-title {
                                color: #126b3d;
                                font-size: 1.22rem;
                                font-weight: 800;
                                line-height: 1.45;
                            }
                            .paseru-search-dot {
                                display: inline-block;
                                width: .62rem;
                                height: .62rem;
                                margin-right: .35rem;
                                border-radius: 50%;
                                background: #1aa260;
                                animation: paseru-search-pulse 1s ease-in-out infinite;
                            }
                            .paseru-search-track {
                                position: relative;
                                height: .48rem;
                                margin: .8rem 0 .65rem;
                                overflow: hidden;
                                border-radius: 99px;
                                background: #c8e8d5;
                            }
                            .paseru-search-track span {
                                position: absolute;
                                top: 0;
                                width: 38%;
                                height: 100%;
                                border-radius: 99px;
                                background: linear-gradient(90deg, #238553, #55c78a);
                                animation: paseru-search-slide 1.25s linear infinite;
                            }
                            .paseru-search-help {
                                color: #344054;
                                font-size: .96rem;
                                font-weight: 600;
                                line-height: 1.5;
                            }
                            </style>
                            <div class="paseru-search-card" role="status" aria-live="polite">
                              <div class="paseru-search-icon" aria-hidden="true">🔎</div>
                              <div class="paseru-search-title">
                                <span class="paseru-search-dot"></span>좌표 검색이 정상 진행 중입니다
                              </div>
                              <div class="paseru-search-track" aria-hidden="true"><span></span></div>
                              <div class="paseru-search-help">
                                완료되면 자동으로 결과가 표시됩니다.<br>
                                기다리는 동안 아래 순찰 용도와 일정을 입력해도 됩니다.
                              </div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                poll_coordinate_search()
            elif coord_future is not None:
                try:
                    coord_result = coord_future.result()
                    st.session_state["coords_df"] = coord_result
                    st.session_state["coord_api_calls"] = int(coord_result.attrs.get("api_calls_used", 0))
                    st.session_state.pop("coord_future", None)
                    st.rerun()
                except Exception as exc:
                    st.session_state.pop("coord_future", None)
                    st.error(f"⚠️ 좌표 검색 중 오류가 발생했습니다: {type(exc).__name__}")
            else:
                fail_early = int(saved_early["위도"].isna().sum())
                if st.button(
                    "🔄 대상 좌표 다시 검색",
                    type="secondary",
                    use_container_width=True,
                    key="restart_coordinate_search_btn",
                    disabled=not has_keys(),
                ):
                    for stale_key in (
                        "coords_df", "coord_api_calls", "mobile_transfer_qr",
                        "station", "route_results", "far_points", "meta",
                    ):
                        st.session_state.pop(stale_key, None)
                    st.session_state["coord_future"] = coordinate_executor().submit(
                        search_coordinates_in_background, df.to_dict("records"),
                        pre_cols[pre_name_idx], pre_cols[pre_addr_idx], pre_lat, pre_lng,
                    )
                    st.rerun()
                if fail_early:
                    st.warning(
                        f"⚠️ 좌표 검색 완료 · 성공 {len(saved_early)-fail_early}건 · "
                        f"**실패 {fail_early}건**\n\n"
                        "아래에서 실패 대상을 하나씩 선택해 처리하세요. 한 대상의 처리가 끝나면 "
                        "목록에서 빠지고 다음 실패 대상으로 이어집니다."
                    )
                    st.markdown("#### 🔁 실패 대상 하나씩 처리")
                    failed_indexes = saved_early.index[saved_early["위도"].isna()].tolist()
                    retry_key = hashlib.sha256(
                        repr(coord_signature).encode("utf-8"),
                    ).hexdigest()[:12]
                    selected_failed_key = f"selected_failed_target_{retry_key}"
                    if st.session_state.get(selected_failed_key) not in failed_indexes:
                        st.session_state.pop(selected_failed_key, None)
                    selected_failed_index = st.selectbox(
                        "처리할 실패 대상",
                        options=failed_indexes,
                        format_func=lambda row_index: (
                            f"{saved_early.at[row_index, '대상명']} · "
                            f"{saved_early.at[row_index, '주소']}"
                        ),
                        key=selected_failed_key,
                    )
                    failed_row = saved_early.loc[selected_failed_index]
                    target_name = str(failed_row.get("대상명", "")).strip()
                    original_address = str(failed_row.get("주소", "")).strip()
                    failure_detail = str(failed_row.get("비고", "")).strip()

                    st.markdown(f"**선택 대상:** {target_name}")
                    st.caption(f"현재 주소: {original_address}")
                    retry_action = st.radio(
                        "처리 방법",
                        [
                            "① 주소 수정 후 재검색",
                            "② 좌표 직접입력",
                            "③ 이번 대상 제외",
                            "④ 지도에서 실제 위치 찍기",
                        ],
                        key=f"single_retry_action_{retry_key}_{selected_failed_index}",
                    )
                    if failure_detail:
                        with st.expander("실패 사유·검색 시도내역 보기", expanded=False):
                            st.caption(failure_detail)

                    used_coord_calls = int(st.session_state.get("coord_api_calls", 0))
                    remaining_coord_calls = max(0, API_CALL_LIMIT - used_coord_calls)

                    def save_coordinate_resolution(updated_coords, message, level="success", api_calls=None):
                        total_calls = used_coord_calls if api_calls is None else int(api_calls)
                        updated_coords.attrs["api_calls_used"] = total_calls
                        st.session_state["coords_df"] = updated_coords
                        st.session_state["coord_api_calls"] = total_calls
                        st.session_state["coord_retry_message"] = message
                        st.session_state["coord_retry_message_level"] = level
                        for stale_key in ("mobile_transfer_qr", "route_results", "far_points", "meta"):
                            st.session_state.pop(stale_key, None)
                        st.rerun()

                    if retry_action == "① 주소 수정 후 재검색":
                        retry_address = st.text_input(
                            "수정한 주소",
                            value=original_address,
                            key=f"single_retry_address_{retry_key}_{selected_failed_index}",
                            help="정확한 도로명주소 또는 지번주소를 입력하세요.",
                        )
                        if remaining_coord_calls == 0:
                            st.warning("API 호출 한도에 도달해 주소 재검색은 할 수 없습니다.")
                        if st.button(
                            "🔎 이 주소로 다시 검색",
                            type="primary",
                            use_container_width=True,
                            disabled=(remaining_coord_calls == 0),
                        ):
                            retry_counter = {"calls": 0}

                            def count_retry_call():
                                retry_counter["calls"] += 1

                            with st.spinner("수정한 주소를 검색하고 있습니다..."):
                                lat, lng, used_q, used_why, tried = geocode_with_fallback(
                                    retry_address,
                                    target_name,
                                    on_call=count_retry_call,
                                    should_stop=lambda: (
                                        used_coord_calls + retry_counter["calls"] >= API_CALL_LIMIT
                                    ),
                                )
                            updated_coords = saved_early.copy()
                            updated_coords.at[selected_failed_index, "주소"] = retry_address
                            total_calls = used_coord_calls + retry_counter["calls"]
                            if lat is None:
                                updated_coords.at[selected_failed_index, "상태"] = "❌ 재검색 실패"
                                updated_coords.at[selected_failed_index, "비고"] = (
                                    geocode_failure_reason(tried) + " | 시도: " + " / ".join(tried)
                                )
                                save_coordinate_resolution(
                                    updated_coords,
                                    f"{target_name} 재검색에 실패했습니다. 주소를 다시 확인하거나 좌표를 직접 입력해 주세요.",
                                    level="warning",
                                    api_calls=total_calls,
                                )
                            old_address = str(saved_early.at[selected_failed_index, "주소"])
                            updated_coords.at[selected_failed_index, "위도"] = lat
                            updated_coords.at[selected_failed_index, "경도"] = lng
                            updated_coords.at[selected_failed_index, "상태"] = "✅ 주소 변경 후 확인"
                            updated_coords.at[selected_failed_index, "비고"] = (
                                f"기존 주소: {old_address} → 변경 주소: {retry_address}"
                                + ("" if used_why == "원본 주소" else f" | {used_why} → {used_q}")
                            )
                            save_coordinate_resolution(
                                updated_coords,
                                f"{target_name}의 좌표를 다시 찾았습니다. 남은 실패 {fail_early-1}건",
                                api_calls=total_calls,
                            )

                    elif retry_action == "② 좌표 직접입력":
                        st.info(
                            "소화전처럼 기존 관리자료에 좌표가 있는 대상은 여기서 위도·경도를 바로 입력하세요. "
                            "입력한 좌표는 주소검색 결과보다 우선 적용됩니다."
                        )
                        st.caption("예: 위도 35.9690000 / 경도 128.2834000")
                        coord_col1, coord_col2 = st.columns(2)
                        with coord_col1:
                            manual_lat = st.number_input(
                                "위도",
                                min_value=30.0,
                                max_value=45.0,
                                value=None,
                                step=0.000001,
                                format="%.7f",
                                key=f"manual_lat_{retry_key}_{selected_failed_index}",
                                placeholder="35.9690000",
                            )
                        with coord_col2:
                            manual_lng = st.number_input(
                                "경도",
                                min_value=120.0,
                                max_value=135.0,
                                value=None,
                                step=0.000001,
                                format="%.7f",
                                key=f"manual_lng_{retry_key}_{selected_failed_index}",
                                placeholder="128.2834000",
                            )
                        naver_query = quote(f"{target_name} {original_address}".strip())
                        st.link_button(
                            "네이버 지도에서 주소·지번 확인",
                            f"https://map.naver.com/p/search/{naver_query}",
                            use_container_width=True,
                        )
                        if st.button(
                            "좌표 직접입력으로 확정",
                            type="primary",
                            use_container_width=True,
                            disabled=(manual_lat is None or manual_lng is None),
                        ):
                            updated_coords = saved_early.copy()
                            updated_coords.at[selected_failed_index, "위도"] = float(manual_lat)
                            updated_coords.at[selected_failed_index, "경도"] = float(manual_lng)
                            updated_coords.at[selected_failed_index, "상태"] = "✍️ 좌표 직접입력"
                            updated_coords.at[selected_failed_index, "비고"] = (
                                f"원주소: {original_address} | 사용자가 관리자료 좌표를 직접 입력"
                            )
                            save_coordinate_resolution(
                                updated_coords,
                                f"{target_name}의 좌표를 직접 입력했습니다. 남은 실패 {fail_early-1}건",
                            )

                    elif retry_action == "④ 지도에서 실제 위치 찍기":
                        st.info(
                            "지도를 확대·이동한 뒤 실제 대상 위치를 한 번 누르세요. "
                            "사용자가 누른 지점만 좌표로 저장하며 임의 좌표는 자동 적용하지 않습니다."
                        )
                        st.warning(
                            "현재 내장 지도는 네이버 지도처럼 지번·지형·위성정보가 한꺼번에 잘 보이지 않을 수 있습니다. "
                            "위치를 확신하기 어려우면 좌표 직접입력이나 대상 제외를 사용하세요."
                        )
                        valid_coords = saved_early.dropna(subset=["위도", "경도"])
                        center_lat, center_lng, start_zoom, center_reason = manual_map_start(
                            saved_early,
                            original_address,
                            station_lat,
                            station_lng,
                        )
                        st.caption(
                            f"🧭 {center_reason}으로 지도를 열었습니다. "
                            "이 시작점은 위치를 찾기 위한 화면 기준이며 대상 좌표로 저장되지 않습니다."
                        )
                        st.caption(
                            "🛰️ 처음에는 위성+도로명 지도로 열립니다. "
                            "도로명과 주변 건물을 함께 확인한 뒤 실제 대상 위치를 누르세요."
                        )

                        manual_map = folium.Map(
                            location=[center_lat, center_lng], zoom_start=start_zoom,
                            tiles=None, control_scale=True,
                        )
                        satellite_layer = folium.TileLayer(
                            tiles=(
                                "https://server.arcgisonline.com/ArcGIS/rest/services/"
                                "World_Imagery/MapServer/tile/{z}/{y}/{x}"
                            ),
                            attr=(
                                "Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics, "
                                "and the GIS User Community"
                            ),
                            name="🛰️ 위성만 보기",
                            overlay=False,
                            control=True,
                            show=True,
                            max_zoom=20,
                        ).add_to(manual_map)
                        normal_layer = folium.TileLayer(
                            tiles="OpenStreetMap",
                            name="🗺️ 일반지도(도로 확인)",
                            overlay=False,
                            control=True,
                            show=False,
                        ).add_to(manual_map)
                        road_layer = folium.TileLayer(
                            tiles=(
                                "https://server.arcgisonline.com/ArcGIS/rest/services/"
                                "Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}"
                            ),
                            attr="Road labels © Esri",
                            name="도로 표시",
                            overlay=True,
                            control=False,
                            show=True,
                            max_zoom=20,
                        ).add_to(manual_map)
                        label_layer = folium.TileLayer(
                            tiles=(
                                "https://server.arcgisonline.com/ArcGIS/rest/services/"
                                "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
                            ),
                            attr="Place labels © Esri",
                            name="지명 표시",
                            overlay=True,
                            control=False,
                            show=True,
                            max_zoom=20,
                        ).add_to(manual_map)
                        add_manual_location_layer_buttons(
                            manual_map, satellite_layer, normal_layer, road_layer, label_layer
                        )
                        for _, known_row in valid_coords.iterrows():
                            folium.CircleMarker(
                                [float(known_row["위도"]), float(known_row["경도"])],
                                radius=3, color="#2f78a8", fill=True, fill_opacity=0.65,
                                tooltip=str(known_row.get("대상명", "")),
                            ).add_to(manual_map)
                        if station_lat is not None and station_lng is not None:
                            folium.Marker(
                                [float(station_lat), float(station_lng)],
                                tooltip="출발지",
                                icon=folium.Icon(color="red", icon="home"),
                            ).add_to(manual_map)
                        folium.LatLngPopup().add_to(manual_map)
                        folium.LayerControl(position="topleft", collapsed=True).add_to(manual_map)
                        manual_map_state = st_folium(
                            manual_map,
                            height=380,
                            use_container_width=True,
                            key=f"manual_location_map_{retry_key}_{selected_failed_index}",
                            returned_objects=["last_clicked"],
                        )
                        clicked_point = (manual_map_state or {}).get("last_clicked")
                        if clicked_point:
                            clicked_lat = float(clicked_point["lat"])
                            clicked_lng = float(clicked_point["lng"])
                            st.success(
                                f"선택한 위치 · 위도 {clicked_lat:.7f} · 경도 {clicked_lng:.7f}"
                            )
                            if st.button(
                                "📍 이 위치로 확정",
                                type="primary",
                                use_container_width=True,
                            ):
                                updated_coords = saved_early.copy()
                                updated_coords.at[selected_failed_index, "위도"] = clicked_lat
                                updated_coords.at[selected_failed_index, "경도"] = clicked_lng
                                updated_coords.at[selected_failed_index, "상태"] = "📍 지도에서 직접 지정"
                                updated_coords.at[selected_failed_index, "비고"] = (
                                    f"원주소: {original_address} | 사용자가 지도에서 직접 지정한 좌표 · 현장 확인 완료"
                                )
                                save_coordinate_resolution(
                                    updated_coords,
                                    f"{target_name}의 위치를 지도에서 확정했습니다. 남은 실패 {fail_early-1}건",
                                )
                        else:
                            st.caption("아직 위치를 선택하지 않았습니다. 지도에서 실제 위치를 눌러주세요.")

                    else:
                        st.warning(f"{target_name}을 이번 노선 대상목록에서 제외합니다.")
                        if st.button(
                            "🗑️ 이 대상 제외",
                            type="primary",
                            use_container_width=True,
                        ):
                            updated_coords = saved_early.drop(index=[selected_failed_index]).reset_index(drop=True)
                            save_coordinate_resolution(
                                updated_coords,
                                f"{target_name}을 대상에서 제외했습니다. 남은 실패 {fail_early-1}건",
                            )
                else:
                    st.success(f"✅ 좌표 확인 완료 · {len(saved_early)}건 모두 확인되었습니다.")

                retry_message = st.session_state.pop("coord_retry_message", None)
                if retry_message:
                    retry_message_level = st.session_state.pop(
                        "coord_retry_message_level", "success",
                    )
                    if retry_message_level == "warning":
                        st.warning(retry_message)
                    else:
                        st.success(f"✅ {retry_message}")

                distribution_map = build_distribution_map(
                    saved_early,
                    {"name": station_name, "lat": station_lat, "lng": station_lng},
                )
                if distribution_map is not None:
                    st.markdown("#### 🗺️ 전체 대상 분포지도")
                    st.caption(
                        f"좌표가 확인된 {len(saved_early)-fail_early}개소를 표시합니다. "
                        "번호 표식에 마우스를 올리거나 누르면 대상물명과 주소를 확인할 수 있습니다."
                    )
                    st_folium(
                        distribution_map, height=500, use_container_width=True,
                        key=f"distribution_map_{hash(coord_signature)}",
                    )

                with st.expander("🔎 좌표 검색 결과 보기", expanded=False):
                    st.dataframe(saved_early, use_container_width=True, hide_index=True)

    has_browser_work = (
        df is not None and len(df)
        and (uploaded is not None or st.session_state.get("browser_restored_df") is not None)
        and not using_sample
    )
    if has_browser_work:
        if st.session_state.get("browser_draft_saving_enabled", True):
            source_name = st.session_state.get("browser_source_name") or "업로드 자료"
            active_draft_key = st.session_state.get("active_browser_draft_key")
            if not active_draft_key:
                active_draft_key = browser_work_key(source_name, df)
                st.session_state["active_browser_draft_key"] = active_draft_key
            current_draft_content = browser_draft_content(
                source_name,
                patrol_title,
                station_query,
                station_result,
                df,
                st.session_state.get("coords_df"),
                st.session_state.get("coord_api_calls", 0),
            )
            fingerprint_source = json.dumps(
                current_draft_content, ensure_ascii=False, sort_keys=True, default=str,
            )
            draft_fingerprint = hashlib.sha256(
                f"{active_draft_key}\n{fingerprint_source}".encode("utf-8"),
            ).hexdigest()
            if draft_fingerprint != st.session_state.get("browser_draft_fingerprint"):
                now_timestamp = datetime.now().timestamp()
                browser_draft_payload = {
                    **current_draft_content,
                    "saved_at": now_timestamp,
                    "expires_at": now_timestamp + BROWSER_DRAFT_DAYS * 24 * 60 * 60,
                }
                serialized_draft = json.dumps(
                    browser_draft_payload, ensure_ascii=False, default=str,
                )
                current_saved_drafts = [
                    draft for draft in st.session_state.get("browser_saved_drafts", [])
                    if draft.get("storage_key") != active_draft_key
                ]
                oldest_draft = None
                if len(current_saved_drafts) >= BROWSER_DRAFT_MAX_ITEMS:
                    oldest_draft = min(
                        current_saved_drafts,
                        key=lambda item: float(item.get("saved_at", 0)),
                    )
                retained_keys = {
                    draft["storage_key"] for draft in current_saved_drafts
                    if oldest_draft is None or draft["storage_key"] != oldest_draft["storage_key"]
                }
                other_saved_size = sum(
                    len(str(raw_value))
                    for storage_key, raw_value in browser_storage.storedItems.items()
                    if storage_key in retained_keys
                )
                if len(serialized_draft) + other_saved_size <= 4_000_000:
                    if oldest_draft is not None:
                        oldest_key = oldest_draft["storage_key"]
                        browser_storage.eraseItem(
                            oldest_key, key=f"erase_oldest_paseru_draft_{oldest_key[-12:]}",
                        )
                        browser_storage.storedItems.pop(oldest_key, None)
                        current_saved_drafts = [
                            draft for draft in current_saved_drafts
                            if draft.get("storage_key") != oldest_key
                        ]
                    browser_storage.setItem(
                        active_draft_key,
                        serialized_draft,
                        key=f"save_paseru_draft_{draft_fingerprint[:16]}",
                    )
                    session_draft = {
                        **browser_draft_payload,
                        "targets_df": df.copy(),
                        "coords_df": (
                            st.session_state["coords_df"].copy()
                            if st.session_state.get("coords_df") is not None else None
                        ),
                        "storage_key": active_draft_key,
                    }
                    current_saved_drafts.append(session_draft)
                    current_saved_drafts.sort(
                        key=lambda item: float(item.get("saved_at", 0)), reverse=True,
                    )
                    st.session_state["browser_saved_drafts"] = current_saved_drafts
                    st.session_state["browser_draft_fingerprint"] = draft_fingerprint
                    st.session_state["browser_draft_loaded"] = True
                else:
                    st.warning(
                        "저장된 작업의 전체 크기가 브라우저 자동저장 허용 범위를 초과했습니다. "
                        "기존 작업을 하나 삭제하거나 대상목록 크기를 줄여주세요. "
                        "현재 작업은 가능하지만 앱을 나가면 자동 복원되지 않을 수 있습니다."
                    )

    st.write("")

    basic_ready = bool(
        patrol_title.strip() and station_name.strip()
        and station_address.strip() and station_lat is not None and station_lng is not None
        and df is not None and len(df) and st.session_state.get("coords_df") is not None
        and st.session_state.get("coord_future") is None
    )
    next_tab_button("2단계로 이동", 1, enabled=basic_ready)
    if not basic_ready:
        st.caption("제목·출발지·대상목록을 입력하고 좌표 검색을 완료하면 버튼이 초록색으로 바뀝니다.")

    # ----------------------------------------------------------------------------

with page_details:
    # 2 · 순찰 방법과 세부 일정
    # ----------------------------------------------------------------------------
    PURPOSE_OPTIONS = [
        "① 지휘관 현장방문", "② 특별경계근무용", "③ 계절순찰", "④ 예방검사", "⑤ 지리조사(센터용)",
    ]
    PURPOSE_HINT = {
        "① 지휘관 현장방문": "방문 지휘관 수를 기준으로 전체 대상을 권역별로 자동 분할하고 구역별 이동거리와 소요시간을 계산합니다.",
        "② 특별경계근무용": "명절·선거·축제 등 특별경계근무 — 휴무 공장과 터미널·역·공항·행사장 등 주요 대상을 하루 1~2회 반복 순찰합니다.",
        "③ 계절순찰": "정해진 기간 동안 수행자·차량·편도 제한·1회 최대시간을 반영해 반복형 또는 전 대상 순환형 노선을 만듭니다.",
        "④ 예방검사": "숙박업소 등 점검 순찰.",
        "⑤ 지리조사(센터용)": "소화전 등 팀별 순회 — 팀 수·목표시간 기준으로 노선수를 자동 산출합니다.",
    }

    with st.container(border=True):
        card_title(2, "순찰 방법 · 세부 조건")
        purpose_label = st.pills("노선 용도", PURPOSE_OPTIONS, default=None,
                                 label_visibility="collapsed")
        if not purpose_label:
            st.info("먼저 순찰방법을 하나 선택하면 대상 업로드와 세부 설정이 나타납니다.")
            next_tab_button("3단계로 이동", 2, enabled=False)
            st.stop()
        if hasattr(st, "popover"):
            with st.popover("❔ 선택한 순찰방법 설명 보기"):
                st.write(PURPOSE_HINT.get(purpose_label, ""))
                st.caption("순찰방법을 바꾸면 해당 업무에 필요한 조건만 자동으로 표시됩니다.")
        else:
            with st.expander("❔ 선택한 순찰방법 설명 보기", expanded=False):
                st.write(PURPOSE_HINT.get(purpose_label, ""))
        purpose = {
            "① 지휘관 현장방문": "other", "② 특별경계근무용": "guard",
            "③ 계절순찰": "season", "④ 예방검사": "inspect",
            "⑤ 지리조사(센터용)": "hydrant",
        }.get(purpose_label, "other")

        guard_repeat_label = None
        guard_rounds = None
        hydrant_members = []
        hydrant_member_count = 0
        hydrant_vehicle_count = 0
        hydrant_workdays = 10
        hydrant_target_min = 90
        hydrant_max_min = 120
        hydrant_inspection_min = 5
        hydrant_distribution_basis = "소요시간 균등"
        season_scope = "관할 전체 대상 균등 순환"
        season_actor = "소방공무원"
        season_vehicle = "소방차"
        season_limit_basis = "편도시간(분)"
        season_oneway_limit = 30
        season_strategy = "전 대상 균등 순환"
        season_delegate = "의용소방대 순찰 권장"
        commander_count = 1
        commander_route_count = 1
        commander_oneway_limit = 20
        commander_stop_min = 30

        if purpose == "guard":
            gc1, gc2 = st.columns([1.6, 1])
            with gc1:
                sub_label("반복 방식")
                guard_repeat_label = st.pills("반복 방식", ["매일 같은 코스 반복", "매일 다른 코스 순환"],
                                              default="매일 같은 코스 반복", label_visibility="collapsed")
            with gc2:
                if guard_repeat_label == "매일 같은 코스 반복":
                    sub_label("하루 반복 횟수")
                    guard_rounds = st.pills("하루 반복 횟수", ["1회", "2회", "3회"], default="1회",
                                            label_visibility="collapsed")
        elif purpose == "season":
            st.caption("업로드한 관할 전체 대상을 기간 동안 서로 다른 코스로 균등하게 순환합니다.")
            sc1, sc2 = st.columns(2)
            with sc1:
                season_actor = st.pills(
                    "누가 순찰합니까?", ["소방공무원", "의용소방대"], default="소방공무원"
                ) or "소방공무원"
            with sc2:
                season_vehicle = st.pills(
                    "어떤 차량을 이용합니까?", ["소방차", "구급차", "행정차", "일반차"], default="소방차"
                ) or "소방차"
            sc3, sc4 = st.columns(2)
            with sc3:
                season_limit_basis = st.pills(
                    "센터 기준 편도 제한", ["편도시간(분)", "편도거리(km)"], default="편도시간(분)"
                ) or "편도시간(분)"
            with sc4:
                season_oneway_limit = st.number_input(
                    "편도 제한값(분 또는 km)",
                    min_value=1.0,
                    max_value=120.0,
                    value=30.0 if season_limit_basis == "편도시간(분)" else 20.0,
                    step=5.0 if season_limit_basis == "편도시간(분)" else 1.0,
                    help="편도 30분은 왕복 이동만 약 1시간인 거리입니다. 기준을 넘으면 의용소방대 순찰을 권장합니다.",
                )
            season_delegate = ("의용소방대 순찰 권장" if season_actor == "소방공무원"
                               else "장거리 별도 순찰 검토")
        elif purpose == "hydrant":
            hc1, hc2 = st.columns(2)
            with hc1:
                hydrant_member_count = st.number_input("지리조사 인원 수", min_value=1, max_value=30, value=2)
            with hc2:
                hydrant_vehicle_count = st.number_input("운행 차량 수", min_value=1, max_value=15, value=1)
            hydrant_distribution_basis = st.radio(
                "팀별 노선 분배 기준",
                ["소요시간 균등", "전체 개수 균등", "거리 km 균등", "센터 가까운 곳 많이, 먼 곳 적게"],
                horizontal=True,
                help=(
                    "지수리처럼 팀별 업무량을 맞출 때 사용할 기준입니다. "
                    "가까운 곳 많이/먼 곳 적게는 센터에서 먼 대상을 적게 배정하는 방식입니다."
                ),
            )
            st.caption("선택한 분배 기준으로 팀별 담당구역을 먼저 나눈 뒤, 각 팀 안에서 가까운 순서로 노선을 만듭니다.")
            # 개인정보 보호를 위해 화면에서는 실명과 차량별 팀원 편성을 입력받지 않는다.
            # 계산에는 익명 순번만 사용하고, 담당 조·조원은 내려받은 엑셀에서 작성한다.
            for member_index in range(int(hydrant_member_count)):
                hydrant_members.append({
                    "name": "",
                    "vehicle_no": (member_index % int(hydrant_vehicle_count)) + 1,
                    "order": member_index,
                })
            st.info("🔒 개인정보 보호를 위해 팀원 이름은 앱에서 입력하지 않습니다. 담당 조·조원은 결과 엑셀을 내려받은 뒤 작성하세요.")
        elif purpose == "other":
            st.caption(
                "방문 지휘관 수를 입력하면 지휘관별 담당구역을 자동으로 나누고 "
                "구역별 이동거리와 예상 소요시간을 계산합니다."
            )
            commander_count = st.number_input(
                "방문 지휘관 수", min_value=1, max_value=20, value=2,
                help="지휘관 1명당 1개 담당구역이 자동으로 생성됩니다.",
            )
            commander_route_count = int(commander_count)
            cc5, cc6 = st.columns(2)
            with cc5:
                commander_oneway_label = st.pills(
                    "출발지 기준 편도 허용시간",
                    ["10분", "20분", "수동입력"],
                    default="20분",
                ) or "20분"
                if commander_oneway_label == "수동입력":
                    commander_oneway_limit = st.number_input(
                        "편도 허용시간 직접 입력(분)",
                        min_value=1, max_value=180, value=30,
                    )
                else:
                    commander_oneway_limit = int(commander_oneway_label.replace("분", ""))
            with cc6:
                commander_stop_label = st.pills(
                    "1개소당 현장 대응 소요시간",
                    ["10분", "30분", "60분", "수동입력"],
                    default="30분",
                ) or "30분"
                if commander_stop_label == "수동입력":
                    commander_stop_min = st.number_input(
                        "현장 대응시간 직접 입력(분)",
                        min_value=1, max_value=360, value=45,
                    )
                else:
                    commander_stop_min = int(commander_stop_label.replace("분", ""))
            st.success(
                f"방문 지휘관 {int(commander_count)}명을 기준으로 "
                f"전체 대상을 최대 {int(commander_route_count)}개 담당구역으로 자동 분할합니다."
            )
            st.caption(
                f"편도 {int(commander_oneway_limit)}분 이내 대상을 배정하며, "
                f"대상 1개소마다 현장 대응시간 {int(commander_stop_min)}분을 더해 "
                "구역별 총 예상시간을 계산합니다."
            )
            safety_warning(
                "본 결과는 평시 순찰계획 및 사전 검토를 위한 참고자료입니다. "
                "실제 재난대응 시에는 기상, 도로 통제, 재난 확산, 인명위험 및 가용 소방력 등 "
                "실시간 변수가 반영되지 않으므로 현장지휘관의 판단과 공식 지휘체계를 우선하십시오."
            )

    st.write("")

    # ----------------------------------------------------------------------------
    # 2 · 순찰 일정
    # ----------------------------------------------------------------------------
    if "period_start" not in st.session_state:
        st.session_state["period_start"] = date(2026, 9, 23)
        st.session_state["period_start_time"] = dtime(18, 0)
        st.session_state["period_end"] = date(2026, 9, 28)
        st.session_state["period_end_time"] = dtime(9, 0)

    with st.container(border=True):
        if purpose == "inspect":
            card_title(2, "예방검사 일정 · 노선 조건")
            st.caption("대상 파일에는 대상명과 주소만 준비하면 됩니다. 공통 검사 조건은 여기에서 한 번만 설정합니다.")

            ic1, ic2 = st.columns(2)
            with ic1:
                period_start = st.date_input("검사 시작일", key="period_start")
            with ic2:
                period_end = st.date_input("검사 완료기한", key="period_end")

            weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
            inspect_weekdays = st.multiselect(
                "검사 가능 요일",
                weekday_names,
                default=["월", "화", "수", "목", "금"],
                help="실제로 예방검사를 실시할 요일만 선택하세요.",
            )

            ic3, ic4, ic5, ic6 = st.columns(4)
            with ic3:
                inspect_teams = st.number_input("검사팀 수", min_value=1, max_value=30, value=1)
            with ic4:
                inspect_targets_per_day = st.number_input(
                    "팀당 하루 검사 대상 수", min_value=1, max_value=100, value=2, step=1,
                    help="한 팀이 하루에 방문할 수 있는 최대 대상 수입니다.",
                )
            with ic5:
                inspect_daily_hours = st.number_input(
                    "팀당 하루 검사 가능시간", min_value=1.0, max_value=12.0, value=6.0, step=0.5,
                )
            with ic6:
                inspect_minutes = st.number_input(
                    "대상당 평균 검사시간(분)", min_value=5, max_value=480, value=40, step=5,
                )

            excluded_text = st.text_input(
                "검사 제외일(선택)",
                placeholder="예) 2026-09-21, 2026-10-03",
                help="공휴일·훈련일 등 검사하지 않는 날짜를 쉼표로 구분해 입력하세요.",
            )
            inspect_excluded_dates = set()
            invalid_excluded_dates = []
            for value in [v.strip() for v in excluded_text.split(",") if v.strip()]:
                try:
                    inspect_excluded_dates.add(datetime.strptime(value, "%Y-%m-%d").date())
                except ValueError:
                    invalid_excluded_dates.append(value)
            if invalid_excluded_dates:
                st.warning("제외일은 YYYY-MM-DD 형식으로 입력해주세요: " + ", ".join(invalid_excluded_dates))

            start_dt = datetime.combine(period_start, dtime(9, 0))
            end_dt = datetime.combine(period_end, dtime(18, 0))
            selected_weekdays = {i for i, name in enumerate(weekday_names) if name in inspect_weekdays}
            inspect_dates = []
            if period_end < period_start:
                st.warning("⚠ 검사 완료기한이 시작일보다 빠릅니다. 기간을 확인해주세요.")
            elif not selected_weekdays:
                st.warning("⚠ 검사 가능 요일을 하나 이상 선택해주세요.")
            else:
                current_date = period_start
                while current_date <= period_end:
                    if current_date.weekday() in selected_weekdays and current_date not in inspect_excluded_dates:
                        inspect_dates.append(current_date)
                    current_date += timedelta(days=1)

            period_days = max(1, len(inspect_dates))
            inspect_capacity = (
                len(inspect_dates) * int(inspect_teams) * int(inspect_targets_per_day)
            )
            st.caption(
                f"실제 검사 가능일 {len(inspect_dates)}일 · 전체 가용 팀 일수 "
                f"{len(inspect_dates) * int(inspect_teams)}팀 일 · 최대 검사 가능 {inspect_capacity}개소"
            )
            vehicle = ""
            st.info(
                f"{int(inspect_teams)}개 팀에 팀당 하루 최대 {int(inspect_targets_per_day)}개소씩 배정하고, "
                f"하루 {inspect_daily_hours:g}시간과 대상당 평균 {int(inspect_minutes)}분을 기준으로 "
                "일정과 노선을 자동 편성합니다."
            )
        elif purpose == "season":
            card_title(2, "계절순찰 일정")
            dc1, dc2 = st.columns(2)
            with dc1:
                period_start = st.date_input("순찰 시작일", key="period_start")
            with dc2:
                period_end = st.date_input("순찰 종료일", key="period_end")
            season_time_choice = st.pills(
                "1회 순찰시간 선택",
                ["30분", "60분", "직접 입력"],
                default="60분",
                help="출발·복귀 이동과 대상별 현장 확인시간을 모두 포함한 시간입니다.",
            ) or "60분"
            sc_time1, sc_time2 = st.columns(2)
            with sc_time1:
                if season_time_choice == "직접 입력":
                    season_target_min = st.number_input(
                        "직접 입력(분)", min_value=20, max_value=360, value=90, step=10,
                    )
                else:
                    season_target_min = int(season_time_choice.replace("분", ""))
                    st.metric("적용할 1회 최대시간", f"{season_target_min}분")
            with sc_time2:
                season_stop_min = st.number_input(
                    "대상당 현장 확인시간(분)", min_value=0, max_value=30, value=2,
                )
            start_dt = datetime.combine(period_start, dtime(9, 0))
            end_dt = datetime.combine(period_end, dtime(18, 0))
            period_days = max(1, (period_end - period_start).days + 1)
            vehicle = season_vehicle
            inspect_weekdays = []
            inspect_teams = 1
            inspect_daily_hours = 6.0
            inspect_minutes = 40
            inspect_targets_per_day = 2
            inspect_capacity = 0
            inspect_dates = []
            st.caption("입력한 조건은 좌표 검색 결과와 결합한 뒤 3단계 노선 생성·결과에서 확인합니다.")
        elif purpose == "hydrant":
            card_title(2, "지리조사 설정")
            st.caption("당비비 근무 기준으로 한 달 10번의 당번일 안에 전체 소화전을 점검하도록 노선을 나눕니다.")
            hc3, hc4, hc5, hc6 = st.columns(4)
            with hc3:
                hydrant_workdays = st.number_input("월 당번 근무일", min_value=1, max_value=31, value=10)
            with hc4:
                hydrant_target_min = st.number_input(
                    "노선 기본 목표시간(분)", min_value=60, max_value=240, value=90, step=10,
                )
            with hc5:
                hydrant_max_min = st.number_input(
                    "노선 최대 허용시간(분)", min_value=60, max_value=360, value=120, step=10,
                )
            with hc6:
                hydrant_inspection_min = st.number_input(
                    "소화전 1개 조사시간(분)", min_value=0, max_value=60, value=5, step=1,
                )

            if hydrant_max_min < hydrant_target_min:
                st.warning("최대 허용시간은 기본 목표시간보다 길게 설정해주세요.")
                hydrant_max_min = hydrant_target_min
            today = date.today()
            last_day = calendar.monthrange(today.year, today.month)[1]
            period_start = date(today.year, today.month, 1)
            period_end = date(today.year, today.month, last_day)
            start_dt = datetime.combine(period_start, dtime(0, 0))
            end_dt = datetime.combine(period_end, dtime(23, 59))
            period_days = int(hydrant_workdays)
            vehicle = f"소방차 {int(hydrant_vehicle_count)}대"
            inspect_weekdays = []
            inspect_teams = 1
            inspect_daily_hours = 6.0
            inspect_minutes = 40
            inspect_targets_per_day = 2
            inspect_capacity = 0
            inspect_dates = []
            st.caption(
                f"기본 {int(hydrant_target_min)}분 이내로 편성하고, 차량별 노선이 "
                f"{int(hydrant_workdays)}개를 넘으면 시간을 늘리도록 안내합니다."
            )
        elif purpose == "other":
            card_title(2, "지휘관 현장방문 조건")
            visit_date = date.today()
            period_start = period_end = visit_date
            start_dt = datetime.combine(visit_date, dtime(9, 0))
            end_dt = datetime.combine(visit_date, dtime(18, 0))
            period_days = 1
            vehicle = ""
            inspect_weekdays = []
            inspect_teams = 1
            inspect_daily_hours = 6.0
            inspect_minutes = 40
            inspect_targets_per_day = 2
            inspect_capacity = 0
            inspect_dates = []
            st.caption(
                f"지휘관 {int(commander_count)}명 기준 자동 분할 · 편도 {int(commander_oneway_limit)}분 이내 · "
                f"현장당 {int(commander_stop_min)}분을 기준으로 편성합니다."
            )
        else:
            card_title(2, "순찰 기간 · 순찰 차량")
            inspect_weekdays = []
            inspect_teams = 1
            inspect_daily_hours = 6.0
            inspect_minutes = 40
            inspect_targets_per_day = 2
            inspect_capacity = 0
            inspect_dates = []

            dc1, dc2, dc3, dc4 = st.columns(4)
            with dc1:
                period_start = st.date_input("시작일", key="period_start")
            with dc2:
                period_start_time = st.time_input("시작 시각", key="period_start_time")
            with dc3:
                period_end = st.date_input("종료일", key="period_end")
            with dc4:
                period_end_time = st.time_input("종료 시각", key="period_end_time")

            start_dt = datetime.combine(period_start, period_start_time)
            end_dt = datetime.combine(period_end, period_end_time)
            if end_dt <= start_dt:
                st.warning("⚠ 종료 일시가 시작 일시보다 빠릅니다. 기간을 확인해주세요.")
                period_days = 1
            else:
                period_days = max(1, math.ceil((end_dt - start_dt).total_seconds() / 86400))
                st.caption(f"총 {period_days}일간")

            vehicle = st.selectbox("순찰 차량", ["소방차", "구급차", "행정차", "개인차"], index=0)

    st.write("")

    # 상세 노선 조건은 별도로 입력받지 않고, 위에서 선택한 순찰방법과 일정 조건으로
    # 자동 결정한다. 화면에는 불필요한 설정 영역을 표시하지 않는다.
    seg_max_km = seg_max_min = None
    long_threshold = 99999.0
    candidate_k = 5

    if purpose == "season":
        mode = "target_time"
        target_min = int(season_target_min)
        target_min_high = target_min
        max_per_route = 100
        max_routes_cap = 0
        basis_label = "소요시간 기준"
        basis = "time"
    elif purpose == "hydrant":
        mode = "target_time"
        target_min = int(hydrant_target_min)
        target_min_high = target_min
        max_per_route = 100
        max_routes_cap = 0
        basis_label = "소요시간 기준"
        basis = "time"
    elif purpose == "other":
        mode = "fixed"
        target_min = target_min_high = None
        max_per_route = 100
        max_routes_cap = int(commander_route_count)
        basis_label = "거리 기준"
        basis = "distance"
    elif purpose == "inspect":
        mode = "fixed"
        target_min = int(float(inspect_daily_hours) * 60)
        target_min_high = target_min
        max_per_route = int(inspect_targets_per_day)
        max_routes_cap = len(inspect_dates) * int(inspect_teams)
        basis_label = "검사일·팀별 대상 수 기준"
        basis = "time"
    else:
        # 특별경계근무는 가까운 대상부터 노선당 5개소씩 자동 편성한다.
        mode = "fixed"
        target_min = target_min_high = None
        max_per_route = 5
        max_routes_cap = 6
        basis_label = "거리 기준"
        basis = "distance"
        long_threshold = 15.0

    coord_api_calls = int(st.session_state.get("coord_api_calls", 0))
    max_calls = max(0, API_CALL_LIMIT - coord_api_calls)

    st.write("")
    next_tab_button("3단계로 이동", 2, enabled=True)


with page_build:
    # 출발지(소방서·센터) 자신이 순찰 대상 목록에 섞여 있으면 제외한다.
    # (업로드 파일 첫 줄에 소방서를 넣어두는 경우가 많아, 그대로 두면 소방서가 경유지로 잡힌다)
    excluded_station_rows = 0
    if df is not None and len(df):
        def _norm(v):
            return re.sub(r"\s+", "", str(v)) if v is not None else ""

        st_name_n, st_addr_n = _norm(station_name), _norm(station_address)
        mask_keep = []
        for _, r in df.iterrows():
            vals = [_norm(v) for v in r.values]
            is_station = any(v and (v == st_name_n or v == st_addr_n) for v in vals)
            mask_keep.append(not is_station)
        excluded_station_rows = len(df) - sum(mask_keep)
        if excluded_station_rows:
            df = df[pd.Series(mask_keep, index=df.index)].reset_index(drop=True)

    # ----------------------------------------------------------------------------
    # 좌표 확인·수정 표 + 노선 생성 버튼
    # (설정 요약·업로드 데이터 확인 등은 걷어내고, 좌표 수정과 실행 버튼만 남긴다)
    # ----------------------------------------------------------------------------
    if df is not None and len(df):
        cols = list(df.columns)
        name_col_guess_idx = find_name_column_index(cols)
        addr_col_guess_idx = find_address_column_index(cols, name_col_guess_idx)

        name_col = cols[name_col_guess_idx]
        addr_col = cols[addr_col_guess_idx]

        lat_col_guess = next((c for c in cols if "위도" in str(c) or str(c).lower() == "lat"), None)
        lng_col_guess = next((c for c in cols if "경도" in str(c) or str(c).lower() in ("lng", "lon")), None)
        has_coords = bool(lat_col_guess and lng_col_guess)
        coord_mode = "file" if has_coords else "geocode"

        n_targets = len(df)

        saved_coords = st.session_state.get("coords_df")
        coords_complete = (saved_coords is not None and len(saved_coords) == n_targets
                           and saved_coords["위도"].notna().all()
                           and saved_coords["경도"].notna().all())
        if st.session_state.get("coord_future") is not None:
            st.info("🔍 좌표 검색 중입니다. 1단계 기본정보에서 완료 상태를 확인하세요.")
        elif saved_coords is None:
            st.warning("⚠️ 1단계 기본정보에서 좌표를 먼저 검색해주세요.")

        coords_df = st.session_state.get("coords_df")

        if coords_df is not None:
            with st.container(border=True):
                st.markdown("### 🔎 좌표 확인 및 수정")
                ok_n = int(coords_df["위도"].notna().sum())
                fail_n = int(coords_df["위도"].isna().sum())
                k1, k2, k3 = st.columns(3)
                k1.metric("전체", f"{len(coords_df)}")
                k2.metric("좌표 확보", f"{ok_n}")
                k3.metric("좌표 없음", f"{fail_n}")

                if fail_n:
                    st.error(
                        f"❌ {fail_n}건은 좌표를 찾지 못했습니다. 1단계의 **좌표 실패건 처리**에서 "
                        "주소 수정 후 재검색·좌표 직접입력·대상 제외 중 하나를 선택하거나, "
                        "아래 표의 위도·경도 칸에 직접 입력하세요."
                    )
                else:
                    st.success("✅ 모든 대상의 좌표가 확보되었습니다. 아래에서 노선을 생성하세요.")

                st.caption("위도·경도 칸은 직접 고칠 수 있습니다. 수정하면 그 값이 노선 생성에 그대로 쓰입니다.")
                edited = st.data_editor(
                    coords_df, use_container_width=True, hide_index=True, num_rows="fixed",
                    key="coords_editor",
                    column_config={
                        "대상명": st.column_config.TextColumn(disabled=True, width="medium"),
                        "주소": st.column_config.TextColumn(disabled=True, width="large"),
                        "위도": st.column_config.NumberColumn(format="%.6f", help="예: 35.919000"),
                        "경도": st.column_config.NumberColumn(format="%.6f", help="예: 128.283000"),
                        "상태": st.column_config.TextColumn(disabled=True, width="small"),
                        "비고": st.column_config.TextColumn(disabled=True, width="large"),
                    },
                )
                # 사용자가 별도 저장 버튼을 누르지 않아도 수정된 좌표를 현재 작업에 즉시 보존한다.
                st.session_state["coords_df"] = edited.copy()

                st.markdown(
                    '''<div style="margin-top:0.75rem;padding:0.9rem 1rem;
                        border:1px solid #48a774;border-left:6px solid #238553;border-radius:10px;
                        background:#e5f6ed;color:#155f3b;font-weight:750;line-height:1.55;">
                        ✅ 확정된 좌표는 현재 작업 동안 백그라운드에 자동 저장되어 노선 생성에 바로 반영됩니다.<br>
                        <span style="font-weight:550;color:#28704b;">별도의 저장 버튼을 누르지 않아도 됩니다.</span>
                    </div>''',
                    unsafe_allow_html=True,
                )

            ready = edited["위도"].notna() & edited["경도"].notna()
            n_ready = int(ready.sum())
            if candidate_k:
                est_calls = n_ready * candidate_k + n_ready
            else:
                est_calls = n_ready * (n_ready + 1) // 2 + n_ready
            if n_ready < len(edited):
                safety_warning(
                    f"좌표가 없는 {len(edited) - n_ready}건은 노선에서 제외됩니다. "
                    "1단계의 좌표 실패건 처리에서 재검색·좌표 직접입력·제외 중 하나를 선택하세요.",
                    title="좌표 없는 대상 안내",
                )

            if purpose == "inspect":
                inspect_capacity = (
                    len(inspect_dates) * int(inspect_teams) * int(inspect_targets_per_day)
                )
                inspect_omitted_count = max(0, n_ready - inspect_capacity)
                capacity_formula = (
                    f"검사 가능일 {len(inspect_dates)}일 × {int(inspect_teams)}팀 × "
                    f"팀당 하루 {int(inspect_targets_per_day)}개소 = 최대 {inspect_capacity}개소"
                )
                if inspect_omitted_count:
                    inspection_capacity_warning(
                        capacity_formula,
                        n_ready,
                        inspect_omitted_count,
                    )
                elif inspect_capacity:
                    st.success(f"✅ {capacity_formula} · 전체 {n_ready}개소를 기간 안에 검사할 수 있습니다.")
                else:
                    st.error("검사 가능한 날짜가 없습니다. 검사기간 또는 검사 가능 요일을 조정하세요.")

            run_disabled = (
                not has_keys() or n_ready == 0 or
                (purpose == "inspect" and inspect_capacity == 0)
            )
            run_col1, run_col2 = st.columns(2)
            with run_col1:
                run = st.button(
                    "🚒 노선 생성 시작", type="primary",
                    disabled=run_disabled,
                    use_container_width=True,
                )
            with run_col2:
                rerun_same_condition = st.button(
                    "🔁 같은 조건으로 노선 재탐색",
                    type="secondary",
                    disabled=run_disabled,
                    help="주소·일정·팀 수는 그대로 두고 다른 후보 순서로 노선을 다시 탐색합니다.",
                    use_container_width=True,
                )
            if run:
                st.session_state["route_search_variant"] = 0
            if rerun_same_condition:
                st.session_state["route_search_variant"] = (
                    int(st.session_state.get("route_search_variant", 0)) + 1
                )
                for stale_key in ("route_results", "far_points", "meta"):
                    st.session_state.pop(stale_key, None)
                run = True
        else:
            run = False
            edited = None
            st.info("먼저 위의 좌표 확인 및 수정에서 좌표를 확정해 주세요.")
    else:
        run = False
        edited = None
        st.info("먼저 1단계 기본정보에서 대상 목록을 업로드해 주세요.")

    if run:
        route_variant = int(st.session_state.get("route_search_variant", 0))
        # ---- 중단 장치 ----------------------------------------------------
        # ① 수동 중단: 아래 '중단' 버튼을 누르면 Streamlit이 새로 실행되면서
        #    지금 돌고 있는 계산이 즉시 멈춘다.
        # ② 자동 중단: 호출 수가 한도(max_calls)를 넘으면 그때까지 만든 노선만 남기고 멈춘다.
        stop_box = st.container()
        with stop_box:
            st.button("⛔ 계산 중단", key="stop_btn", type="secondary",
                      help="지금 진행 중인 노선 생성을 즉시 멈춥니다.")

        call_counter = {"n": 0}
        limit_hit = {"v": False}

        def over_limit():
            if call_counter["n"] >= max_calls:
                limit_hit["v"] = True
                return True
            return False

        # 1) 기본정보에서 검색·확정한 출발부서 좌표 사용
        s_lat, s_lng = station_lat, station_lng
        if s_lat is None or s_lng is None:
            st.error("1단계 기본정보에서 출발부서를 검색하거나 현 위치를 조회해주세요.")
            st.stop()
        station = {"name": station_name, "lat": s_lat, "lng": s_lng}

        # 2) 1단계에서 확정한 좌표를 그대로 사용 (여기서는 지오코딩을 하지 않는다)
        points = []
        for _, r in edited.iterrows():
            try:
                lat, lng = float(r["위도"]), float(r["경도"])
            except (TypeError, ValueError):
                continue
            if math.isnan(lat) or math.isnan(lng):
                continue
            points.append({"name": str(r["대상명"]), "address": str(r["주소"]),
                           "lat": lat, "lng": lng})

        if not points:
            st.error("좌표가 있는 대상이 없습니다. 좌표 확인 및 수정에서 좌표를 확정해 주세요.")
            st.stop()

        # 3) 용도별 원거리 판정
        if purpose == "hydrant":
            normal_points = allocate_hydrants_by_distribution(
                points, station, int(hydrant_vehicle_count), hydrant_distribution_basis
            )
            far_points = []
        elif purpose == "inspect":
            # 예방검사는 거리에 관계없이 업로드한 모든 대상을 반드시 포함한다.
            normal_points = points
            far_points = []
        elif purpose == "other":
            long_progress = st.progress(0.0, text="출발지 기준 편도시간 확인 중...")

            def bump(total_hint=len(points)):
                call_counter["n"] += 1
                long_progress.progress(
                    min(call_counter["n"] / max(total_hint, 1), 1.0),
                    text=f"실제 도로시간 API 호출 중... ({call_counter['n']:,}/{max_calls:,}회)",
                )

            normal_points, far_points = separate_long_time(
                points, station, float(commander_oneway_limit),
                "편도 허용시간 초과 - 별도 지휘구역 검토",
                on_call=bump, should_stop=over_limit,
            )
            long_progress.empty()
        else:
            long_progress = st.progress(0.0, text="소방서 기준 실도로거리 확인 중...")

            def bump(total_hint=len(points)):
                call_counter["n"] += 1
                long_progress.progress(min(call_counter["n"] / max(total_hint, 1), 1.0),
                                       text=f"실제 도로거리 API 호출 중... "
                                            f"({call_counter['n']:,}/{max_calls:,}회)")

            if purpose == "season":
                if season_limit_basis == "편도시간(분)":
                    normal_points, far_points = separate_long_time(
                        points, station, float(season_oneway_limit), season_delegate,
                        on_call=bump, should_stop=over_limit,
                    )
                else:
                    normal_points, far_points = separate_long_distance(
                        points, station, float(season_oneway_limit), on_call=bump,
                        save_calls=False, should_stop=over_limit,
                    )
                    far_points = [{**point, "권장수행": season_delegate} for point in far_points]
            else:
                normal_points, far_points = separate_long_distance(
                    points, station, long_threshold, on_call=bump, save_calls=bool(candidate_k),
                    should_stop=over_limit,
                )
            long_progress.empty()

        # 4) 노선 편성
        build_progress = st.empty()

        def bump_build():
            call_counter["n"] += 1
            if purpose == "hydrant":
                build_progress.text("지리조사 노선을 자동 편성하고 있습니다...")
            else:
                build_progress.text(f"실도로 기준 노선 편성 중... "
                                    f"(API 호출 {call_counter['n']:,}/{max_calls:,}회)")

        if purpose == "hydrant":
            routes = []
            unassigned = []
            for vehicle_no in range(1, int(hydrant_vehicle_count) + 1):
                vehicle_points = [p for p in normal_points if p.get("vehicle_no") == vehicle_no]
                if not vehicle_points:
                    continue
                vehicle_routes, vehicle_unassigned = build_routes(
                    vehicle_points, station, "target_time", 100,
                    None, None, int(hydrant_target_min),
                    None, basis="time", on_call=bump_build,
                    candidate_k=candidate_k, should_stop=over_limit,
                    service_min_per_stop=int(hydrant_inspection_min),
                    route_variant=route_variant,
                )
                routes.extend(vehicle_routes)
                unassigned.extend(vehicle_unassigned)
        elif purpose == "other":
            commander_per_route = max(1, math.ceil(len(normal_points) / commander_route_count))
            routes, unassigned = build_routes(
                normal_points, station, "fixed", commander_per_route,
                None, None, None, commander_route_count,
                basis="distance", on_call=bump_build,
                candidate_k=candidate_k, should_stop=over_limit,
                service_min_per_stop=int(commander_stop_min),
                route_variant=route_variant,
            )
        else:
            routes, unassigned = build_routes(
                normal_points, station, mode, max_per_route,
                seg_max_km, seg_max_min, target_min_high,
                (max_routes_cap if purpose == "inspect" else max_routes_cap or None),
                basis=basis, on_call=bump_build,
                candidate_k=candidate_k, should_stop=over_limit,
                service_min_per_stop=(int(season_stop_min) if purpose == "season" else
                                      int(inspect_minutes) if purpose == "inspect" else 0),
                strict_route_cap=(purpose == "inspect"),
                route_variant=route_variant,
            )
        build_progress.empty()

        if limit_hit["v"]:
            if purpose == "hydrant":
                st.warning(
                    f"⛔ 계산 범위 한도에 도달해 편성을 중단했습니다. "
                    f"그때까지 편성된 {len(routes)}개 노선은 아래에 표시됩니다."
                )
            else:
                st.warning(
                    f"⛔ API 호출 한도({max_calls:,}회)에 도달해 노선 편성을 중단했습니다. "
                    f"그때까지 편성된 {len(routes)}개 노선은 아래에 그대로 표시됩니다. "
                    "한도를 늘리거나 'API 호출 절약'을 켜고 다시 실행해 보세요."
                )

        inspect_omitted_points = unassigned[:] if purpose == "inspect" else []
        if unassigned and purpose != "inspect":
            for p in unassigned:
                if over_limit():
                    km = haversine_km(
                        (station["lat"], station["lng"]), (p["lat"], p["lng"])
                    ) * ROAD_FACTOR
                else:
                    km, _ = real_leg(station, p, on_call=bump_build)
                far_points.append({**p, "도로거리_km": round(km, 1)})

        # 용도별 부가 정보
        team_info = ""
        if purpose == "hydrant":
            base_count, extra_count = divmod(len(points), max(int(hydrant_member_count), 1))
            count_text = (f"{base_count}~{base_count + 1}개" if extra_count else f"{base_count}개")
            team_info = (f" · {hydrant_member_count}명 개인별 {count_text}"
                         f" · 차량 {hydrant_vehicle_count}대 · 월 {hydrant_workdays}근무일"
                         f" · {hydrant_distribution_basis}")
        elif purpose == "season":
            limit_unit = "분" if season_limit_basis == "편도시간(분)" else "km"
            team_info = (f" · 관할 전체 대상 균등 순환 · {season_actor}/{season_vehicle}"
                         f" · 1회 최대 {int(season_target_min)}분"
                         f" · 편도 {season_oneway_limit:g}{limit_unit} 제한")
        elif purpose == "other":
            team_info = (
                f" · 지휘관 {int(commander_count)}명 · 자동 생성구역 {len(routes)}개"
                f" · 편도 {int(commander_oneway_limit)}분 이내"
                f" · 현장당 {int(commander_stop_min)}분"
            )
        elif purpose == "inspect":
            available_team_days = len(inspect_dates) * int(inspect_teams)
            assigned_targets = sum(len(route) for route in routes)
            team_info = (f" · 검사 가능일 {len(inspect_dates)}일 · {inspect_teams}팀"
                         f" · 팀당 하루 최대 {int(inspect_targets_per_day)}개소")
            if inspect_omitted_points:
                inspection_capacity_warning(
                    (f"검사 가능일 {len(inspect_dates)}일 × {int(inspect_teams)}팀 × "
                     f"팀당 하루 {int(inspect_targets_per_day)}개소 = 최대 {inspect_capacity}개소"),
                    len(points),
                    len(inspect_omitted_points),
                )
            else:
                st.success(
                    f"✅ 검사 가능일 {len(inspect_dates)}일, {int(inspect_teams)}팀, "
                    f"팀당 하루 최대 {int(inspect_targets_per_day)}개소 조건으로 "
                    f"전체 {assigned_targets}개소를 기간 안에 편성했습니다."
                )
        elif purpose == "guard" and guard_repeat_label == "매일 같은 코스 반복" and guard_rounds:
            total_runs = int(guard_rounds.replace("회", "")) * period_days
            team_info = f" · 매일 같은 코스로 하루 {guard_rounds} 반복({period_days}일간 총 {total_runs}회)"
        elif purpose == "guard":
            team_info = f" · 매일 다른 코스로 순환({period_days}일간 {len(routes)}개 노선 배정)"

        far_word = "편도 기준 초과" if purpose in ("season", "other") else "장거리 별도"
        far_summary = "" if purpose == "inspect" else f" ({far_word} {len(far_points)}개소)"
        st.success(f"[{purpose_label}] 총 {len(routes)}개 노선, {sum(len(r) for r in routes)}개소 배정 완료 "
                   f"{far_summary}{team_info}")
        # 5) 확정 노선의 구간별 실도로거리·경로좌표
        route_results = []
        total_calls = sum(len(r) + 1 for r in routes)
        call_progress = st.progress(0.0, text="노선별 실도로 경로 확정 중...")
        done = 0
        for ri, route in enumerate(routes):
            legs = []
            cur = station
            acc_km = 0.0
            acc_min = 0.0
            all_path = []
            for p in route:
                km, mins, path = road_route(cur["lat"], cur["lng"], p["lat"], p["lng"])
                if km is None:
                    km = haversine_km((cur["lat"], cur["lng"]), (p["lat"], p["lng"])) * ROAD_FACTOR
                    mins = km / AVG_SPEED_KMH * 60
                    path = [(cur["lat"], cur["lng"]), (p["lat"], p["lng"])]
                service_min = (int(hydrant_inspection_min) if purpose == "hydrant" else
                               int(season_stop_min) if purpose == "season" else
                               int(commander_stop_min) if purpose == "other" else 0)
                if purpose == "inspect":
                    service_min = int(inspect_minutes)
                legs.append({"from": cur["name"], "to": p["name"], "to_address": p.get("address", ""),
                             "km": km, "min": mins, "inspection_min": service_min,
                             "assigned_to": p.get("assigned_to", ""),
                             "vehicle_no": p.get("vehicle_no"),
                             "lat": p["lat"], "lng": p["lng"]})
                acc_km += km
                acc_min += mins + service_min
                all_path += path
                cur = p
                done += 1
                call_progress.progress(min(done / max(total_calls, 1), 1.0), text="노선별 실도로 경로 확정 중...")
            back_km, back_min, back_path = road_route(cur["lat"], cur["lng"], station["lat"], station["lng"])
            if back_km is None:
                back_km = haversine_km((cur["lat"], cur["lng"]), (station["lat"], station["lng"])) * ROAD_FACTOR
                back_min = back_km / AVG_SPEED_KMH * 60
                back_path = [(cur["lat"], cur["lng"]), (station["lat"], station["lng"])]
            acc_km += back_km
            acc_min += back_min
            all_path += back_path
            done += 1
            call_progress.progress(min(done / max(total_calls, 1), 1.0), text="노선별 실도로 경로 확정 중...")

            result = {
                "route_no": ri + 1, "stops": route, "legs": legs,
                "vehicle_no": route[0].get("vehicle_no") if route else None,
                "assigned_members": sorted({p.get("assigned_to", "") for p in route if p.get("assigned_to")}),
                "back_km": back_km, "back_min": back_min,
                "total_km": acc_km, "total_min": acc_min, "path": all_path,
            }
            if purpose == "inspect" and inspect_dates:
                date_index = ri // int(inspect_teams)
                if date_index < len(inspect_dates):
                    result["inspection_date"] = inspect_dates[date_index]
                    result["inspection_team"] = ri % int(inspect_teams) + 1
            route_results.append(result)
        call_progress.empty()

        if purpose == "hydrant":
            route_counts = {
                vehicle_no: sum(1 for result in route_results if result.get("vehicle_no") == vehicle_no)
                for vehicle_no in range(1, int(hydrant_vehicle_count) + 1)
            }
            over_vehicles = {v: count for v, count in route_counts.items() if count > int(hydrant_workdays)}
            if over_vehicles:
                detail = ", ".join(f"{v}호차 {count}개 노선" for v, count in over_vehicles.items())
                suggested = min(
                    int(hydrant_max_min),
                    math.ceil(int(hydrant_target_min) * max(over_vehicles.values()) / int(hydrant_workdays) / 10) * 10,
                )
                st.warning(
                    f"월 {int(hydrant_workdays)}번의 당번일을 초과하는 차량이 있습니다: {detail}. "
                    f"우선 목표시간을 약 {suggested}분으로 늘려 다시 편성해 보세요. "
                    f"{int(hydrant_max_min)}분에서도 10개 이내가 되지 않으면 인원 또는 차량 편성을 조정해야 합니다."
                )
            else:
                detail = " · ".join(f"{v}호차 {count}개 노선" for v, count in route_counts.items())
                st.success(f"월 {int(hydrant_workdays)}번의 당번일 안에 전수조사가 가능합니다. {detail}")
        elif purpose == "season":
            required_routes = len(route_results)
            if required_routes > period_days:
                st.warning(
                    f"전 대상을 누락 없이 순찰하려면 1회 최대 {int(season_target_min)}분 기준으로 "
                    f"최소 {required_routes}일이 필요하지만 "
                    f"설정한 기간은 {period_days}일입니다. 기간을 {required_routes}일 이상으로 늘리거나 "
                    "1회 최대시간을 늘려 다시 편성해주세요."
                )
            elif required_routes:
                base_runs, extra_runs = divmod(period_days, required_routes)
                for route_index, result in enumerate(route_results):
                    result["period_runs"] = base_runs + (1 if route_index < extra_runs else 0)
                min_runs = base_runs
                max_runs = base_runs + (1 if extra_runs else 0)
                run_text = f"{min_runs}회" if min_runs == max_runs else f"{min_runs}~{max_runs}회"
                st.success(
                    f"일반 순찰 대상 {sum(len(result['stops']) for result in route_results)}개소를 "
                    f"{required_routes}개 코스로 나누었습니다. {period_days}일 동안 코스를 차례로 순환하면 "
                    f"각 코스와 소속 대상은 예상 {run_text} 방문합니다."
                )
                if far_points:
                    st.info(
                        f"편도 기준을 넘는 {len(far_points)}개소는 출동 공백을 줄이기 위해 "
                        f"{season_delegate} 대상으로 별도 안내합니다."
                    )

        st.session_state["station"] = station
        st.session_state["route_results"] = route_results
        st.session_state["far_points"] = far_points
        st.session_state["meta"] = {
            "title": patrol_title, "purpose": purpose_label, "vehicle": vehicle,
            "period": (f"{period_start:%Y-%m-%d} ~ {period_end:%Y-%m-%d} "
                       f"(검사 가능일 {len(inspect_dates)}일)" if purpose == "inspect" else
                       "" if purpose == "hydrant" else
                       f"{period_start:%Y-%m-%d}" if purpose == "other" else
                       f"{start_dt:%Y-%m-%d %H:%M} ~ {end_dt:%Y-%m-%d %H:%M} ({period_days}일간)"),
            "period_label": ("검사기간" if purpose == "inspect" else
                             "" if purpose == "hydrant" else
                             "방문일" if purpose == "other" else "순찰기간"),
            "basis": basis_label, "route_prefix": route_prefix, "team_info": team_info.strip(" ·"),
            "target_min": target_min,
            "hydrant_distribution_basis": hydrant_distribution_basis,
            "route_search_variant": route_variant,
            "api_calls_used": coord_api_calls + call_counter["n"] + total_calls,
            "api_call_limit": API_CALL_LIMIT,
            "validated_target_count": 217,
        }

    # ----------------------------------------------------------------------------
    # 결과 표시 (구 ⑤ 노선 결과 탭 내용 — 이제 이 탭 안에서 이어서 보여준다)
    # ----------------------------------------------------------------------------
    if st.session_state.get("stop_btn"):
        st.info("⛔ 계산을 중단했습니다. (중단 시점까지의 계산 결과는 저장되지 않습니다. "
                "직전에 완료된 결과가 있으면 아래에 그대로 남아 있습니다.)")

    if "route_results" in st.session_state:
        station = st.session_state["station"]
        route_results = st.session_state["route_results"]
        far_points = st.session_state["far_points"]
        meta = st.session_state.get("meta", {})

        st.write("")
        st.divider()
        st.header("📍 노선 생성 결과")
        if meta:
            period_summary = (f" · {meta.get('period_label')} {meta.get('period')}"
                              if meta.get("period_label") and meta.get("period") else "")
            st.caption(f"**{meta.get('title','')}** · {meta.get('purpose','')} · 기준: {meta.get('basis','')}"
                       + period_summary
                       + (f" · 차량: {meta['vehicle']}" if meta.get("vehicle") else "")
                       + (f" · {meta['team_info']}" if meta.get("team_info") else ""))
        if meta.get("purpose") == "① 지휘관 현장방문":
            safety_warning(
                "이 노선은 평시 순찰계획과 사전 검토용입니다. "
                "실제 재난현장에서는 실시간 상황이 반영되지 않으므로, "
                "본 계산만으로 지휘·대응을 결정하지 말고 현장지휘관의 판단과 공식 지휘체계를 우선하십시오."
            )

        metric_columns = st.columns(4 if meta.get("purpose") == "④ 예방검사" else 5)
        m1, m2, m3, m4 = metric_columns[:4]
        m1.metric("생성 노선 수", f"{len(route_results)}")
        m2.metric("전체 방문지", f"{sum(len(r['stops']) for r in route_results)}")
        m3.metric("총 이동거리(km)", f"{sum(r['total_km'] for r in route_results):.1f}")
        m4.metric("노선 평균시간", f"{sum(r['total_min'] for r in route_results) / max(len(route_results), 1):.0f}분")
        if meta.get("purpose") != "④ 예방검사":
            m5 = metric_columns[4]
            m5.metric("편도 기준 초과" if meta.get("purpose") in ("① 지휘관 현장방문", "③ 계절순찰") else "원거리 분리 대상",
                      f"{len(far_points)}")
        if meta.get("purpose") == "③ 계절순찰":
            visit_runs = [r.get("period_runs", 0) for r in route_results if r.get("period_runs")]
            if visit_runs:
                repeat_text = (f"{min(visit_runs)}회" if min(visit_runs) == max(visit_runs)
                               else f"{min(visit_runs)}~{max(visit_runs)}회")
                st.info(f"🔁 설정 기간 동안 같은 대상의 예상 방문 횟수: {repeat_text}")

        # 결과 완성 안내 — 특정 노선을 대표로 크게 보여주지 않고, 생성된 노선 수만큼
        # 아래 지도 그리드에서 전부 동일하게 보여준다(노선 2 이상이 묻히지 않도록).
        if route_results:
            st.markdown(
                f"""
                <div style="margin:1rem 0 1.1rem;padding:1.25rem 1.4rem;border-radius:18px;
                            background:linear-gradient(135deg,#e9f8ef 0%,#f5fbf7 55%,#fff8df 100%);
                            border:1px solid #9bcdb0;box-shadow:0 10px 28px -18px rgba(25,100,62,.65);">
                  <div style="font-size:1.45rem;font-weight:850;color:#165d39;margin-bottom:.3rem;">
                    🎉 노선이 완성되었습니다!
                  </div>
                  <div style="font-size:1rem;font-weight:650;color:#29483a;">
                    아래에서 생성된 {len(route_results)}개 노선을 지도로 모두 확인하세요.
                    지도 아래 '상세보기'를 열면 방문순서·카카오맵·QR코드가 나옵니다.
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            api_calls_used = int(meta.get("api_calls_used", 0))
            api_call_limit = int(meta.get("api_call_limit", API_CALL_LIMIT))
            validated_target_count = int(meta.get("validated_target_count", 217))
            st.markdown(
                f"""
                <div style="margin:-.2rem 0 1.25rem;padding:1rem 1.2rem;border-radius:14px;
                            background:#f5f8ff;border:1px solid #b8c8e8;
                            box-shadow:0 8px 20px -18px rgba(31,71,135,.7);">
                  <div style="font-size:.92rem;font-weight:750;color:#284f84;margin-bottom:.35rem;">
                    ⚙️ 대규모 데이터 처리 규모
                  </div>
                  <div style="font-size:1.12rem;font-weight:800;color:#17375f;">
                    이번 작업 사용 API 호출 <span style="color:#126f4b;">{api_calls_used:,}건</span>
                    <span style="color:#68788d;font-weight:650;"> / 일일 기준 참고 한도 {api_call_limit:,}건</span>
                  </div>
                  <div style="margin-top:.3rem;font-size:.88rem;color:#53657b;">
                    조회 대상이 많으면 한 작업에서도 수백~천 건 이상 사용할 수 있어, 하루 사용량 기준으로 표시합니다.
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        # ---- 담당 조 · 조원 입력(지리조사는 개인정보 보호를 위해 엑셀에서만 작성) ----
        st.markdown("### 📌 다음 작업")
        is_center_route = meta.get("purpose") == "⑤ 지리조사(센터용)"
        if is_center_route:
            st.caption("개인정보 보호를 위해 담당 조·조원은 화면에서 입력하지 않습니다. 결과자료를 내려받은 뒤 엑셀에서 작성하세요.")
            action_right = st.container()
        else:
            st.caption("담당자를 입력하거나 완성된 결과자료를 내려받으세요.")
            action_left, action_right = st.columns(2)
            with action_left:
                with st.expander("👥 담당 조·조원 입력 (선택)", expanded=False):
                    st.caption("필요한 경우에만 입력하세요. 입력 내용은 최종 엑셀 파일에 반영됩니다.")
                    for rr in route_results:
                        st.markdown(f"**노선 {rr['route_no']}**")
                        st.text_input("담당 조 이름", key=f"team_name_{rr['route_no']}",
                                      placeholder="예) 가천1팀1조", label_visibility="collapsed")
                        st.text_input("조원", key=f"team_members_{rr['route_no']}",
                                      placeholder="조원 예) 홍길동, 이순신", label_visibility="collapsed")

        # ---- 엑셀(xlsx) 다운로드: 노선 1개 = 1행, 경유지가 옆으로 펼쳐지는 가로형 ----
        def build_wide_excel(station, route_results, far_points, meta):
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
            from openpyxl.utils import get_column_letter

            wb = Workbook()
            ws = wb.active
            ws.title = "순찰노선"

            prefix = (meta.get("route_prefix") or "").strip() or station["name"]
            base_cols = ["연번", "부서명", "노선이름", "구분", "담당 조", "조원", "출발지"]
            max_stops = max((len(rr["legs"]) for rr in route_results), default=0)

            thin = Side(style="thin", color="9AA59D")
            border = Border(left=thin, right=thin, top=thin, bottom=thin)
            head_fill = PatternFill("solid", fgColor="F4DDD8")
            head_font = Font(bold=True, size=10)
            center = Alignment(horizontal="center", vertical="center", wrap_text=True)

            # 제목 줄
            total_cols = len(base_cols) + max_stops * 3 + 3
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(total_cols, 1))
            title_parts = [value for value in (
                meta.get("purpose", ""), meta.get("vehicle", ""), meta.get("period", "")
            ) if value]
            title_cell = ws.cell(
                row=1, column=1,
                value=f"{meta.get('title', '순찰노선')}   [{ ' · '.join(title_parts) }]",
            )
            title_cell.font = Font(bold=True, size=13)
            title_cell.alignment = Alignment(horizontal="center", vertical="center")

            HEAD1, HEAD2, DATA0 = 2, 3, 4

            for i, name in enumerate(base_cols, start=1):
                ws.merge_cells(start_row=HEAD1, start_column=i, end_row=HEAD2, end_column=i)
                ws.cell(row=HEAD1, column=i, value=name)

            col = len(base_cols) + 1
            for i in range(1, max_stops + 1):
                ws.merge_cells(start_row=HEAD1, start_column=col, end_row=HEAD1, end_column=col + 2)
                ws.cell(row=HEAD1, column=col, value=f"경유{i}")
                ws.cell(row=HEAD2, column=col, value="대상명(주소)")
                ws.cell(row=HEAD2, column=col + 1, value="거리(km)")
                ws.cell(row=HEAD2, column=col + 2, value="누적(km)")
                col += 3

            for label in ("귀소", "노선거리(km)", "순찰 총 소요시간(분)"):
                ws.merge_cells(start_row=HEAD1, start_column=col, end_row=HEAD2, end_column=col)
                ws.cell(row=HEAD1, column=col, value=label)
                col += 1
            last_col = col - 1

            for r_ in (HEAD1, HEAD2):
                for c_ in range(1, last_col + 1):
                    cell = ws.cell(row=r_, column=c_)
                    cell.fill = head_fill
                    cell.font = head_font
                    cell.alignment = center
                    cell.border = border

            r = DATA0
            for rr in route_results:
                no = rr["route_no"]
                c = 1
                ws.cell(row=r, column=c, value=no); c += 1
                ws.cell(row=r, column=c, value=station["name"]); c += 1
                ws.cell(row=r, column=c, value=f"{prefix}노선{no}"); c += 1
                ws.cell(row=r, column=c, value="근거리"); c += 1
                auto_team = f"{rr.get('vehicle_no')}호차" if rr.get("vehicle_no") else ""
                auto_members = ", ".join(rr.get("assigned_members") or [])
                if meta.get("purpose") == "⑤ 지리조사(센터용)":
                    team_value = ""
                    members_value = ""
                else:
                    team_value = st.session_state.get(f"team_name_{no}", "") or auto_team
                    members_value = st.session_state.get(f"team_members_{no}", "") or auto_members
                ws.cell(row=r, column=c, value=team_value); c += 1
                ws.cell(row=r, column=c, value=members_value); c += 1
                ws.cell(row=r, column=c, value=station["name"]); c += 1
                acc_km = 0.0
                for leg in rr["legs"]:
                    acc_km += leg["km"]
                    ws.cell(row=r, column=c, value=stop_label(leg["to"], leg.get("to_address"))); c += 1
                    ws.cell(row=r, column=c, value=round(leg["km"], 1)); c += 1
                    ws.cell(row=r, column=c, value=round(acc_km, 1)); c += 1
                c += (max_stops - len(rr["legs"])) * 3  # 경유지 수가 적은 노선은 빈칸 패딩
                ws.cell(row=r, column=c, value=station["name"]); c += 1
                ws.cell(row=r, column=c, value=round(rr["total_km"], 1)); c += 1
                ws.cell(row=r, column=c, value=round(rr["total_min"])); c += 1
                r += 1

            for idx, p in enumerate(far_points, start=1):
                c = 1
                ws.cell(row=r, column=c, value=f"장거리{idx}"); c += 1
                ws.cell(row=r, column=c, value=station["name"]); c += 1
                ws.cell(row=r, column=c, value="-"); c += 1
                ws.cell(row=r, column=c, value="원거리"); c += 1
                c += 2  # 담당 조 · 조원 빈칸
                ws.cell(row=r, column=c, value=station["name"]); c += 1
                ws.cell(row=r, column=c, value=stop_label(p["name"], p.get("address"))); c += 1
                ws.cell(row=r, column=c, value=p.get("도로거리_km", "")); c += 1
                r += 1

            for row_cells in ws.iter_rows(min_row=DATA0, max_row=r - 1, min_col=1, max_col=last_col):
                for cell in row_cells:
                    cell.border = border
                    cell.alignment = Alignment(vertical="center", wrap_text=True)

            for i in range(1, last_col + 1):
                width = 24 if (i > len(base_cols) and (i - len(base_cols) - 1) % 3 == 0) else 13
                ws.column_dimensions[get_column_letter(i)].width = width
            ws.freeze_panes = ws.cell(row=DATA0, column=len(base_cols) + 1)

            buf = io.BytesIO()
            wb.save(buf)
            return buf.getvalue()

        def build_center_integrated_excel(station, route_results, meta):
            from openpyxl import Workbook
            from openpyxl.drawing.image import Image as XLImage
            from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
            from openpyxl.utils import get_column_letter

            wb = Workbook()
            ws = wb.active
            ws.title = "지리조사 통합표"
            ws.sheet_view.showGridLines = False

            thin = Side(style="thin", color="D9D9D9")
            border = Border(left=thin, right=thin, top=thin, bottom=thin)
            title_fill = PatternFill("solid", fgColor="17375F")
            header_fill = PatternFill("solid", fgColor="D9EAF7")
            summary_fill = PatternFill("solid", fgColor="F7E9EA")
            center = Alignment(horizontal="center", vertical="center", wrap_text=True)
            left = Alignment(horizontal="left", vertical="center", wrap_text=True)
            right = Alignment(horizontal="right", vertical="center", wrap_text=True)

            columns = [
                "팀", "조", "노선", "순번", "대상명", "주소",
                "구간거리(km)", "누적거리(km)", "노선거리(km)", "총소요시간(분)",
                "카카오맵", "QR",
            ]
            last_col = len(columns)

            title = meta.get("title", "센터 지리조사 노선 결과")
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
            title_cell = ws.cell(row=1, column=1, value=f"{title} - 지도·팀별목록 통합 엑셀")
            title_cell.fill = title_fill
            title_cell.font = Font(color="FFFFFF", bold=True, size=14)
            title_cell.alignment = center

            basis = meta.get("hydrant_distribution_basis") or meta.get("basis") or ""
            ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
            ws.cell(
                row=2, column=1,
                value=(
                    f"출발·복귀: {station['name']} / 분배기준: {basis} / "
                    f"{meta.get('team_info', '')}"
                ),
            ).alignment = left

            summary_start = 4
            ws.cell(row=summary_start, column=1, value="팀별 요약표")
            ws.merge_cells(start_row=summary_start, start_column=1, end_row=summary_start, end_column=last_col)
            ws.cell(row=summary_start, column=1).fill = summary_fill
            ws.cell(row=summary_start, column=1).font = Font(bold=True, size=12)
            ws.cell(row=summary_start, column=1).alignment = center

            summary_headers = ["팀", "조/노선 수", "대상 수", "총 거리(km)", "총 소요시간(분)", "비고"]
            for col_no, header in enumerate(summary_headers, start=1):
                cell = ws.cell(row=summary_start + 1, column=col_no, value=header)
                cell.fill = header_fill
                cell.font = Font(bold=True)
                cell.border = border
                cell.alignment = center

            team_summary = {}
            team_route_order = {}
            for rr in route_results:
                team_no = rr.get("vehicle_no") or rr["route_no"]
                team_summary.setdefault(team_no, {"routes": 0, "stops": 0, "km": 0.0, "min": 0.0})
                team_summary[team_no]["routes"] += 1
                team_summary[team_no]["stops"] += len(rr["stops"])
                team_summary[team_no]["km"] += rr["total_km"]
                team_summary[team_no]["min"] += rr["total_min"]
                team_route_order[rr["route_no"]] = team_summary[team_no]["routes"]

            row = summary_start + 2
            for team_no in sorted(team_summary):
                info = team_summary[team_no]
                values = [
                    f"{team_no}팀",
                    info["routes"],
                    info["stops"],
                    round(info["km"], 1),
                    round(info["min"]),
                    "",
                ]
                for col_no, value in enumerate(values, start=1):
                    cell = ws.cell(row=row, column=col_no, value=value)
                    cell.border = border
                    cell.alignment = center if col_no != 6 else left
                row += 1

            list_start = row + 2
            ws.cell(row=list_start, column=1, value="팀별·조별 배정 목록")
            ws.merge_cells(start_row=list_start, start_column=1, end_row=list_start, end_column=last_col)
            ws.cell(row=list_start, column=1).fill = summary_fill
            ws.cell(row=list_start, column=1).font = Font(bold=True, size=12)
            ws.cell(row=list_start, column=1).alignment = center

            header_row = list_start + 1
            for col_no, header in enumerate(columns, start=1):
                cell = ws.cell(row=header_row, column=col_no, value=header)
                cell.fill = header_fill
                cell.font = Font(bold=True)
                cell.border = border
                cell.alignment = center

            row = header_row + 1
            for rr in route_results:
                team_no = rr.get("vehicle_no") or rr["route_no"]
                group_no = team_route_order.get(rr["route_no"], 1)
                route_links = kakao_route_links(station, rr["legs"])
                first_url = route_links[0][0] if route_links else ""
                acc_km = 0.0
                route_start_row = row

                for stop_no, leg in enumerate(rr["legs"], start=1):
                    acc_km += leg["km"]
                    values = [
                        f"{team_no}팀",
                        f"{group_no}조",
                        f"노선 {rr['route_no']}",
                        stop_no,
                        leg["to"],
                        leg.get("to_address", ""),
                        round(leg["km"], 1),
                        round(acc_km, 1),
                        round(rr["total_km"], 1) if stop_no == 1 else "",
                        round(rr["total_min"]) if stop_no == 1 else "",
                        "카카오맵 열기" if stop_no == 1 and first_url else "",
                        "",
                    ]
                    for col_no, value in enumerate(values, start=1):
                        cell = ws.cell(row=row, column=col_no, value=value)
                        cell.border = border
                        cell.alignment = left if col_no in (5, 6) else center
                    if stop_no == 1 and first_url:
                        link_cell = ws.cell(row=row, column=11)
                        link_cell.hyperlink = first_url
                        link_cell.style = "Hyperlink"
                    row += 1

                if first_url:
                    qr_img = XLImage(io.BytesIO(make_qr_png(first_url)))
                    qr_img.width = 92
                    qr_img.height = 92
                    ws.add_image(qr_img, f"L{route_start_row}")
                    ws.row_dimensions[route_start_row].height = 74

            widths = {
                "A": 10, "B": 8, "C": 11, "D": 8, "E": 28, "F": 48,
                "G": 13, "H": 13, "I": 13, "J": 15, "K": 18, "L": 15,
            }
            for column, width in widths.items():
                ws.column_dimensions[column].width = width

            for row_cells in ws.iter_rows(min_row=1, max_row=ws.max_row, min_col=1, max_col=last_col):
                for cell in row_cells:
                    if cell.row not in (1,) and cell.value is not None:
                        cell.border = border
                    if cell.column in (7, 8, 9, 10):
                        cell.alignment = right
            ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

            buf = io.BytesIO()
            wb.save(buf)
            return buf.getvalue()

        safe_title = re.sub(r'[\\/:*?"<>|]', "_", meta.get("title", "순찰노선")) or "순찰노선"

        center_print_bytes = build_center_route_print_html(station, route_results, meta)

        # 공문서 작업과 현장 전달에 필요한 4개 자료를 하나의 ZIP으로 묶는다.
        wide_excel_bytes = build_wide_excel(station, route_results, far_points, meta)
        center_integrated_excel_bytes = (
            build_center_integrated_excel(station, route_results, meta) if is_center_route else None
        )
        route_links_excel_bytes = build_route_links_excel(station, route_results)
        qr_zip_bytes = build_qr_zip(station, route_results)
        printable_qr_html_bytes = build_printable_qr_html(station, route_results, meta)

        center_materials = io.BytesIO()
        if is_center_route:
            with zipfile.ZipFile(center_materials, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(f"1_{safe_title}_노선지도.html", center_print_bytes)
                zf.writestr(f"2_{safe_title}_팀별조별_통합표_QR포함.xlsx", center_integrated_excel_bytes)

        all_materials = io.BytesIO()
        with zipfile.ZipFile(all_materials, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"1_{safe_title}_최종순찰표.xlsx", wide_excel_bytes)
            zf.writestr(f"2_{safe_title}_카카오맵_경로링크.xlsx", route_links_excel_bytes)
            zf.writestr(f"3_{safe_title}_QR코드_전체.zip", qr_zip_bytes)
            zf.writestr(f"4_{safe_title}_QR인쇄문서.html", printable_qr_html_bytes)

        with action_right:
            with st.expander("📦 최종 결과자료 받기", expanded=False):
                st.markdown("### 공문서·현장 전달 자료")
                st.caption("모든 자료는 한 번에 내려받고, 현장에서는 카카오 경로 링크를 바로 여세요.")

                if is_center_route:
                    st.download_button(
                        "지도·팀별목록 한꺼번에 다운로드", data=center_materials.getvalue(),
                        file_name=f"{safe_title}_지도_팀별목록.zip", mime="application/zip",
                        use_container_width=True,
                    )
                else:
                    st.download_button(
                        "📦 모든 자료 한 번에 내려받기", data=all_materials.getvalue(),
                        file_name=f"{safe_title}_모든자료.zip", mime="application/zip",
                        use_container_width=True,
                    )

                link_box = (st.popover("🔗 카카오 경로 링크 열기", use_container_width=True)
                            if hasattr(st, "popover") else st.expander("🔗 카카오 경로 링크 열기"))
                with link_box:
                    for rr in route_results:
                        route_links = kakao_route_links(station, rr["legs"])
                        for link_no, (kurl, _origin, _destinations) in enumerate(route_links, start=1):
                            suffix = "" if len(route_links) == 1 else f" · {link_no}/{len(route_links)}구간"
                            st.link_button(f"🚗 노선 {rr['route_no']}{suffix} 열기", kurl,
                                           use_container_width=True)

                if is_center_route:
                    st.caption("문서를 열어 인쇄하면 노선별로 A4 한 장씩 출력됩니다.")
                else:
                    st.caption("순찰표·경로링크·QR코드·QR 인쇄문서가 들어 있습니다.")

        target_min_ref = meta.get("target_min")
        if target_min_ref:
            over = [r for r in route_results if r["total_min"] > target_min_ref]
            if over:
                over_routes = ", ".join(str(r["route_no"]) for r in over)
                st.markdown(
                    f'''<div style="margin:0.75rem 0 1rem;padding:1rem 1.15rem;
                        border:2px solid #e58a14;border-left:7px solid #d97706;border-radius:10px;
                        background:#fff3df;color:#7c3f00;font-weight:650;line-height:1.6;">
                        <strong style="color:#a94f00;font-size:1.02rem;">⚠️ {target_min_ref:g}분 초과 노선 안내</strong><br>
                        목표시간을 넘는 노선이 {len(over)}개 있습니다. (노선 {over_routes})<br>
                        노선당 방문지 수를 줄이거나 목표시간을 늘려 다시 편성해 주세요.
                    </div>''',
                    unsafe_allow_html=True,
                )
            else:
                st.success(f"⏱ 모든 노선이 목표 {target_min_ref}분 이내입니다.")

        st.markdown("### 🗺️ 노선별 지도 · 상세보기")
        st.caption(
            f"생성된 {len(route_results)}개 노선을 모두 지도로 보여드립니다. "
            "각 지도 아래의 '상세보기'를 열면 방문순서·카카오맵 길안내·QR코드가 나옵니다."
        )

        ROUTE_CARDS_PER_ROW = 2
        for row_start in range(0, len(route_results), ROUTE_CARDS_PER_ROW):
            row_routes = route_results[row_start:row_start + ROUTE_CARDS_PER_ROW]
            route_cols = st.columns(len(row_routes))
            for route_col, rr in zip(route_cols, row_routes):
                with route_col:
                    team_name = st.session_state.get(f"team_name_{rr['route_no']}", "")
                    over_mark = " ⏱초과" if target_min_ref and rr["total_min"] > target_min_ref else ""
                    hydrant_label = ""
                    if meta.get("purpose") == "⑤ 지리조사(센터용)" and rr.get("vehicle_no"):
                        members = ", ".join(rr.get("assigned_members") or [])
                        hydrant_label = f" · {rr['vehicle_no']}호차" + (f" · {members}" if members else "")
                    season_runs = (f" · 기간 중 {rr.get('period_runs', 0)}회 예상"
                                   if meta.get("purpose") == "③ 계절순찰" and rr.get("period_runs") else "")
                    inspect_schedule = ""
                    if meta.get("purpose") == "④ 예방검사" and rr.get("inspection_date"):
                        inspect_schedule = (
                            f" · {rr['inspection_date']:%Y-%m-%d} · {rr.get('inspection_team', 1)}팀"
                        )
                    head = (f"노선 {rr['route_no']}" + inspect_schedule + hydrant_label
                            + (f" · {team_name}" if team_name else ""))
                    st.markdown(f"#### 🚒 {head}{over_mark}")
                    st.caption(
                        f"{len(rr['stops'])}개소 · 총 {rr['total_km']:.1f}km · 약 {rr['total_min']:.0f}분"
                        + season_runs
                    )

                    with st.container(border=True):
                        m = folium.Map(location=[station["lat"], station["lng"]], zoom_start=12)
                        if rr["path"]:
                            folium.PolyLine(rr["path"], color="#a33a3f", weight=4, opacity=0.85).add_to(m)

                        # 방문 순서를 지도 위에 숫자로 표시 (기본 핀 대신 번호 원)
                        for i, leg in enumerate(rr["legs"], start=1):
                            folium.Marker(
                                [leg["lat"], leg["lng"]],
                                tooltip=f"{i}. {leg['to']}",
                                icon=folium.DivIcon(
                                    icon_size=(30, 30), icon_anchor=(15, 15),
                                    html=(
                                        '<div style="background:#1f6fb2;color:#ffffff;'
                                        'width:26px;height:26px;border-radius:50%;'
                                        'border:2px solid #ffffff;box-shadow:0 1px 5px rgba(0,0,0,.45);'
                                        'display:flex;align-items:center;justify-content:center;'
                                        'font-family:sans-serif;font-weight:700;font-size:13px;'
                                        f'line-height:1;">{i}</div>'
                                    ),
                                ),
                            ).add_to(m)

                        # 출발·복귀 지점은 눈에 띄게 빨간 '출발' 표식으로
                        folium.Marker(
                            [station["lat"], station["lng"]], tooltip=f"출발·복귀: {station['name']}",
                            icon=folium.DivIcon(
                                icon_size=(56, 26), icon_anchor=(28, 13),
                                html=(
                                    '<div style="background:#a33a3f;color:#ffffff;'
                                    'padding:3px 8px;border-radius:13px;border:2px solid #ffffff;'
                                    'box-shadow:0 1px 5px rgba(0,0,0,.45);text-align:center;'
                                    'font-family:sans-serif;font-weight:700;font-size:12px;'
                                    'line-height:1.2;white-space:nowrap;">🚒 출발</div>'
                                ),
                            ),
                        ).add_to(m)
                        st_folium(m, height=260, use_container_width=True, key=f"map_{rr['route_no']}")

                        with st.expander("🔎 상세보기 · 방문순서 · 카카오맵 · QR코드", expanded=False):
                            rows = []
                            for i, leg in enumerate(rr["legs"], start=1):
                                row = {
                                    "순번": str(i), "지점": stop_label(leg["to"], leg.get("to_address")),
                                    "구간거리(km)": round(leg["km"], 1), "구간시간(분)": round(leg["min"]),
                                }
                                if meta.get("purpose") == "⑤ 지리조사(센터용)":
                                    row["담당"] = leg.get("assigned_to", "")
                                    row["조사시간(분)"] = leg.get("inspection_min", 0)
                                rows.append(row)
                            rows.append({"순번": "", "지점": f"복귀 ({station['name']})",
                                         "구간거리(km)": round(rr["back_km"], 1),
                                         "구간시간(분)": round(rr["back_min"])})
                            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                            st.markdown("**🟨 카카오맵 — 전체 순찰코스와 QR코드**")
                            kakao_links = kakao_route_links(station, rr["legs"])
                            st.download_button(
                                f"🖨 노선 {rr['route_no']} QR 인쇄용 문서",
                                data=build_printable_qr_html(station, [rr], meta),
                                file_name=f"{safe_title}_노선_{rr['route_no']}_QR인쇄.html",
                                mime="text/html",
                                key=f"route_print_{rr['route_no']}",
                                use_container_width=True,
                            )
                            for li, (kurl, origin, destinations) in enumerate(kakao_links, start=1):
                                suffix = "" if len(kakao_links) == 1 else f" ({li}/{len(kakao_links)}구간)"
                                seq = " → ".join([origin["name"]] + [p["name"] for p in destinations])
                                qr_png = make_qr_png(kurl)

                                st.link_button(
                                    f"🚗 카카오맵 전체 코스 길안내{suffix}",
                                    kurl,
                                    use_container_width=True,
                                )
                                qr_box = (st.popover(f"📱 QR코드 보기{suffix}", use_container_width=True)
                                          if hasattr(st, "popover")
                                          else st.expander(f"📱 QR코드 보기{suffix}"))
                                with qr_box:
                                    st.image(qr_png, caption="휴대폰 카메라로 스캔하세요.", width=220)
                                    st.download_button(
                                        "QR코드 이미지 저장",
                                        data=qr_png,
                                        file_name=f"노선_{rr['route_no']}_카카오맵_QR_{li}.png",
                                        mime="image/png",
                                        key=f"kakao_qr_{rr['route_no']}_{li}",
                                        use_container_width=True,
                                    )
                                st.caption(f"경로: {seq}")

                            if len(kakao_links) > 1:
                                st.caption(
                                    f"※ 카카오맵은 경유지를 한 구간에 최대 {KAKAO_MAX_VIA}개까지 지원하므로 "
                                    "긴 노선은 이어지는 구간으로 나눴습니다. 현장에서 순서대로 열어 주세요."
                                )
                            else:
                                st.caption(
                                    "※ 소방서를 출발지와 최종 목적지로, 순찰 대상을 경유지 순서대로 "
                                    "입력한 카카오맵 링크입니다. QR을 스캔하면 같은 코스가 열립니다."
                                )

                            with st.expander("지점별 개별 길안내(카카오맵) — 예비용", expanded=False):
                                for i, leg in enumerate(rr["legs"], start=1):
                                    st.markdown(
                                        f"- [{i}. {leg['to']} 길안내]({kakao_url(leg['to'], leg['lat'], leg['lng'])})"
                                    )
                                st.markdown(
                                    f"- [🚒 {station['name']} 귀소 길안내]"
                                    f"({kakao_url(station['name'], station['lat'], station['lng'])})"
                                )
                                st.caption(
                                    "전체 코스가 특정 휴대폰에서 열리지 않을 때 사용하세요. 각 버튼은 "
                                    "현재 위치에서 선택한 다음 지점까지 안내합니다."
                                )

        if far_points:
            is_season_far = meta.get("purpose") == "③ 계절순찰"
            st.header("🚙 편도 기준 초과 대상" if is_season_far else "⚠️ 장거리 별도 대상")
            far_df = pd.DataFrame(far_points)
            far_columns = ["name", "address", "도로거리_km"]
            if is_season_far:
                far_columns += ["편도시간_분", "권장수행"]
                st.info("선택한 편도 시간 또는 거리 제한값을 넘는 대상입니다. "
                        "수행자·차량·제한 기준을 조정하거나 별도 순찰 대상으로 검토하세요.")
            st.dataframe(far_df[[column for column in far_columns if column in far_df.columns]],
                         use_container_width=True, hide_index=True)
            for p in far_points:
                st.markdown(
                    f"- [{p['name']} 길안내]({kakao_url(p['name'], p['lat'], p['lng'])})"
                    f" · 실도로거리 {p['도로거리_km']}km"
                    + (f" · 편도 약 {p.get('편도시간_분', '-')}분 · {p.get('권장수행', '')}"
                       if is_season_far else "")
                )

        st.divider()
        st.caption(
            "⚠️ 이 페이지의 API 키는 서버(Secrets)에만 저장되며 브라우저로 노출되지 않습니다. "
            "실제 도로거리·소요시간은 NCP Geocoding·Directions5 실시간 계산 결과입니다. "
            "📲 휴대폰에서는 브라우저 메뉴의 '홈 화면에 추가'를 누르면 앱처럼 사용할 수 있습니다."
        )

    st.info(
        "📌 본 앱이 생성한 순찰노선은 업무 지원을 위한 참고자료입니다. "
        "현장 여건, 도로 상황, 출동 공백 및 대상별 특성을 담당자가 충분히 검토한 후 사용하시기 바랍니다. "
        "최종 노선의 검토·결정과 운용 책임은 프로그램 사용자 및 해당 부서에 있습니다."
    )

    st.markdown(
        '<div style="margin-top:18px;padding:14px 8px 4px;text-align:center;'
        'border-top:1px solid #d9dee4;color:#5f6b64;font-size:0.86rem;">'
        '파세루 오리진&nbsp; | &nbsp;기획·개발 임미성&nbsp; | &nbsp;문의: '
        '<a href="mailto:emtmisung@gmail.com" style="color:#1f6fb2;'
        'text-decoration:none;">emtmisung@gmail.com</a></div>',
        unsafe_allow_html=True,
    )
