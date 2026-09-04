import io
import math
import time

import folium
import pandas as pd
import requests
import streamlit as st
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


def build_routes(points, station, mode, max_per_route, seg_max_km, seg_max_min,
                  target_min_high, max_routes_cap, on_call=None):
    """points: list of dict(name, address, lat, lng)
    반환: routes(list of list of point dict), unassigned(장거리/미배정)
    매 단계마다 NCP Directions5 실도로거리로 다음 방문지를 선택한다.
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
            # 실도로거리 기준 가장 가까운 다음 지점 선택
            legs = [(p, *real_leg(cur, p, on_call)) for p in remaining]
            legs.sort(key=lambda t: t[1])  # km 기준 정렬
            nxt, leg_km, leg_min = legs[0]

            if mode == "segment":
                if leg_km > seg_max_km or leg_min > seg_max_min:
                    break
                if len(route) >= max_per_route:
                    break
            else:  # target_time
                back_km, back_min = real_leg(nxt, station, on_call)
                projected = acc_min + leg_min + back_min
                if route and projected > target_min_high:
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


def separate_long_distance(points, station, threshold_km, on_call=None):
    normal, far = [], []
    for p in points:
        km, _ = real_leg(station, p, on_call)
        if km > threshold_km:
            far.append({**p, "도로거리_km": round(km, 1)})
        else:
            normal.append(p)
    return normal, far


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
st.title("🚒 파세루 오리진 (FireSafe Route Origin)")
st.caption("주소 목록을 업로드하면 실제 도로거리 기준(NCP Geocoding·Directions5 실연동)으로 순찰노선을 자동 편성합니다.")

if not has_keys():
    st.error(
        "NCP(네이버클라우드플랫폼) Client ID/Secret이 설정되지 않았습니다. "
        "`.streamlit/secrets.toml` 또는 Streamlit Cloud의 Secrets 설정에 "
        "NCP_CLIENT_ID / NCP_CLIENT_SECRET 값을 등록해주세요."
    )

with st.sidebar:
    st.header("① 출발점(소방서) 설정")
    station_name = st.text_input("이름", value="성주소방서")
    station_address = st.text_input("주소", value="경상북도 성주군 성주읍 주산로 193")

    st.header("② 순찰 기준 설정")
    mode_label = st.radio("기준 방식", ["구간별 제한", "노선 전체 목표시간"])
    mode = "segment" if mode_label == "구간별 제한" else "target_time"

    max_per_route = st.number_input("노선당 최대 대상 수", min_value=1, max_value=30, value=4)

    if mode == "segment":
        seg_max_km = st.number_input("구간당 최대 거리(km)", min_value=1.0, value=7.0, step=0.5)
        seg_max_min = st.number_input("구간당 최대 시간(분)", min_value=1, value=10)
        target_min = target_min_low = target_min_high = None
    else:
        target_min = st.number_input("목표 왕복시간(분)", min_value=10, value=60)
        allow_range = st.slider("허용 범위(분, ±)", 0, 60, 15)
        target_min_low = target_min - allow_range
        target_min_high = target_min + allow_range
        seg_max_km = seg_max_min = None

    max_routes_cap = st.number_input("전체 노선 개수 상한(0=무제한)", min_value=0, value=0)

    st.header("③ 장거리 분리 기준")
    long_threshold = st.number_input("소방서 실제 도로거리(km) 초과 시 별도 표시", min_value=1.0, value=15.0)

st.subheader("주소 파일 업로드")
uploaded = st.file_uploader("CSV 또는 Excel 파일 (주소 컬럼 포함)", type=["csv", "xlsx", "xls"])

use_sample = st.checkbox("샘플 데이터로 테스트 (성주읍·월항면 마을회관 30개소)", value=uploaded is None)

df = None
if uploaded is not None:
    if uploaded.name.endswith(".csv"):
        df = pd.read_csv(uploaded)
    else:
        df = pd.read_excel(uploaded)
elif use_sample:
    df = pd.read_csv("seongju_patrol_coordinates_updated_modified.csv")
    # 이 샘플 파일의 연번 0행은 출발점(성주소방서) 자신이므로 순찰 대상 목록에서 제외
    if "연번" in df.columns:
        df = df[df["연번"] != 0].reset_index(drop=True)

if df is not None:
    st.write("업로드된 데이터 미리보기")
    st.dataframe(df.head(10), use_container_width=True)

    cols = list(df.columns)
    name_col = st.selectbox("이름(주소지) 컬럼", cols, index=0)
    addr_col = st.selectbox(
        "지오코딩에 사용할 주소 컬럼", cols, index=min(3, len(cols) - 1)
    )
    lat_col_guess = next((c for c in cols if "위도" in c or c.lower() == "lat"), None)
    lng_col_guess = next((c for c in cols if "경도" in c or c.lower() in ("lng", "lon")), None)
    use_existing_coords = st.checkbox(
        "파일에 이미 위·경도가 있으면 재지오코딩 없이 사용", value=bool(lat_col_guess and lng_col_guess)
    )

    run = st.button("🚀 노선 생성 (실제 지오코딩 · 실도로거리 계산)", type="primary", disabled=not has_keys())

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
        for i, row in df.iterrows():
            if use_existing_coords and lat_col_guess and lng_col_guess:
                lat, lng = row[lat_col_guess], row[lng_col_guess]
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

        normal_points, far_points = separate_long_distance(points, station, long_threshold, on_call=bump)
        long_progress.empty()

        # 4) 노선 편성 (매 단계 NCP Directions5 실도로거리로 최근접 지점 선택)
        call_counter["n"] = 0
        build_progress = st.empty()

        def bump_build():
            call_counter["n"] += 1
            build_progress.text(f"실도로거리 기준 노선 편성 중... (API 호출 {call_counter['n']}건)")

        routes, unassigned = build_routes(
            normal_points, station, mode, max_per_route,
            seg_max_km, seg_max_min, target_min_high,
            max_routes_cap or None, on_call=bump_build,
        )
        build_progress.empty()

        if unassigned:
            for p in unassigned:
                km, _ = real_leg(station, p)
                far_points.append({**p, "도로거리_km": round(km, 1)})

        st.success(f"총 {len(routes)}개 노선, {sum(len(r) for r in routes)}개소 배정 완료 "
                   f"(장거리 별도 {len(far_points)}개소)")

        # 5) 확정 노선의 구간별 실도로거리·경로좌표 (이미 계산된 값은 캐시로 재사용됨)
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
                legs.append({"from": cur["name"], "to": p["name"], "km": km, "min": mins,
                             "lat": p["lat"], "lng": p["lng"]})
                acc_km += km
                acc_min += mins
                all_path += path
                cur = p
                done += 1
                call_progress.progress(min(done / total_calls, 1.0), text="노선별 실도로 경로 확정 중...")
            back_km, back_min, back_path = road_route(cur["lat"], cur["lng"], station["lat"], station["lng"])
            if back_km is None:
                back_km = haversine_km((cur["lat"], cur["lng"]), (station["lat"], station["lng"])) * ROAD_FACTOR
                back_min = back_km / AVG_SPEED_KMH * 60
                back_path = [(cur["lat"], cur["lng"]), (station["lat"], station["lng"])]
            acc_km += back_km
            acc_min += back_min
            all_path += back_path
            done += 1
            call_progress.progress(min(done / total_calls, 1.0), text="노선별 실도로 경로 확정 중...")

            route_results.append({
                "route_no": ri + 1, "stops": route, "legs": legs,
                "back_km": back_km, "back_min": back_min,
                "total_km": acc_km, "total_min": acc_min, "path": all_path,
            })
        call_progress.empty()

        st.session_state["station"] = station
        st.session_state["route_results"] = route_results
        st.session_state["far_points"] = far_points

# ----------------------------------------------------------------------------
# 결과 표시
# ----------------------------------------------------------------------------
if "route_results" in st.session_state:
    station = st.session_state["station"]
    route_results = st.session_state["route_results"]
    far_points = st.session_state["far_points"]

    st.header("📍 노선별 결과")

    # ---- 엑셀(xlsx) 다운로드: 관리자가 받아서 담당 조·조원 등을 직접 채워 넣을 수 있도록 ----
    xlsx_rows = []
    for rr in route_results:
        for i, leg in enumerate(rr["legs"], start=1):
            xlsx_rows.append({
                "노선": f"노선 {rr['route_no']}", "순번": i, "목적지": leg["to"],
                "구간거리(km)": round(leg["km"], 1), "구간시간(분)": round(leg["min"]),
                "담당 조 이름": "", "조원": "",
            })
        xlsx_rows.append({
            "노선": f"노선 {rr['route_no']}", "순번": "", "목적지": f"복귀 ({station['name']})",
            "구간거리(km)": round(rr["back_km"], 1), "구간시간(분)": round(rr["back_min"]),
            "담당 조 이름": "", "조원": "",
        })
    for p in far_points:
        xlsx_rows.append({
            "노선": "장거리 별도", "순번": "", "목적지": p["name"],
            "구간거리(km)": p.get("도로거리_km", ""), "구간시간(분)": "",
            "담당 조 이름": "", "조원": "",
        })
    xlsx_buf = io.BytesIO()
    pd.DataFrame(xlsx_rows).to_excel(xlsx_buf, index=False, engine="openpyxl")
    st.download_button(
        "📥 전체 노선 엑셀(xlsx)로 다운로드",
        data=xlsx_buf.getvalue(),
        file_name=f"{station['name']}_순찰노선.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    for rr in route_results:
        with st.expander(f"노선 {rr['route_no']} — {len(rr['stops'])}개소 · "
                          f"총 {rr['total_km']:.1f}km · 약 {rr['total_min']:.0f}분", expanded=True):
            col1, col2 = st.columns([1, 1])

            with col1:
                rows = []
                for i, leg in enumerate(rr["legs"], start=1):
                    rows.append({
                        "순번": i, "지점": leg["to"],
                        "구간거리(km)": round(leg["km"], 1), "구간시간(분)": round(leg["min"]),
                    })
                rows.append({"순번": "", "지점": f"복귀 ({station['name']})",
                              "구간거리(km)": round(rr["back_km"], 1),
                              "구간시간(분)": round(rr["back_min"])})
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                st.markdown("**📱 내비게이션 바로가기**")
                for leg in rr["legs"]:
                    kakao_link = f"https://map.kakao.com/link/to/{leg['to']},{leg['lat']},{leg['lng']}"
                    st.markdown(f"- [{leg['to']} 길안내]({kakao_link})")

            with col2:
                m = folium.Map(location=[station["lat"], station["lng"]], zoom_start=12)
                folium.Marker([station["lat"], station["lng"]], tooltip=station["name"],
                               icon=folium.Icon(color="red", icon="home")).add_to(m)
                for i, leg in enumerate(rr["legs"], start=1):
                    folium.Marker([leg["lat"], leg["lng"]], tooltip=f"{i}. {leg['to']}",
                                  icon=folium.Icon(color="blue")).add_to(m)
                if rr["path"]:
                    folium.PolyLine(rr["path"], color="#2563eb", weight=4, opacity=0.8).add_to(m)
                st_folium(m, height=350, use_container_width=True, key=f"map_{rr['route_no']}")

    if far_points:
        st.header("⚠️ 장거리 별도 대상")
        st.dataframe(pd.DataFrame(far_points)[["name", "address", "도로거리_km"]],
                     use_container_width=True, hide_index=True)
        for p in far_points:
            kakao_link = f"https://map.kakao.com/link/to/{p['name']},{p['lat']},{p['lng']}"
            st.markdown(f"- [{p['name']} 길안내]({kakao_link}) (실도로거리 {p['도로거리_km']}km)")

    st.divider()
    st.caption(
        "⚠️ 이 페이지의 API 키는 서버(Secrets)에만 저장되며 브라우저로 노출되지 않습니다. "
        "실제 도로거리·소요시간은 NCP Directions5 실시간 계산 결과입니다."
        
    )
