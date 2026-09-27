"""2단계 순찰방법 · 세부 조건.

원래 app.py 한 파일에 있던 화면 코드를 그대로 옮기고 render(ctx) 함수로 감쌌다."""
import calendar
import math
from datetime import date
from datetime import datetime
from datetime import time as dtime
from datetime import timedelta

import streamlit as st

from paseru.settings import API_CALL_LIMIT
from paseru.ui import (
    card_title,
    next_tab_button,
    safety_warning,
    sub_label,
)


def render(ctx):
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
                    "노선 기본 목표시간(분)",
                    min_value=10, max_value=240, value=60, step=5,
                    help="직접 숫자를 입력할 수 있습니다. 60분보다 짧은 노선도 편성할 수 있습니다.",
                )
            with hc5:
                hydrant_max_min = st.number_input(
                    "노선 최대 허용시간(분)",
                    min_value=10, max_value=360, value=90, step=5,
                    help="직접 숫자를 입력할 수 있습니다. 기본 목표시간보다 크게 잡으면 여유 노선을 허용합니다.",
                )
            with hc6:
                hydrant_inspection_min = st.number_input(
                    "소화전 1개 조사시간(분)", min_value=0, max_value=60, value=5, step=1,
                    help="직접 숫자를 입력할 수 있습니다.",
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
                f"입력한 목표시간 {int(hydrant_target_min)}분을 기준으로 편성하고, 차량별 노선이 "
                f"{int(hydrant_workdays)}개를 넘으면 최대 허용시간 범위에서 조정하도록 안내합니다."
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
    return locals()
