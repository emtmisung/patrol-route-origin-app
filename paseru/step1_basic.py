"""1단계 기본정보 · 대상목록 · 좌표 확인.

원래 app.py 한 파일에 있던 화면 코드를 그대로 옮기고 render(ctx) 함수로 감쌌다."""
import hashlib
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from paseru.exports import kakao_route_url, make_qr_png
from paseru.geo import (
    geocode_failure_reason,
    geocode_once,
    geocode_with_fallback,
    has_keys,
    haversine_km,
    road_route,
    search_departure_department,
)
from paseru.maps import add_manual_location_layer_buttons, build_distribution_map, manual_map_start
from paseru.settings import (
    API_CALL_LIMIT,
    AVG_SPEED_KMH,
    BROWSER_DRAFT_DAYS,
    BROWSER_DRAFT_MAX_ITEMS,
    ROAD_FACTOR,
    app_public_url,
)
from paseru.storage import (
    browser_draft_content,
    browser_draft_label,
    browser_work_key,
    create_mobile_transfer,
    minimum_transfer_targets,
)
from paseru.ui import card_title, geolocation_component, next_tab_button
from paseru.upload import (
    _cell_text,
    build_upload_template,
    find_address_column_index,
    find_name_column_index,
    load_sample_targets,
    parse_hwpx,
    read_uploaded_table,
)


def render(ctx):
    # 앞 단계에서 만든 값(원래 한 파일의 전역 변수였던 것)
    IS_MOBILE_DEVICE = ctx.get("IS_MOBILE_DEVICE")
    browser_storage = ctx.get("browser_storage")
    now_timestamp = ctx.get("now_timestamp")
    saved_drafts = ctx.get("saved_drafts")

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
                    "출발부서 이름(주소)",
                    value=st.session_state.get("station_query", "성주소방서"),
                    placeholder="예: 선남119안전센터 또는 경북 성주군 ○○로 00",
                    help="소방서·119안전센터·구조구급센터 등 출발할 부서명이나 주소를 입력하세요.",
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

        if "load_sample_targets" not in st.session_state:
            st.session_state["load_sample_targets"] = (restored_df is None)

        st.markdown("**대상 목록 업로드**")
        emergency_mode = bool(st.session_state.get("show_hydrant_direct_panel", False))
        if emergency_mode:
            action_col1, _ = st.columns([1, 1], gap="small")
        else:
            action_col1, action_col2 = st.columns(2, gap="small")
        with action_col1:
            if st.button(
                "🚨 [긴급] 소화전 또는 지원집결지 노선안내",
                key="open_hydrant_direct_panel",
                use_container_width=True,
                help="동료에게 소화전 및 집결지 위치를 카카오내비(QR코드)로 안내합니다.",
            ):
                st.session_state["show_hydrant_direct_panel"] = True
                st.session_state["load_sample_targets"] = False
                st.session_state["sample_mode_active"] = False
        if not emergency_mode:
            with action_col2:
                if st.button(
                    "🧪 기능확인용 예시 불러오기",
                    key="load_sample_targets_button",
                    use_container_width=True,
                    help="평가·시연용 성주군 주요 대상 18건을 불러옵니다.",
                ):
                    st.session_state["load_sample_targets"] = True
                    st.session_state["sample_mode_active"] = True
                    st.session_state["show_hydrant_direct_panel"] = False
                    st.session_state.pop("hydrant_direct_result", None)

        use_sample = bool(st.session_state.get("load_sample_targets", False))
        if use_sample:
            st.caption("평가용 예시: 오류 표시가 과하게 복잡하지 않도록 오류 확인용 1건만 남긴 목록")

        if st.session_state.get("show_hydrant_direct_panel", False):
            st.warning("🚨 긴급 공유 안내: 동료에게 소화전 및 집결지 위치를 카카오내비(QR코드)로 안내합니다.")
            if station_lat is not None and station_lng is not None:
                origin_ready = True
                st.success(f"출발지: {station_name} · {station_address}")
            else:
                origin_ready = False
                st.info("먼저 상단의 출발부서 이름(주소)을 조회하거나 현 위치 조회로 출발지를 설정하세요.")

            hydrant_address_col, hydrant_no_col = st.columns([2.15, 1], gap="small")
            with hydrant_address_col:
                hydrant_address = st.text_input(
                    "목적지 주소(소화전·지원집결지)",
                    placeholder="예: 경북 성주군 ○○읍 ○○리 000",
                    key="hydrant_direct_target_address",
                    help="소화전 또는 지원집결지 등 동료에게 안내할 목적지 주소를 입력하세요.",
                )
            with hydrant_no_col:
                number_col, suffix_col = st.columns([5.5, 1], gap="small")
                with number_col:
                    hydrant_direct_no = st.text_input(
                        "표시명(선택)",
                        placeholder="예: 114 또는 지원집결지",
                        key="hydrant_direct_no",
                        help="숫자만 입력하면 '소화전 114호'로, 글자를 입력하면 그 이름으로 표시됩니다.",
                    )
                with suffix_col:
                    st.markdown("<div style='height:1.72rem'></div>", unsafe_allow_html=True)
                    st.markdown(
                        "<div style='padding:.6rem 0;text-align:center;font-weight:800;color:#7f1d1d;'>명</div>",
                        unsafe_allow_html=True,
                    )

            search_disabled = not (origin_ready and hydrant_address.strip())
            if st.button(
                "🔎 주소 검색 후 카카오맵 길안내 만들기",
                type="primary",
                use_container_width=True,
                disabled=search_disabled,
                key="build_hydrant_direct_route",
            ):
                scene_lat = float(station_lat)
                scene_lng = float(station_lng)
                scene_status = "ok"
                origin_address = station_address
                origin_name = station_name or "출발지"

                hydrant_lat, hydrant_lng, hydrant_status = geocode_once(hydrant_address.strip())
                if scene_status == "ok" and hydrant_status == "ok":
                    destination_label = hydrant_direct_no.strip()
                    hydrant_no = re.sub(r"^(소화전\s*)?", "", destination_label)
                    hydrant_no = re.sub(r"\s*호$", "", hydrant_no).strip()
                    if hydrant_no and re.fullmatch(r"\d{1,3}", hydrant_no):
                        hydrant_display_name = f"소화전 {hydrant_no}호"
                    elif destination_label:
                        hydrant_display_name = destination_label
                    else:
                        hydrant_display_name = "긴급 목적지"
                    origin = {
                        "name": origin_name,
                        "address": origin_address,
                        "lat": scene_lat,
                        "lng": scene_lng,
                    }
                    destination = {
                        "name": hydrant_display_name,
                        "address": hydrant_address.strip(),
                        "lat": hydrant_lat,
                        "lng": hydrant_lng,
                    }
                    st.session_state["hydrant_direct_result"] = {
                        "origin": origin,
                        "destination": destination,
                        "url": kakao_route_url(origin, [destination]),
                    }
                else:
                    st.session_state["hydrant_direct_result"] = {
                        "error": {
                            "scene_status": scene_status,
                            "hydrant_status": hydrant_status,
                        }
                    }

            direct_result = st.session_state.get("hydrant_direct_result")
            if isinstance(direct_result, dict) and direct_result.get("error"):
                error_info = direct_result["error"]
                st.error("주소 좌표를 찾지 못했습니다. 출발지와 목적지 주소를 도로명 또는 지번까지 조금 더 정확히 입력해 주세요.")
                st.caption(
                    f"현장주소 검색결과: {error_info.get('scene_status')} · "
                    f"목적지주소 검색결과: {error_info.get('hydrant_status')}"
                )
            elif isinstance(direct_result, dict) and direct_result.get("url"):
                origin = direct_result["origin"]
                destination = direct_result["destination"]
                guide_url = direct_result["url"]
                route_km, route_min, route_path = road_route(
                    origin["lat"], origin["lng"], destination["lat"], destination["lng"],
                )
                if route_km is None:
                    route_km = haversine_km(
                        (origin["lat"], origin["lng"]),
                        (destination["lat"], destination["lng"]),
                    ) * ROAD_FACTOR
                    route_min = route_km / AVG_SPEED_KMH * 60
                    route_path = [
                        (origin["lat"], origin["lng"]),
                        (destination["lat"], destination["lng"]),
                    ]

                st.success("긴급 목적지 노선안내 결과가 준비되었습니다.")
                info_a, info_b = st.columns(2)
                with info_a:
                    st.caption("출발지")
                    st.write(f"**{origin['name']}**")
                    st.caption(origin["address"])
                with info_b:
                    st.caption("목적지")
                    st.write(f"**{destination['name']}**")
                    st.caption(destination["address"])

                with st.container(border=True):
                    st.markdown("#### 🗺️ 긴급 목적지 노선안내 지도")
                    st.caption(
                        f"{origin['name']} → {destination['name']} · "
                        f"약 {route_km:.1f}km · {route_min:.0f}분"
                    )
                    center_lat = (origin["lat"] + destination["lat"]) / 2
                    center_lng = (origin["lng"] + destination["lng"]) / 2
                    hydrant_map = folium.Map(location=[center_lat, center_lng], zoom_start=14)
                    folium.PolyLine(route_path, color="#a33a3f", weight=5, opacity=0.88).add_to(hydrant_map)
                    folium.Marker(
                        [origin["lat"], origin["lng"]],
                        tooltip=f"출발: {origin['name']}",
                        icon=folium.DivIcon(
                            icon_size=(64, 28), icon_anchor=(32, 14),
                            html=(
                                '<div style="background:#1f6fb2;color:#ffffff;'
                                'padding:4px 9px;border-radius:14px;border:2px solid #ffffff;'
                                'box-shadow:0 1px 5px rgba(0,0,0,.45);text-align:center;'
                                'font-family:sans-serif;font-weight:800;font-size:12px;'
                                'line-height:1.2;white-space:nowrap;">출발</div>'
                            ),
                        ),
                    ).add_to(hydrant_map)
                    folium.Marker(
                        [destination["lat"], destination["lng"]],
                        tooltip=f"목적지: {destination['name']}",
                        icon=folium.DivIcon(
                            icon_size=(72, 28), icon_anchor=(36, 14),
                            html=(
                                '<div style="background:#b91c1c;color:#ffffff;'
                                'padding:4px 9px;border-radius:14px;border:2px solid #ffffff;'
                                'box-shadow:0 1px 5px rgba(0,0,0,.45);text-align:center;'
                                'font-family:sans-serif;font-weight:800;font-size:12px;'
                                'line-height:1.2;white-space:nowrap;">목적지</div>'
                            ),
                        ),
                    ).add_to(hydrant_map)
                    st_folium(hydrant_map, height=260, use_container_width=True, key="hydrant_direct_map")

                st.markdown("**🟨 카카오맵 — 길안내 및 공유용 QR**")
                st.link_button(
                    "🚗 카카오맵으로 긴급 목적지 길안내 열기",
                    guide_url,
                    type="primary",
                    use_container_width=True,
                )
                route_sequence = f"{origin['name']} → {destination['name']}"
                qr_png = make_qr_png(guide_url)
                qr_box = (st.popover("📱 동료 공유용 QR 열기", use_container_width=True)
                          if hasattr(st, "popover")
                          else st.expander("📱 동료 공유용 QR 열기"))
                with qr_box:
                    st.markdown("### 긴급 목적지 노선안내")
                    st.caption(
                        f"이 화면을 캡처해 현장 단톡방에 공유하거나, "
                        f"아래 버튼으로 QR 이미지만 저장해 인쇄물에 넣을 수 있습니다."
                    )
                    st.image(qr_png, caption="휴대폰 카메라로 스캔하면 카카오맵 길안내가 열립니다.", width=220)
                    st.markdown(f"**경로:** {route_sequence}")
                    st.caption(
                        f"계획 기준 출발지: {origin['address']}  \n"
                        f"목적지: {destination['address']}  \n"
                        "※ 카카오맵 실행 후 실제 내비는 휴대폰 현재 위치를 기준으로 "
                        "출발하여 소요시간이 달라질 수 있습니다."
                    )
                    st.download_button(
                        "QR 이미지 파일 다운로드",
                        data=qr_png,
                        file_name="긴급_목적지_노선안내_QR.png",
                        mime="image/png",
                        key="hydrant_direct_qr_download",
                        use_container_width=True,
                    )
                st.caption(f"경로: {route_sequence}")
                st.caption(
                    "※ 지도와 예상거리는 입력한 출발지 기준입니다. 카카오맵 실행 후 실제 내비는 "
                    "사용자의 현재 위치 기준으로 다시 안내될 수 있습니다."
                )
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
              .st-key-open_hydrant_direct_panel button,
              .st-key-load_sample_targets_button button {
                height:4.5rem!important; min-height:4.5rem!important; padding:0!important;
                border-radius:10px!important; font-size:1.02rem!important; font-weight:900!important;
                box-shadow:0 8px 18px rgba(15, 23, 42, 0.12)!important;
              }
              .st-key-open_hydrant_direct_panel button {
                border:3px solid #991b1b!important;
                background:linear-gradient(135deg,#dc2626 0%,#991b1b 100%)!important;
                color:#ffffff!important;
                -webkit-text-fill-color:#ffffff!important;
                box-shadow:0 10px 24px rgba(153,27,27,.28)!important;
              }
              .st-key-open_hydrant_direct_panel button:hover {
                border-color:#7f1d1d!important; background:linear-gradient(135deg,#ef4444 0%,#991b1b 100%)!important;
              }
              .st-key-load_sample_targets_button button {
                border:2px solid #f59e0b!important;
                background:#fff7ed!important;
                color:#9a3412!important;
                -webkit-text-fill-color:#9a3412!important;
              }
              .st-key-load_sample_targets_button button:hover {
                border-color:#d97706!important; background:#ffedd5!important;
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
              .st-key-load_sample_targets {
                border:2px solid #f59e0b!important;
                background:#fff7ed!important;
                border-radius:14px!important;
                padding:0.85rem 0.95rem!important;
                margin:0.6rem 0 1rem 0!important;
                box-shadow:0 10px 24px rgba(245, 158, 11, 0.16)!important;
              }
              .st-key-load_sample_targets label p {
                color:#9a3412!important;
                -webkit-text-fill-color:#9a3412!important;
                font-weight:900!important;
                font-size:1.05rem!important;
              }
              .st-key-load_sample_targets [data-testid="stCheckbox"] > label {
                align-items:flex-start!important;
              }
              .st-key-load_selected_browser_draft button,
              .st-key-delete_selected_browser_draft button {
                height:4.5rem!important; min-height:4.5rem!important; padding:0!important; font-weight:750!important;
              }
              .st-key-load_selected_browser_draft button {
                border:2px solid #f59e0b!important;
                background:#fff3cd!important;
                color:#7c2d12!important;
                -webkit-text-fill-color:#7c2d12!important;
                box-shadow:0 8px 18px rgba(245, 158, 11, 0.16)!important;
              }
              .st-key-load_selected_browser_draft button:hover {
                border-color:#d97706!important;
                background:#fde68a!important;
                color:#7c2d12!important;
                -webkit-text-fill-color:#7c2d12!important;
              }
              .st-key-delete_selected_browser_draft button {
                border:1px solid #cbd5e1!important;
                background:#f8fafc!important;
                color:#334155!important;
                -webkit-text-fill-color:#334155!important;
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
            transfer_notice_title = "💻 PC 자료 넘기기" if IS_MOBILE_DEVICE else "📱 휴대폰 자료 넘기기"
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
                st.markdown("### 💻 PC로 자료 넘기기")
                st.caption("대상목록·출발지·좌표검색 결과를 PC로 넘깁니다.")
            else:
                st.markdown("### 📱 휴대폰으로 자료 넘기기")
                st.caption("대상목록·출발지·좌표검색 결과를 휴대폰으로 넘깁니다.")
            coords_ready_for_transfer = st.session_state.get("coords_df") is not None
            st.caption("좌표 검색 완료 후 사용할 수 있습니다.")
            if st.button(
                (
                    "💻 PC로 자료 넘기기 링크 만들기"
                    if IS_MOBILE_DEVICE
                    else "📲 휴대폰으로 자료 넘기기 QR 만들기"
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
                    transfer_url = f"{app_public_url()}/?transfer={quote(transfer_token, safe='')}"
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
            nm, ad = _cell_text(record.get(name_key, "")), _cell_text(record.get(address_key, ""))
            if not nm and not ad:
                continue
            if not ad:
                rows.append({"대상명": nm, "주소": ad, "위도": None, "경도": None,
                             "상태": "⚠️ 주소 없음", "비고": "주소 칸이 비어 있어 검색하지 않았습니다."})
                continue
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
        pre_addr_idx = find_address_column_index(pre_cols, pre_name_idx, df)
        pre_lat = next((c for c in pre_cols if "위도" in str(c) or str(c).lower() == "lat"), None)
        pre_lng = next((c for c in pre_cols if "경도" in str(c) or str(c).lower() in ("lng", "lon")), None)
        coord_signature = tuple((_cell_text(row[pre_cols[pre_name_idx]]), _cell_text(row[pre_cols[pre_addr_idx]]))
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
    return locals()
