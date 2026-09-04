import io
import math
import re
import zipfile
from datetime import datetime, date, time as dtime
from urllib.parse import quote

import folium
import pandas as pd
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
def geocode_address(address: str):
    """주소 -> (lat, lng). 실패 시 (None, None)."""
    try:
        r = requests.get(
            GEOCODE_URL, params={"query": address}, headers=ncp_headers(), timeout=10
        )
        data = r.json()
        addrs = data.get("addresses") or []
        if addrs:
            a = addrs[0]
            return float(a["y"]), float(a["x"])
        return None, None
    except Exception:
        return None, None


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
                 candidate_k=5):
    """points: list of dict(name, address, lat, lng)
    반환: routes(list of list of point dict), unassigned(장거리/미배정)

    mode:
      "segment"     — 구간당 거리·시간 제한
      "target_time" — 노선 전체 왕복 목표시간 제한
      "fixed"       — 노선당 구간 수(max_per_route)를 그대로 채움 (노선 수 = 상한까지)
    basis: "distance"(거리 기준) | "time"(소요시간 기준)
    candidate_k: 다음 지점 후보를 직선거리로 몇 개까지 좁혀서 실제 API로 확인할지 (0=전수)
    """
    remaining = points[:]
    routes = []

    guard = 0
    while remaining and guard < 500:
        guard += 1
        cur = station
        route = []
        acc_min = 0.0

        while remaining:
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


def separate_long_distance(points, station, threshold_km, on_call=None, save_calls=True):
    """소방서에서 실도로거리가 기준을 넘는 대상을 분리한다.

    save_calls=True면 직선거리 추정값이 기준에서 충분히 멀리 떨어진(애매하지 않은)
    대상은 API를 호출하지 않고 추정값으로 판정해 호출 횟수를 줄인다.
    """
    normal, far = [], []
    for p in points:
        straight = haversine_km((station["lat"], station["lng"]), (p["lat"], p["lng"]))
        est = straight * ROAD_FACTOR

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
  --accent:#c23c2c; --accent-hover:#a32f22; --accent-soft:#f4ddd8;
  --line:#d7ddd2; --surface:#ffffff; --bg:#f1f4f0; --ink:#1c2420; --muted:#5c6660;
}
.stApp{ background: var(--bg); }
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
  box-shadow: 0 1px 2px rgba(28,36,32,.06), 0 10px 26px -14px rgba(28,36,32,.22);
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
  box-shadow: 0 4px 12px -6px rgba(194,60,44,.8);
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
  box-shadow: 0 6px 16px -8px rgba(194,60,44,.7);
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
div[data-testid="stMetricValue"]{ font-family:'IBM Plex Mono', monospace; }
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
      background_color: "#f1f4f0", theme_color: "#c23c2c",
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
    theme.name = 'theme-color'; theme.content = '#c23c2c';
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
    # 이 샘플 파일의 연번 0행은 출발점(성주소방서) 자신이므로 순찰 대상 목록에서 제외
    if "연번" in df.columns:
        df = df[df["연번"] != 0].reset_index(drop=True)

# ----------------------------------------------------------------------------
# 5 · 미리보기 · 노선 생성
# ----------------------------------------------------------------------------
if df is not None and len(df):
    st.write("")
    with st.container(border=True):
        card_title(5, "데이터 확인 · 노선 생성")
        st.dataframe(df.head(10), use_container_width=True)
        st.caption(f"총 {len(df)}건의 대상이 인식되었습니다.")

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
        use_existing_coords = st.checkbox(
            "파일에 이미 위·경도가 있으면 재지오코딩 없이 사용", value=bool(lat_col_guess and lng_col_guess)
        )

        # 실행 전 예상 API 호출량 안내 (요금·시간 가늠용)
        n_targets = len(df)
        if candidate_k:
            est_calls = n_targets * candidate_k + n_targets  # 후보 확인 + 장거리 판정(일부)
        else:
            est_calls = n_targets * (n_targets + 1) // 2 + n_targets
        est_sec = int(est_calls * 0.25)
        st.info(f"대상 {n_targets}개소 · 예상 NCP 호출 약 **{est_calls:,}회** "
                f"(예상 소요 약 {est_sec // 60}분 {est_sec % 60}초). "
                + ("‘API 호출 절약’이 켜져 있습니다." if candidate_k
                   else "⚠ ‘API 호출 절약’이 꺼져 있어 호출량이 많습니다."))

        run = st.button("🚀 노선 생성 (실제 지오코딩 · 실도로거리 계산)", type="primary",
                        disabled=not has_keys(), use_container_width=True)

    if run:
        # 1) 소방서 좌표
        with st.spinner("소방서 좌표 확인 중..."):
            s_lat, s_lng = geocode_address(station_address)
        if s_lat is None:
            st.error("소방서 주소 지오코딩에 실패했습니다. 주소를 확인해주세요.")
            st.stop()
        station = {"name": station_name, "lat": s_lat, "lng": s_lng}

        # 2) 대상지 지오코딩
        points = []
        progress = st.progress(0.0, text="주소 지오코딩 중...")
        n = len(df)
        for i, (_, row) in enumerate(df.iterrows()):
            if use_existing_coords and lat_col_guess and lng_col_guess:
                lat, lng = row[lat_col_guess], row[lng_col_guess]
                try:
                    lat, lng = float(lat), float(lng)
                except (TypeError, ValueError):
                    lat, lng = geocode_address(str(row[addr_col]))
            else:
                lat, lng = geocode_address(str(row[addr_col]))
            if lat is not None and not (isinstance(lat, float) and math.isnan(lat)):
                points.append(
                    {"name": str(row[name_col]), "address": str(row[addr_col]),
                     "lat": float(lat), "lng": float(lng)}
                )
            progress.progress((i + 1) / n, text=f"주소 지오코딩 중... ({i+1}/{n})")
        progress.empty()

        failed = n - len(points)
        if failed:
            st.warning(f"{failed}건은 지오코딩에 실패해 제외되었습니다.")

        # 3) 장거리 분리 (실도로거리 기준)
        call_counter = {"n": 0}
        long_progress = st.progress(0.0, text="소방서 기준 실도로거리 확인 중...")

        def bump(total_hint=len(points)):
            call_counter["n"] += 1
            long_progress.progress(min(call_counter["n"] / max(total_hint, 1), 1.0),
                                   text=f"실제 도로거리 API 호출 중... ({call_counter['n']}건)")

        normal_points, far_points = separate_long_distance(
            points, station, long_threshold, on_call=bump, save_calls=bool(candidate_k)
        )
        long_progress.empty()

        # 4) 노선 편성
        call_counter["n"] = 0
        build_progress = st.empty()

        def bump_build():
            call_counter["n"] += 1
            build_progress.text(f"실도로 기준 노선 편성 중... (API 호출 {call_counter['n']}건)")

        routes, unassigned = build_routes(
            normal_points, station, mode, max_per_route,
            seg_max_km, seg_max_min, target_min_high,
            max_routes_cap or None, basis=basis, on_call=bump_build,
            candidate_k=candidate_k,
        )
        build_progress.empty()

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
                        "순번": i, "지점": stop_label(leg["to"], leg.get("to_address")),
                        "구간거리(km)": round(leg["km"], 1), "구간시간(분)": round(leg["min"]),
                    })
                rows.append({"순번": "", "지점": f"복귀 ({station['name']})",
                             "구간거리(km)": round(rr["back_km"], 1),
                             "구간시간(분)": round(rr["back_min"])})
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                st.markdown("**📱 내비게이션 바로가기**")
                for i, leg in enumerate(rr["legs"], start=1):
                    st.markdown(
                        f"- [{i}. {leg['to']} 길안내]({kakao_url(leg['to'], leg['lat'], leg['lng'])})"
                    )

            with col2:
                m = folium.Map(location=[station["lat"], station["lng"]], zoom_start=12)
                folium.Marker([station["lat"], station["lng"]], tooltip=station["name"],
                              icon=folium.Icon(color="red", icon="home")).add_to(m)
                for i, leg in enumerate(rr["legs"], start=1):
                    folium.Marker([leg["lat"], leg["lng"]], tooltip=f"{i}. {leg['to']}",
                                  icon=folium.Icon(color="blue")).add_to(m)
                if rr["path"]:
                    folium.PolyLine(rr["path"], color="#c23c2c", weight=4, opacity=0.85).add_to(m)
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
