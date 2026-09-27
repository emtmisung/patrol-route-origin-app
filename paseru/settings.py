"""기본 설정 · 비밀값 조회 · 공통 상수."""
import tempfile
from pathlib import Path

import streamlit as st


# 저장소 최상위 폴더(app.py가 있는 곳)
ROOT_DIR = Path(__file__).resolve().parent.parent

GEOCODE_URL = "https://maps.apigw.ntruss.com/map-geocode/v2/geocode"
DIRECTIONS_URL = "https://maps.apigw.ntruss.com/map-direction/v1/driving"
KAKAO_PLACE_SEARCH_URL = "https://search.map.kakao.com/mapsearch/map.daum"
AUTH_REMEMBER_KEY = "paseru_auth_remember_v1"
AUTH_REMEMBER_DAYS = 30

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


def _secret(name, default=""):
    """Streamlit Secrets 값을 호출할 때마다 읽는다(비밀값을 바꿔도 앱 재시작 없이 반영)."""
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def ncp_key_id():
    return _secret("NCP_CLIENT_ID", "")


def ncp_key():
    return _secret("NCP_CLIENT_SECRET", "")


def app_password():
    return _secret("APP_PASSWORD", "")


def mobile_transfer_secret():
    return str(_secret("MOBILE_TRANSFER_SECRET", ncp_key() or app_password()))


def app_public_url():
    return str(_secret("APP_PUBLIC_URL", "https://faseru-origin.streamlit.app/")).rstrip("/")
