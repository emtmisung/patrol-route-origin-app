"""NCP 지오코딩·길찾기 호출과 주소 보정."""
import math
import re

import requests
import streamlit as st

from paseru import usage_tracker
from paseru.settings import (
    DIRECTIONS_URL,
    GEOCODE_URL,
    KAKAO_PLACE_SEARCH_URL,
    ncp_key,
    ncp_key_id,
)


def ncp_headers():
    return {
        "x-ncp-apigw-api-key-id": ncp_key_id(),
        "x-ncp-apigw-api-key": ncp_key(),
        "Accept": "application/json",
    }


def has_keys():
    return bool(ncp_key_id()) and bool(ncp_key())


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
        usage_tracker.add_calls(1)
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
        usage_tracker.add_calls(1)
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
        usage_tracker.add_calls(1)
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
        usage_tracker.add_calls(1)
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
