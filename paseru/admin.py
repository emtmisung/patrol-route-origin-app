"""관리자 전용 사용량 확인 화면. 일반 앱 비밀번호와 별개인 ADMIN_PASSWORD로 들어간다.

주소 뒤에 ?admin=1 을 붙이면 열리고, 일반 로그인 화면·앱 본문과는 완전히 분리되어 있다.
"""
import hmac

import streamlit as st

from paseru import usage_tracker
from paseru.settings import MONTHLY_API_LIMIT, admin_password


def maybe_render_admin_panel():
    if st.query_params.get("admin") != "1":
        return

    st.markdown("### 🛠️ 파세루 오리진 — 관리자 화면")
    st.caption("API 사용량 확인 전용 화면입니다. 일반 사용자 화면과는 별개의 비밀번호를 씁니다.")

    if not st.session_state.get("paseru_admin_authenticated", False):
        with st.form("paseru_admin_login_form"):
            entered = st.text_input("관리자 비밀번호", type="password")
            submitted = st.form_submit_button("확인", type="primary")
        if submitted:
            if not admin_password():
                st.error("관리자가 Streamlit Secrets에 ADMIN_PASSWORD를 먼저 등록해야 합니다.")
            elif hmac.compare_digest(entered, admin_password()):
                st.session_state["paseru_admin_authenticated"] = True
                st.rerun()
            else:
                st.error("비밀번호가 올바르지 않습니다.")
        st.stop()

    if not usage_tracker.is_configured():
        st.warning(
            "구글시트 연동이 아직 설정되지 않았습니다. "
            "Streamlit Secrets에 GSHEET_SERVICE_ACCOUNT · GSHEET_SPREADSHEET_ID를 등록하면 "
            "이번 달 호출량이 여기에 표시됩니다."
        )
        st.stop()

    if st.button("🔄 새로고침"):
        st.cache_resource.clear()
        st.rerun()

    used, _ = usage_tracker.current_month_usage()
    remaining = max(0, MONTHLY_API_LIMIT - used)
    ratio = min(1.0, used / MONTHLY_API_LIMIT) if MONTHLY_API_LIMIT else 0

    col1, col2, col3 = st.columns(3)
    col1.metric("이번 달 사용량", f"{used:,}건")
    col2.metric("이번 달 상한", f"{MONTHLY_API_LIMIT:,}건")
    col3.metric("남은 호출", f"{remaining:,}건")
    st.progress(ratio, text=f"{ratio*100:.1f}% 사용")
    if used >= MONTHLY_API_LIMIT:
        st.error("이번 달 상한에 도달해 현재 앱에서 새 검색·노선 생성이 막혀 있습니다.")
    elif ratio >= 0.9:
        st.warning("이번 달 상한의 90%를 넘었습니다. 사용량이 몰리는 시기라면 다음 달 초까지 여유를 확인하세요.")

    st.markdown("#### 월별 사용 내역")
    history = usage_tracker.monthly_usage_all()
    if history:
        st.dataframe(
            {"월": [h[0] for h in history], "호출수": [f"{h[1]:,}" for h in history]},
            use_container_width=True, hide_index=True,
        )
    else:
        st.caption("아직 기록된 사용 내역이 없습니다.")

    st.caption(
        "※ 실시간 호출 수는 구글시트에 20건 단위로 모아서 반영되어 최대 19건 정도 늦게 표시될 수 있습니다."
    )
    st.stop()
