import io
import math
import re
import zipfile
from datetime import datetime, date, time as dtime
from urllib.parse import quote

import folium
import pandas as pd
import qrcode
import requests
import streamlit as st
import streamlit.components.v1 as components
from streamlit_folium import st_folium

# ----------------------------------------------------------------------------
# 기본 설정
# ----------------------------------------------------------------------------
st.set_page_config(page_title="파세루 오리진 (FireSafe Route Origin)", page_icon="🚒", layout="wide")

GEOCODE_URL = "https://maps.apigw.ntruss.com/map-geocode/v2/geocode"
DIRECTIONS_URL = "https://maps.apigw.ntruss.com/map-direction/v1/driving"
NCP_KEY_ID = st.secrets.get("NCP_CLIENT_ID", "")
NCP_KEY = st.secrets.get("NCP_CLIENT_SECRET", "")

AVG_SPEED_KMH = 35.0      # NCP 호출 실패 시에만 쓰는 비상 대체값(직선거리 보정)
ROAD_FACTOR = 1.3         # NCP 호출 실패 시에만 쓰는 비상 대체 보정계수

SAMPLE_CSV = "seongju_patrol_coordinates_updated_modified.csv"


def ncp_headers():
    return {
        "x-ncp-apigw-api-key-id": NCP_KEY_ID,
        "x-ncp-apigw-api-key": NCP_KEY,
        "Accept": "application/json",
    }


def has_keys():
    return bool(NCP_KEY_ID) and bool(NCP_KEY)


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


def geocode_with_fallback(address: str, name: str = "", on_call=None):
    """여러 주소 형태로 순차 시도. 반환: (lat, lng, 성공에 쓴 주소, 방법, 시도내역)"""
    tried = []
    for query, why in address_variants(address, name):
        lat, lng, status = geocode_once(query)
        if on_call:
            on_call()
        tried.append(f"{why}: {query} → {status}")
        if status == "ok":
            return lat, lng, query, why, tried
    return None, None, None, None, tried


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
                 candidate_k=5, should_stop=None):
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
                projected = acc_min + leg_min + back_min
                if not first_stop and projected > target_min_high:
                    break
                if len(route) >= max_per_route:
                    break

            route.append(nxt)
            acc_min += leg_min
            cur = nxt
            remaining.remove(nxt)

        if not route:
            # 어떤 조건도 만족 못하는 경우(예: 첫 지점부터 원거리) -> 강제 배정 방지, 장거리로 이관
            break
        routes.append(route)

        if max_routes_cap and len(routes) >= max_routes_cap and remaining:
            # 노선 수 상한 도달 -> 남은 지점은 마지막 노선에 최대한 이어붙임(완화)
            for p in remaining[:]:
                route.append(p)
                remaining.remove(p)
            break

    return routes, remaining


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


# ----------------------------------------------------------------------------
# UI — 파세루 데모(웹 프로토타입)와 같은 카드+칩 스타일
# ----------------------------------------------------------------------------
PASERU_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700;800&family=Noto+Serif+KR:wght@600;700&family=IBM+Plex+Mono:wght@500;700&display=swap');

:root{
  --accent:#dc2626; --accent-hover:#b91c1c; --accent-soft:#fee2e2;
  --line:#d9dee4; --surface:#ffffff; --bg:#f3f5f7; --ink:#141a17; --muted:#414a44;
  color-scheme: light;   /* 휴대폰 다크모드에서도 밝은 화면으로 고정 */
}
.stApp{ background: var(--bg); color: var(--ink); }

/* 휴대폰 다크모드에서 '흰 배경 + 흰 글씨'가 되는 문제를 막기 위해
   본문 글자색을 어두운 색으로 명시적으로 고정한다. */
.stApp, .stApp p, .stApp span, .stApp label, .stApp li, .stApp div,
.stMarkdown, .stMarkdown *, [data-testid="stWidgetLabel"] *,
[data-testid="stMetricLabel"] *, [data-testid="stMetricValue"],
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *{
  color: var(--ink);
}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *,
[data-testid="stMetricLabel"] *{ color: var(--muted) !important; }

/* 알림 박스(노란색·파란색 등) 안 글씨도 항상 검정 계열로 */
div[data-testid="stAlert"], div[data-testid="stAlert"] *,
div[data-testid="stNotification"], div[data-testid="stNotification"] *{
  color:#1c2420 !important;
}

/* 입력창·표를 밝은 배경 + 어두운 글씨로 고정 */
input, textarea, select,
[data-baseweb="input"] input, [data-baseweb="base-input"] input,
[data-baseweb="select"] div{
  background-color:#ffffff !important; color:#1c2420 !important;
}
[data-testid="stDataFrame"], [data-testid="stDataEditor"],
[data-testid="stTable"]{ background:#ffffff !important; }

html, body, [class*="css"], .stMarkdown, .stTextInput, .stNumberInput{
  font-family:'Noto Sans KR', -apple-system, 'Malgun Gothic', sans-serif;
}
h1, h2, h3, h4, h5, h6{
  font-family:'Noto Serif KR', serif !important; color: var(--ink) !important; font-weight:700 !important;
}
.block-container{ padding-top: 2.2rem; max-width: 1180px; }

/* ---- 카드 컨테이너(border=True) ---- */
div[data-testid="stVerticalBlockBorderWrapper"]{
  background: var(--surface);
  border-radius: 14px !important;
  border: 1px solid var(--line) !important;
  box-shadow: 0 1px 2px rgba(20,26,30,.05), 0 10px 26px -14px rgba(20,26,30,.18);
  padding: 10px 16px 14px;
  margin-bottom: 6px;
}

/* ---- 카드 제목 + 번호 뱃지 ---- */
.paseru-card-title{
  display:flex; align-items:center; gap:9px;
  font-family:'Noto Serif KR', serif; font-size:17px; font-weight:700; color:var(--ink);
  margin: 2px 0 10px;
}
.paseru-step{
  display:inline-flex; align-items:center; justify-content:center;
  width:23px; height:23px; border-radius:50%;
  background:var(--accent); color:#fff;
  font-family:'IBM Plex Mono', monospace; font-size:12px; font-weight:700; flex:none;
}
.paseru-sub{ font-weight:700; font-size:13.5px; color:var(--ink); margin:14px 0 6px; }
.paseru-eyebrow{
  font-family:'IBM Plex Mono', monospace; font-size:12px; letter-spacing:.08em;
  text-transform:uppercase; color:var(--accent); font-weight:700; margin-bottom:2px;
}

/* ---- pills(칩) : 선택 시 브랜드 레드로 채움 ---- */
button[data-variant="pills"]{
  border-radius: 999px !important;
  border: 1px solid var(--line) !important;
  background: #e9ede6 !important;
  color: var(--ink) !important;
  font-weight: 600 !important;
  padding: 0.42em 1.05em !important;
  transition: background .12s, border-color .12s, color .12s;
}
button[data-variant="pills"]:hover{ border-color: var(--accent) !important; }
button[data-variant="pills"][data-selected="true"],
button[data-variant="pills"][aria-checked="true"],
button[data-variant="pills"][aria-pressed="true"]{
  background: var(--accent) !important;
  border-color: var(--accent) !important;
  color: #ffffff !important;
  font-weight: 700 !important;
  box-shadow: 0 4px 12px -6px rgba(220,38,38,.55);
}
button[data-variant="pills"][data-selected="true"] *,
button[data-variant="pills"][aria-checked="true"] *,
button[data-variant="pills"][aria-pressed="true"] *{ color:#ffffff !important; }
div[data-testid="stButtonGroup"]{ gap: 8px !important; }

/* ---- 버튼 ---- */
div.stButton > button, .stDownloadButton > button, div.stFormSubmitter > button{
  background-color: var(--accent) !important;
  color:#fff !important; border:none !important; border-radius:10px !important;
  font-weight:700 !important; padding:0.65em 1.1em !important;
  box-shadow: 0 6px 16px -8px rgba(220,38,38,.45);
}
div.stButton > button:hover, .stDownloadButton > button:hover{ background-color: var(--accent-hover) !important; }
div.stButton > button[kind="secondary"]{
  background:#e9ede6 !important; color:var(--ink) !important; box-shadow:none !important;
}

/* ---- 결과 영역 ---- */
div[data-testid="stExpander"]{
  border:1px solid var(--line) !important; border-radius:12px !important;
  background: var(--surface); overflow:hidden;
}

/* ---- 대비 강화: 지표·표·라벨이 흐리게 보이지 않도록 ---- */
div[data-testid="stMetricValue"]{
  font-family:'IBM Plex Mono', monospace;
  color: var(--accent) !important; font-weight:700 !important;
}
div[data-testid="stMetricLabel"], div[data-testid="stMetricLabel"] *{
  color:#2b332e !important; font-weight:600 !important;
}
[data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label{
  color:#1c2420 !important; font-weight:600 !important;
}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *{
  color:#414a44 !important;
}
/* 표(데이터프레임·편집표) 글씨와 테두리를 진하게 */
[data-testid="stDataFrame"] *, [data-testid="stDataEditor"] *{
  color:#141a17 !important;
}
[data-testid="stDataFrame"], [data-testid="stDataEditor"]{
  border:1px solid var(--line) !important; border-radius:8px;
}
[data-testid="stDataFrame"] [role="columnheader"],
[data-testid="stDataEditor"] [role="columnheader"]{
  background:#e8ecf1 !important; font-weight:700 !important;
}
/* 알림 박스에 색 띠를 넣어 눈에 잘 띄게 */
div[data-testid="stAlert"]{
  border-left:5px solid var(--accent) !important; border-radius:8px !important;
}
.paseru-sub{ color:#141a17 !important; }

/* ---- 내비게이션 버튼: 링크 기본색(파랑)에 밀리지 않도록 클래스로 고정 ---- */
a.paseru-navbtn, a.paseru-navbtn:link, a.paseru-navbtn:visited,
a.paseru-navbtn:hover, a.paseru-navbtn:active,
a.paseru-navbtn *{
  color:#ffffff !important;
  text-decoration:none !important;
}
a.paseru-navbtn{
  display:inline-block; padding:12px 16px; border-radius:10px;
  font-weight:700 !important; font-size:14px; margin:4px 8px 4px 0;
  box-shadow:0 3px 10px -4px rgba(0,0,0,.35);
}
a.paseru-navbtn.nav-and{ background:#03C75A !important; }   /* 안드로이드 (네이버 초록) */
a.paseru-navbtn.nav-ios{ background:#0a8f45 !important; }   /* 아이폰 */
a.paseru-navbtn.nav-pc{  background:#2563eb !important; }   /* PC 웹 (밝은 파랑) */
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


NAVER_MAX_VIA = 5   # 네이버지도 URL Scheme이 지원하는 경유지 최대 개수


def naver_route_url(station, stops, app_name="faseru-origin.streamlit.app"):
    """네이버지도 자동차 길찾기 링크 — 출발(센터) → 경유지들 → 도착(센터).

    경유지는 최대 5개까지 지원하므로, 그보다 많으면 호출부에서 나눠서 만든다.
    """
    q = lambda s: quote(str(s), safe="")
    parts = [
        f"slat={station['lat']}", f"slng={station['lng']}", f"sname={q(station['name'])}",
    ]
    for i, s in enumerate(stops[:NAVER_MAX_VIA], start=1):
        parts += [f"v{i}lat={s['lat']}", f"v{i}lng={s['lng']}", f"v{i}name={q(s['name'])}"]
    # 마지막은 다시 센터로 복귀
    parts += [f"dlat={station['lat']}", f"dlng={station['lng']}", f"dname={q(station['name'])}"]
    parts.append(f"appname={app_name}")
    return "nmap://route/car?" + "&".join(parts)


def naver_intent_url(nmap_url):
    """안드로이드 크롬은 nmap:// 같은 커스텀 스킴을 막기 때문에 intent:// 형식으로 바꿔준다.
    (앱이 없으면 플레이스토어로 이동)"""
    body = nmap_url[len("nmap://"):]
    return f"intent://{body}#Intent;scheme=nmap;package=com.nhn.android.nmap;end"


def naver_web_route_url(station, stops):
    """네이버지도 웹 길찾기 — 앱이 없거나 PC에서 경로를 눈으로 확인할 때 쓰는 대체 수단.
    (웹은 경유지 지정이 제한적이라 출발지 → 마지막 지점 기준으로 열린다)"""
    last = stops[-1]
    q = lambda s: quote(str(s), safe="")
    return ("https://map.naver.com/p/directions/"
            f"{station['lng']},{station['lat']},{q(station['name'])},,/"
            f"{last['lng']},{last['lat']},{q(last['name'])},,/-/car")


def naver_route_links(station, legs):
    """노선 전체를 경유지 포함 링크로 만든다. 경유지가 5개를 넘으면 구간을 나눠 여러 개로.
    반환: [(앱링크, 웹대체링크, 구간지점들), ...]"""
    stops = [{"name": lg["to"], "lat": lg["lat"], "lng": lg["lng"]} for lg in legs]
    if not stops:
        return []
    chunks = [stops[i:i + NAVER_MAX_VIA] for i in range(0, len(stops), NAVER_MAX_VIA)]
    return [(naver_route_url(station, c), naver_web_route_url(station, c), c) for c in chunks]


def card_title(step, text):
    st.markdown(
        f'<div class="paseru-card-title"><span class="paseru-step">{step}</span>{text}</div>',
        unsafe_allow_html=True,
    )


def sub_label(text):
    st.markdown(f'<div class="paseru-sub">{text}</div>', unsafe_allow_html=True)


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
      background_color: "#f3f5f7", theme_color: "#dc2626",
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
    theme.name = 'theme-color'; theme.content = '#dc2626';
    d.head.appendChild(theme);
  }
} catch (e) { /* 환경상 주입이 막히면 조용히 무시 */ }
</script>
""",
    height=0,
)

st.markdown('<div class="paseru-eyebrow">성주소방서 · 119재난대응과 · 실동 버전</div>', unsafe_allow_html=True)
st.title("🚒 파세루 오리진 (FireSafe Route Origin)")
st.caption("주소 목록과 순찰 조건만 넣으면, 실도로 기준(NCP Geocoding · Directions5 실연동)으로 노선을 자동 편성합니다.")

if not has_keys():
    st.error(
        "NCP(네이버클라우드플랫폼) Client ID/Secret이 설정되지 않았습니다. "
        "`.streamlit/secrets.toml` 또는 Streamlit Cloud의 Secrets 설정에 "
        "NCP_CLIENT_ID / NCP_CLIENT_SECRET 값을 등록해주세요."
    )

# ----------------------------------------------------------------------------
# 0 · 순찰 제목 / 출발·복귀 기준점
# ----------------------------------------------------------------------------
with st.container(border=True):
    card_title(0, "순찰 제목 · 출발·복귀 기준점")
    patrol_title = st.text_input("순찰 제목", value="추석 특별경계근무 순찰노선 - 성주군 일원")
    c1, c2, c3 = st.columns([1, 1.6, 1])
    with c1:
        station_name = st.text_input("출발 부서(소방서·센터) 이름", value="성주소방서")
    with c2:
        station_address = st.text_input("출발 부서 주소", value="경상북도 성주군 성주읍 주산로 193")
    with c3:
        route_prefix = st.text_input("노선 이름 접두어", value="성주",
                                     help="엑셀의 '노선이름' 열에 쓰입니다. 예) 성주 → 성주노선1")

st.write("")

# ----------------------------------------------------------------------------
# 1 · 순찰 방법(노선 용도)
# ----------------------------------------------------------------------------
PURPOSE_OPTIONS = [
    "① 특별경계근무용", "② 계절순찰", "③ 예방검사", "④ 지리조사(센터용)", "⑤ 기타",
]
PURPOSE_HINT = {
    "① 특별경계근무용": "명절 등 경계근무 순찰 — 같은 코스를 반복하거나 날짜별로 순환합니다.",
    "② 계절순찰": "산불·폭염·풍수해 등 계절별 순찰.",
    "③ 예방검사": "숙박업소 등 점검 순찰.",
    "④ 지리조사(센터용)": "소화전 등 팀별 순회 — 팀 수·목표시간 기준으로 노선수를 자동 산출합니다.",
    "⑤ 기타": "지휘관 방문 등 1회성 — 노선당 30분 이내, 팀 수만큼 전 대상을 1회씩 배분합니다.",
}

with st.container(border=True):
    card_title(1, "순찰 방법(노선 용도)")
    purpose_label = st.pills("노선 용도", PURPOSE_OPTIONS, default=PURPOSE_OPTIONS[0],
                             label_visibility="collapsed")
    if not purpose_label:
        purpose_label = PURPOSE_OPTIONS[0]
    st.caption(PURPOSE_HINT.get(purpose_label, ""))
    purpose = {
        "① 특별경계근무용": "guard", "② 계절순찰": "season", "③ 예방검사": "inspect",
        "④ 지리조사(센터용)": "hydrant", "⑤ 기타": "other",
    }.get(purpose_label, "guard")

    guard_repeat_label = None
    guard_rounds = None
    hydrant_teams = None
    hydrant_target_min = 60
    other_teams = None

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
    elif purpose == "hydrant":
        hc1, hc2 = st.columns(2)
        with hc1:
            hydrant_teams = st.number_input("순찰팀 수", min_value=1, value=3)
        with hc2:
            hydrant_target_min = st.number_input("팀당 목표 소요시간(분)", min_value=10, value=60)
    elif purpose == "other":
        other_teams = st.number_input("순찰팀 수", min_value=1, value=2)

st.write("")

# ----------------------------------------------------------------------------
# 2 · 순찰 기간 · 차량
# ----------------------------------------------------------------------------
if "period_start" not in st.session_state:
    st.session_state["period_start"] = date(2026, 9, 23)
    st.session_state["period_start_time"] = dtime(18, 0)
    st.session_state["period_end"] = date(2026, 9, 28)
    st.session_state["period_end_time"] = dtime(9, 0)

with st.container(border=True):
    card_title(2, "순찰 기간 · 순찰 차량")

    if st.button("🎑 추석 특별경계근무 자동입력 (9.23 18:00 ~ 9.28 09:00, 5일)", type="secondary"):
        st.session_state["period_start"] = date(2026, 9, 23)
        st.session_state["period_start_time"] = dtime(18, 0)
        st.session_state["period_end"] = date(2026, 9, 28)
        st.session_state["period_end_time"] = dtime(9, 0)
        st.rerun()

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

# ----------------------------------------------------------------------------
# 3 · 노선 조건 설정
# ----------------------------------------------------------------------------
with st.container(border=True):
    card_title(3, "노선 조건 설정")

    time_based = purpose in ("hydrant", "other")

    if time_based:
        mode = "target_time"
        target_min = 30 if purpose == "other" else hydrant_target_min
        target_min_low = target_min - 10
        target_min_high = target_min + 10
        seg_max_km = seg_max_min = None
        max_per_route = 25
        max_routes_cap = 0
        st.caption(f"목표 {target_min}분/노선 기준으로 자동 편성합니다 (노선 내 대상 수 제한 없음).")
    else:
        sub_label("가. 기준 방식")
        mode_label = st.pills("기준 방식",
                              ["노선 수·구간 수 지정", "구간별 제한", "노선 전체 목표시간"],
                              default="노선 수·구간 수 지정", label_visibility="collapsed")
        if not mode_label:
            mode_label = "노선 수·구간 수 지정"
        mode = {"노선 수·구간 수 지정": "fixed", "구간별 제한": "segment",
                "노선 전체 목표시간": "target_time"}[mode_label]

        cc1, cc2 = st.columns(2)
        with cc1:
            max_per_route = st.number_input("노선 내 구간 수(방문지 수)", min_value=1, max_value=30, value=5)
        with cc2:
            max_routes_cap = st.number_input("총 노선 수 상한(0 = 전수 자동배분)", min_value=0, value=6)

        if mode == "fixed":
            st.caption(f"노선당 {max_per_route}개소씩 최대 {max_routes_cap or '제한 없이'}개 노선으로 나눕니다. "
                       "거리·시간 제한 없이 개수대로 나눈 뒤, 아래 목표시간을 넘는 노선은 표시해 드립니다.")
            target_min = st.number_input("참고용 목표 왕복시간(분) — 초과 노선을 표시만 합니다",
                                         min_value=10, value=30)
            target_min_low = target_min_high = None
            seg_max_km = seg_max_min = None
        elif mode == "segment":
            sc1, sc2 = st.columns(2)
            with sc1:
                seg_max_km = st.number_input("구간당 최대 거리(km)", min_value=1.0, value=7.0, step=0.5)
            with sc2:
                seg_max_min = st.number_input("구간당 최대 시간(분)", min_value=1, value=10)
            target_min = target_min_low = target_min_high = None
        else:
            sub_label("나. 순찰 소요시간(왕복 목표시간)")
            quick_min = st.pills("목표 시간", ["30분", "1시간", "2시간", "직접입력"],
                                 default="1시간", label_visibility="collapsed")
            if quick_min == "30분":
                target_min = 30
            elif quick_min == "2시간":
                target_min = 120
            elif quick_min == "직접입력":
                target_min = st.number_input("목표 왕복시간(분)", min_value=10, value=90)
            else:
                target_min = 60
            allow_range = st.slider("허용 범위(분, ±)", 0, 60, 15)
            target_min_low = target_min - allow_range
            target_min_high = target_min + allow_range
            seg_max_km = seg_max_min = None

    sub_label("다. 노선 생성 기준")
    basis_label = st.pills("노선 생성 기준", ["거리 기준", "소요시간 기준"],
                           default="거리 기준", label_visibility="collapsed")
    if not basis_label:
        basis_label = "거리 기준"
    basis = "time" if basis_label == "소요시간 기준" else "distance"
    st.caption("거리 기준: 이동 거리(km)가 가장 짧은 순서로 연결 / 소요시간 기준: 이동 시간(분)이 가장 짧은 순서로 연결")

    sub_label("라. 장거리 분리 기준")
    long_threshold = st.number_input("소방서 실제 도로거리(km) 초과 시 별도 표시", min_value=1.0, value=15.0)

    sub_label("마. API 호출 절약 (요금·시간 절감)")
    save_calls = st.checkbox(
        "직선거리로 후보를 먼저 좁힌 뒤, 가까운 후보만 실제 도로거리로 확인 (권장)", value=True,
        help="끄면 매 단계마다 남은 모든 대상을 실제 도로거리로 확인합니다. "
             "정확도는 거의 같지만 호출 횟수가 대상 수의 제곱으로 늘어납니다.",
    )
    if save_calls:
        candidate_k = st.slider("실제 도로거리로 확인할 후보 수", 3, 12, 5,
                                help="숫자가 클수록 정확하지만 호출이 늘어납니다. 5개면 대부분 충분합니다.")
    else:
        candidate_k = 0

    max_calls = st.number_input(
        "최대 API 호출 수 (초과하면 자동으로 중단합니다)", min_value=50, max_value=100000,
        value=1500, step=100,
        help="여기까지만 호출하고 멈춥니다. 중단되어도 그때까지 편성된 노선은 그대로 볼 수 있습니다.",
    )

st.write("")

# ----------------------------------------------------------------------------
# 4 · 대상 목록 업로드
# ----------------------------------------------------------------------------
with st.container(border=True):
    card_title(4, "대상 목록 업로드")
    st.caption("엑셀(xlsx/xls) · CSV · 아래아한글(hwpx) 표를 올리면 자동으로 인식합니다. "
               "권장 양식: 연번 / 주소지(이름) / 비고 / 정제_주소 / 위도 / 경도")
    uploaded = st.file_uploader("대상 목록 파일", type=["csv", "xlsx", "xls", "hwpx"],
                                label_visibility="collapsed")
    use_sample = st.checkbox("🧪 심사용 예시 30건 불러오기 (성주읍·월항면 마을회관 추석 특별경계근무 실데이터)",
                             value=uploaded is None)

df = None
if uploaded is not None:
    name_lower = uploaded.name.lower()
    if name_lower.endswith(".csv"):
        df = pd.read_csv(uploaded)
    elif name_lower.endswith(".hwpx"):
        df = parse_hwpx(uploaded.getvalue())
        if df is None:
            st.error("hwpx 파일에서 표나 목록을 찾지 못했습니다. 표 형식인지 확인해주세요.")
    else:
        df = pd.read_excel(uploaded)
elif use_sample:
    df = pd.read_csv(SAMPLE_CSV)

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
# 5 · 미리보기 · 노선 생성
# ----------------------------------------------------------------------------
if df is not None and len(df):
    st.write("")
    with st.container(border=True):
        card_title(5, "데이터 확인 · 좌표 찾기")
        st.dataframe(df.head(10), use_container_width=True)
        st.caption(f"총 {len(df)}건의 대상이 인식되었습니다.")
        if excluded_station_rows:
            st.info(f"ℹ️ 목록에 있던 출발지({station_name}) {excluded_station_rows}건은 "
                    "순찰 대상이 아니라 출발·복귀 지점이므로 자동으로 제외했습니다.")

        cols = list(df.columns)
        # 이름 컬럼 기본값: "연번" 같은 숫자 컬럼이 아니라 실제 명칭이 담긴 컬럼을 우선 선택
        name_col_guess_idx = next(
            (i for i, c in enumerate(cols)
             if str(c) not in ("연번",) and ("주소지" in str(c) or "이름" in str(c) or "명" in str(c))),
            None,
        )
        if name_col_guess_idx is None:
            name_col_guess_idx = 1 if len(cols) > 1 else 0
        # 주소 컬럼 기본값: "정제_주소"처럼 지오코딩에 바로 쓸 수 있는 컬럼을 최우선으로
        addr_col_guess_idx = next(
            (i for i, c in enumerate(cols) if "정제" in str(c)),
            next((i for i, c in enumerate(cols)
                  if "주소" in str(c) and str(c) != str(cols[name_col_guess_idx])),
                 min(3, len(cols) - 1)),
        )

        pc1, pc2 = st.columns(2)
        with pc1:
            name_col = st.selectbox("이름(대상명) 컬럼", cols, index=name_col_guess_idx)
        with pc2:
            addr_col = st.selectbox("지오코딩에 사용할 주소 컬럼", cols, index=addr_col_guess_idx)

        lat_col_guess = next((c for c in cols if "위도" in str(c) or str(c).lower() == "lat"), None)
        lng_col_guess = next((c for c in cols if "경도" in str(c) or str(c).lower() in ("lng", "lon")), None)
        has_coords = bool(lat_col_guess and lng_col_guess)

        sub_label("좌표 처리 방식")
        if has_coords:
            coord_mode_label = st.pills(
                "좌표 처리 방식",
                ["주소로 새로 찾기(권장)", "파일 좌표 + 검증", "파일 좌표 그대로 사용"],
                default="주소로 새로 찾기(권장)", label_visibility="collapsed",
            ) or "주소로 새로 찾기(권장)"
            coord_mode = {
                "주소로 새로 찾기(권장)": "geocode",
                "파일 좌표 + 검증": "verify",
                "파일 좌표 그대로 사용": "file",
            }[coord_mode_label]
            st.caption({
                "geocode": "파일의 위·경도는 무시하고 주소만 보고 NCP Geocoding으로 좌표를 새로 찾습니다. "
                           "가장 정확하며, 다른 도구로 만든 좌표가 틀렸을 때 이 방식으로 바로잡을 수 있습니다.",
                "verify": "파일 좌표와 NCP가 찾은 좌표를 비교해, 차이가 큰 항목은 NCP 좌표로 바꾸고 목록으로 알려줍니다.",
                "file": "파일에 적힌 좌표를 그대로 씁니다. 호출은 가장 적지만 좌표가 틀려도 걸러지지 않습니다.",
            }[coord_mode])
            if coord_mode == "verify":
                verify_tol_km = st.number_input("검증 허용 오차(km) — 이보다 많이 다르면 NCP 좌표로 교체",
                                                min_value=0.1, value=1.0, step=0.5)
            else:
                verify_tol_km = 1.0
        else:
            coord_mode = "geocode"
            verify_tol_km = 1.0
            st.caption("파일에 위·경도가 없어 주소로 좌표를 찾습니다(NCP Geocoding).")


        n_targets = len(df)
        st.caption(f"1단계에서 좌표를 먼저 확정하고, 2단계에서 그 좌표로 노선을 만듭니다. "
                   f"좌표 찾기에는 약 {n_targets}~{n_targets * 3}회의 호출이 듭니다.")

        find_coords = st.button("① 좌표 찾기 (주소 → 위·경도)", type="primary",
                                disabled=not has_keys(), use_container_width=True)

    # ---- 1단계: 좌표 확정 ---------------------------------------------------
    if find_coords:
        rows = []
        prog = st.progress(0.0, text="주소로 좌표를 찾는 중...")
        n = len(df)
        for i, (_, row) in enumerate(df.iterrows()):
            nm, ad = str(row[name_col]), str(row[addr_col])

            file_lat = file_lng = None
            if lat_col_guess and lng_col_guess:
                try:
                    file_lat, file_lng = float(row[lat_col_guess]), float(row[lng_col_guess])
                    if math.isnan(file_lat) or math.isnan(file_lng):
                        file_lat = file_lng = None
                except (TypeError, ValueError):
                    file_lat = file_lng = None

            if coord_mode == "file" and file_lat is not None:
                rows.append({"대상명": nm, "주소": ad, "위도": file_lat, "경도": file_lng,
                             "상태": "파일 좌표", "비고": "파일에 있던 좌표를 그대로 사용"})
            else:
                lat, lng, used_q, used_why, tried = geocode_with_fallback(ad, nm)
                if lat is None and file_lat is not None:
                    rows.append({"대상명": nm, "주소": ad, "위도": file_lat, "경도": file_lng,
                                 "상태": "⚠ 파일 좌표로 대체",
                                 "비고": "주소로는 못 찾아 파일 좌표를 사용했습니다"})
                elif lat is None:
                    rows.append({"대상명": nm, "주소": ad, "위도": None, "경도": None,
                                 "상태": "❌ 실패",
                                 "비고": "시도: " + " / ".join(tried)})
                else:
                    gap_note = ""
                    if coord_mode == "verify" and file_lat is not None:
                        gap = haversine_km((file_lat, file_lng), (lat, lng))
                        if gap > verify_tol_km:
                            gap_note = f" (파일 좌표와 {gap:.1f}km 차이 — API 좌표로 교체)"
                        else:
                            lat, lng = file_lat, file_lng
                    rows.append({
                        "대상명": nm, "주소": ad, "위도": lat, "경도": lng,
                        "상태": "✅ 확인" if used_why == "원본 주소" else "🔧 주소 보정 후 확인",
                        "비고": ("" if used_why == "원본 주소" else f"{used_why} → {used_q}") + gap_note,
                    })
            prog.progress((i + 1) / n, text=f"주소로 좌표를 찾는 중... ({i+1}/{n})")
        prog.empty()
        st.session_state["coords_df"] = pd.DataFrame(rows)
        st.session_state.pop("route_results", None)   # 좌표가 바뀌었으니 이전 노선 결과는 폐기

    coords_df = st.session_state.get("coords_df")

    if coords_df is not None:
        st.write("")
        with st.container(border=True):
            card_title(6, "좌표 확인 · 직접 수정")
            ok_n = int(coords_df["위도"].notna().sum())
            fail_n = int(coords_df["위도"].isna().sum())
            k1, k2, k3 = st.columns(3)
            k1.metric("전체", f"{len(coords_df)}")
            k2.metric("좌표 확보", f"{ok_n}")
            k3.metric("좌표 없음", f"{fail_n}")

            if fail_n:
                st.error(f"❌ {fail_n}건은 좌표를 찾지 못했습니다. 아래 표의 **위도·경도 칸에 직접 입력**하시면 "
                         "노선 생성에 포함됩니다. (네이버·카카오 지도에서 해당 지점을 찍고 좌표를 확인해 넣으시면 됩니다.)")
            else:
                st.success("✅ 모든 대상의 좌표가 확보되었습니다. 아래 2단계로 진행하세요.")

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
            st.session_state["coords_df"] = edited

            st.download_button(
                "📥 확정된 좌표 CSV로 저장 (다음엔 이 파일을 올리면 좌표 찾기 없이 바로 진행)",
                data=edited.to_csv(index=False).encode("utf-8-sig"),
                file_name="확정좌표.csv", mime="text/csv",
            )

        st.write("")
        with st.container(border=True):
            card_title(7, "노선 생성")
            ready = edited["위도"].notna() & edited["경도"].notna()
            n_ready = int(ready.sum())

            if candidate_k:
                est_calls = n_ready * candidate_k + n_ready
            else:
                est_calls = n_ready * (n_ready + 1) // 2 + n_ready
            est_sec = int(est_calls * 0.25)
            st.info(f"좌표 확보 {n_ready}개소 기준 · 예상 NCP 호출 약 **{est_calls:,}회** "
                    f"(약 {est_sec // 60}분 {est_sec % 60}초). "
                    + ("‘API 호출 절약’이 켜져 있습니다." if candidate_k
                       else "⚠ ‘API 호출 절약’이 꺼져 있어 호출량이 많습니다."))
            st.caption(f"⛔ 최대 {max_calls:,}회까지만 호출하고 자동으로 멈춥니다.")

            if n_ready < len(edited):
                st.warning(f"좌표가 없는 {len(edited) - n_ready}건은 노선에서 제외됩니다.")

            run = st.button("② 노선 생성 (실도로거리 계산)", type="primary",
                            disabled=(not has_keys() or n_ready == 0), use_container_width=True)
    else:
        run = False
        st.info("먼저 위의 **① 좌표 찾기**를 눌러 좌표를 확정해 주세요.")

    if run:
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

        # 1) 소방서 좌표
        with st.spinner("소방서 좌표 확인 중..."):
            s_lat, s_lng = geocode_address(station_address)
        if s_lat is None:
            st.error("소방서 주소 지오코딩에 실패했습니다. 주소를 확인해주세요.")
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
            st.error("좌표가 있는 대상이 없습니다. 1단계에서 좌표를 확정해 주세요.")
            st.stop()

        # 3) 장거리 분리 (실도로거리 기준)
        long_progress = st.progress(0.0, text="소방서 기준 실도로거리 확인 중...")

        def bump(total_hint=len(points)):
            call_counter["n"] += 1
            long_progress.progress(min(call_counter["n"] / max(total_hint, 1), 1.0),
                                   text=f"실제 도로거리 API 호출 중... "
                                        f"({call_counter['n']:,}/{max_calls:,}회)")

        normal_points, far_points = separate_long_distance(
            points, station, long_threshold, on_call=bump, save_calls=bool(candidate_k),
            should_stop=over_limit,
        )
        long_progress.empty()

        # 4) 노선 편성
        build_progress = st.empty()

        def bump_build():
            call_counter["n"] += 1
            build_progress.text(f"실도로 기준 노선 편성 중... "
                                f"(API 호출 {call_counter['n']:,}/{max_calls:,}회)")

        routes, unassigned = build_routes(
            normal_points, station, mode, max_per_route,
            seg_max_km, seg_max_min, target_min_high,
            max_routes_cap or None, basis=basis, on_call=bump_build,
            candidate_k=candidate_k, should_stop=over_limit,
        )
        build_progress.empty()

        if limit_hit["v"]:
            st.warning(
                f"⛔ API 호출 한도({max_calls:,}회)에 도달해 노선 편성을 중단했습니다. "
                f"그때까지 편성된 {len(routes)}개 노선은 아래에 그대로 표시됩니다. "
                "한도를 늘리거나 'API 호출 절약'을 켜고 다시 실행해 보세요."
            )

        if unassigned:
            for p in unassigned:
                km, _ = real_leg(station, p)
                far_points.append({**p, "도로거리_km": round(km, 1)})

        # 용도별 부가 정보
        team_info = ""
        if purpose == "hydrant" and hydrant_teams:
            rounds_needed = math.ceil(len(routes) / hydrant_teams) if routes else 0
            team_info = f" · 팀 {hydrant_teams}개 기준 팀당 {rounds_needed}회"
        elif purpose == "other" and other_teams:
            rounds_needed = math.ceil(len(routes) / other_teams) if routes else 0
            team_info = f" · 팀 {other_teams}개 기준 팀당 {rounds_needed}회"
        elif purpose == "guard" and guard_repeat_label == "매일 같은 코스 반복" and guard_rounds:
            total_runs = int(guard_rounds.replace("회", "")) * period_days
            team_info = f" · 매일 같은 코스로 하루 {guard_rounds} 반복({period_days}일간 총 {total_runs}회)"
        elif purpose == "guard":
            team_info = f" · 매일 다른 코스로 순환({period_days}일간 {len(routes)}개 노선 배정)"

        st.success(f"[{purpose_label}] 총 {len(routes)}개 노선, {sum(len(r) for r in routes)}개소 배정 완료 "
                   f"(장거리 별도 {len(far_points)}개소){team_info}")

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
                legs.append({"from": cur["name"], "to": p["name"], "to_address": p.get("address", ""),
                             "km": km, "min": mins, "lat": p["lat"], "lng": p["lng"]})
                acc_km += km
                acc_min += mins
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

            route_results.append({
                "route_no": ri + 1, "stops": route, "legs": legs,
                "back_km": back_km, "back_min": back_min,
                "total_km": acc_km, "total_min": acc_min, "path": all_path,
            })
        call_progress.empty()

        st.session_state["station"] = station
        st.session_state["route_results"] = route_results
        st.session_state["far_points"] = far_points
        st.session_state["meta"] = {
            "title": patrol_title, "purpose": purpose_label, "vehicle": vehicle,
            "period": f"{start_dt:%Y-%m-%d %H:%M} ~ {end_dt:%Y-%m-%d %H:%M} ({period_days}일간)",
            "basis": basis_label, "route_prefix": route_prefix, "team_info": team_info.strip(" ·"),
            "target_min": target_min,
        }

# ----------------------------------------------------------------------------
# 결과 표시
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
    st.header("📍 노선 생성 결과")
    if meta:
        st.caption(f"**{meta.get('title','')}** · {meta.get('purpose','')} · 기준: {meta.get('basis','')} · "
                   f"순찰기간 {meta.get('period','')} · 차량: {meta.get('vehicle','')}"
                   + (f" · {meta['team_info']}" if meta.get("team_info") else ""))

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("생성 노선 수", f"{len(route_results)}")
    m2.metric("전체 방문지", f"{sum(len(r['stops']) for r in route_results)}")
    m3.metric("총 이동거리(km)", f"{sum(r['total_km'] for r in route_results):.1f}")
    m4.metric("원거리 분리 대상", f"{len(far_points)}")

    # ---- 담당 조 · 조원 입력(화면에서 직접 입력 → 엑셀에 그대로 반영) ----
    with st.container(border=True):
        card_title("조", "노선별 담당 조 · 조원 입력")
        st.caption("여기에 입력한 내용은 아래 엑셀 다운로드 파일에 그대로 들어갑니다.")
        for rr in route_results:
            tc1, tc2, tc3 = st.columns([0.8, 1.2, 2])
            with tc1:
                st.markdown(f"**노선 {rr['route_no']}**")
            with tc2:
                st.text_input("담당 조 이름", key=f"team_name_{rr['route_no']}",
                              placeholder="예) 가천1팀1조", label_visibility="collapsed")
            with tc3:
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
        title_cell = ws.cell(row=1, column=1, value=f"{meta.get('title', '순찰노선')}   "
                                                    f"[{meta.get('purpose', '')} · {meta.get('vehicle', '')} · "
                                                    f"{meta.get('period', '')}]")
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
            ws.cell(row=r, column=c, value=st.session_state.get(f"team_name_{no}", "")); c += 1
            ws.cell(row=r, column=c, value=st.session_state.get(f"team_members_{no}", "")); c += 1
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

    safe_title = re.sub(r'[\\/:*?"<>|]', "_", meta.get("title", "순찰노선")) or "순찰노선"
    st.download_button(
        "📥 전체 노선 엑셀(xlsx)로 다운로드",
        data=build_wide_excel(station, route_results, far_points, meta),
        file_name=f"{safe_title}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    target_min_ref = meta.get("target_min")
    if target_min_ref:
        over = [r for r in route_results if r["total_min"] > target_min_ref]
        if over:
            st.warning(
                f"⏱ 목표 {target_min_ref}분을 넘는 노선이 {len(over)}개 있습니다 "
                f"(노선 {', '.join(str(r['route_no']) for r in over)}). "
                "노선당 구간 수를 줄이거나 목표시간을 늘려 다시 편성해 보세요."
            )
        else:
            st.success(f"⏱ 모든 노선이 목표 {target_min_ref}분 이내입니다.")

    for rr in route_results:
        team_name = st.session_state.get(f"team_name_{rr['route_no']}", "")
        over_mark = " ⏱초과" if target_min_ref and rr["total_min"] > target_min_ref else ""
        head = (f"노선 {rr['route_no']}" + (f" · {team_name}" if team_name else "") +
                f" — {len(rr['stops'])}개소 · 총 {rr['total_km']:.1f}km · 약 {rr['total_min']:.0f}분{over_mark}")
        with st.expander(head, expanded=True):
            col1, col2 = st.columns([1, 1])

            with col1:
                rows = []
                for i, leg in enumerate(rr["legs"], start=1):
                    rows.append({
                        "순번": str(i), "지점": stop_label(leg["to"], leg.get("to_address")),
                        "구간거리(km)": round(leg["km"], 1), "구간시간(분)": round(leg["min"]),
                    })
                rows.append({"순번": "", "지점": f"복귀 ({station['name']})",
                             "구간거리(km)": round(rr["back_km"], 1),
                             "구간시간(분)": round(rr["back_min"])})
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                st.markdown("**📱 내비게이션 — 경유지 포함 전체 경로**")
                links = naver_route_links(station, rr["legs"])
                for li, (nurl, wurl, chunk) in enumerate(links, start=1):
                    seq = " → ".join([station["name"]] + [c["name"] for c in chunk] + [station["name"]])
                    suffix = "" if len(links) == 1 else f" ({li}/{len(links)}구간)"
                    iurl = naver_intent_url(nurl)
                    st.markdown(
                        f'<a class="paseru-navbtn nav-and" href="{iurl}" target="_top">'
                        f'📱 안드로이드폰에서 경유지 안내{suffix}</a>'
                        f'<a class="paseru-navbtn nav-ios" href="{nurl}" target="_top">'
                        f'📱 아이폰에서 경유지 안내{suffix}</a>'
                        f'<a class="paseru-navbtn nav-pc" href="{wurl}" target="_blank">'
                        f'💻 PC에서 지도로 보기{suffix}</a>',
                        unsafe_allow_html=True,
                    )
                    st.caption(f"경로: {seq}")
                    with st.expander("🔧 버튼이 안 열릴 때 — 주소를 복사해 직접 열어보기", expanded=False):
                        st.markdown("아래 주소를 **복사해서 휴대폰 브라우저 주소창에 붙여넣고 이동**해 보세요. "
                                    "버튼은 막혀도 주소창 입력은 열리는 경우가 많습니다.")
                        st.markdown("**① 안드로이드폰용 (intent 방식)**")
                        st.code(iurl, language=None)
                        st.markdown("**② 안드로이드·아이폰 공용 (nmap 방식)**")
                        st.code(nurl, language=None)
                        st.markdown("**경유지 좌표 목록** (다른 내비 앱에 직접 입력할 때)")
                        st.code("\n".join(
                            [f"출발  {station['name']}  {station['lat']:.6f}, {station['lng']:.6f}"]
                            + [f"경유{n}  {c['name']}  {c['lat']:.6f}, {c['lng']:.6f}"
                               for n, c in enumerate(chunk, start=1)]
                            + [f"도착  {station['name']}  {station['lat']:.6f}, {station['lng']:.6f}"]
                        ), language=None)
                if len(links) > 1:
                    st.caption(f"※ 네이버지도는 경유지를 최대 {NAVER_MAX_VIA}개까지 지원해서 "
                               "구간을 나눴습니다. 한 구간씩 순서대로 눌러 주세요.")
                st.caption("※ **경유지를 한 번에 안내받는 것은 휴대폰에서만 됩니다** "
                           "(네이버지도 앱 필요). 안드로이드폰은 첫 번째, 아이폰은 두 번째 버튼을 "
                           "누르세요. PC에서는 앱을 열 수 없어 세 번째 버튼으로 경로만 확인됩니다.")

                st.markdown("**🟨 카카오맵 — 전체 순찰코스와 QR코드**")
                kakao_links = kakao_route_links(station, rr["legs"])
                for li, (kurl, origin, destinations) in enumerate(kakao_links, start=1):
                    suffix = "" if len(kakao_links) == 1 else f" ({li}/{len(kakao_links)}구간)"
                    seq = " → ".join([origin["name"]] + [p["name"] for p in destinations])
                    qr_png = make_qr_png(kurl)

                    guide_col, qr_col = st.columns([1.35, 1])
                    with guide_col:
                        st.link_button(
                            f"🚗 카카오맵 전체 코스 길안내{suffix}",
                            kurl,
                            use_container_width=True,
                        )
                    with qr_col:
                        qr_box = (st.popover(f"📱 QR코드 보기{suffix}", use_container_width=True)
                                  if hasattr(st, "popover")
                                  else st.expander(f"📱 QR코드 보기{suffix}"))
                        with qr_box:
                            st.image(qr_png, caption="휴대폰 카메라로 스캔하세요.", width=260)
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

            with col2:
                m = folium.Map(location=[station["lat"], station["lng"]], zoom_start=12)
                if rr["path"]:
                    folium.PolyLine(rr["path"], color="#dc2626", weight=4, opacity=0.85).add_to(m)

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
                            '<div style="background:#dc2626;color:#ffffff;'
                            'padding:3px 8px;border-radius:13px;border:2px solid #ffffff;'
                            'box-shadow:0 1px 5px rgba(0,0,0,.45);text-align:center;'
                            'font-family:sans-serif;font-weight:700;font-size:12px;'
                            'line-height:1.2;white-space:nowrap;">🚒 출발</div>'
                        ),
                    ),
                ).add_to(m)
                st_folium(m, height=350, use_container_width=True, key=f"map_{rr['route_no']}")

    if far_points:
        st.header("⚠️ 장거리 별도 대상")
        st.dataframe(pd.DataFrame(far_points)[["name", "address", "도로거리_km"]],
                     use_container_width=True, hide_index=True)
        for p in far_points:
            st.markdown(
                f"- [{p['name']} 길안내]({kakao_url(p['name'], p['lat'], p['lng'])})"
                f" · 실도로거리 {p['도로거리_km']}km"
            )

    st.divider()
    st.caption(
        "⚠️ 이 페이지의 API 키는 서버(Secrets)에만 저장되며 브라우저로 노출되지 않습니다. "
        "실제 도로거리·소요시간은 NCP Geocoding·Directions5 실시간 계산 결과입니다. "
        "📲 휴대폰에서는 브라우저 메뉴의 '홈 화면에 추가'를 누르면 앱처럼 사용할 수 있습니다."
    )
