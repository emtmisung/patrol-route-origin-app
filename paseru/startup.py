"""앱 입구: 로그인 · 저장 작업 복원 · 휴대폰 전달 처리 · 사용 안내.

원래 app.py 한 파일에 있던 화면 코드를 그대로 옮기고 render(ctx) 함수로 감쌌다."""
import hashlib
import hmac
import json
from datetime import datetime

import streamlit as st
from streamlit_local_storage import LocalStorage

from paseru.geo import has_keys
from paseru.settings import (
    AUTH_REMEMBER_KEY,
    BROWSER_DRAFT_DAYS,
    BROWSER_DRAFT_KEY_PREFIX,
    BROWSER_DRAFT_LEGACY_KEY,
    BROWSER_DRAFT_MAX_ITEMS,
    app_password,
    app_public_url,
)
from paseru.storage import (
    apply_browser_draft,
    browser_work_key,
    consume_mobile_transfer,
    decode_browser_draft,
    make_auth_remember_token,
    valid_auth_remember_token,
)
from paseru.ui import render_route_execution_panel


def render(ctx):
    browser_storage = LocalStorage(key="paseru_browser_storage")
    browser_storage_items = dict(browser_storage.getAll() or {})

    # 공개 주소를 통한 무단 API 사용을 막기 위한 앱 입구 인증
    if not st.session_state.get("paseru_authenticated", False):
        saved_auth_token = browser_storage_items.get(AUTH_REMEMBER_KEY)
        if valid_auth_remember_token(saved_auth_token):
            st.session_state["paseru_authenticated"] = True
        elif saved_auth_token:
            browser_storage.eraseItem(AUTH_REMEMBER_KEY, key="erase_expired_paseru_auth")

    if not st.session_state.get("paseru_authenticated", False):
        with st.container(border=True):
            st.markdown(
                '<div class="paseru-auth-title">🔐 파세루 오리진 사용자 인증</div>',
                unsafe_allow_html=True,
            )
            st.caption("이 앱은 승인된 업무 담당자만 이용할 수 있습니다.")
            with st.form("paseru_login_form", clear_on_submit=False):
                entered_password = st.text_input("비밀번호", type="password", placeholder="비밀번호를 입력하세요")
                remember_login = st.checkbox(
                    "이 기기에서 다음부터 자동 로그인",
                    value=True,
                    help="개인 휴대폰이나 업무용 PC에서만 켜두세요. 비밀번호가 바뀌면 자동로그인은 해제됩니다.",
                )
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

                ※ 첫 로그인 때 자동로그인을 켜두면 같은 기기에서는 다음 접속부터 비밀번호 입력을 건너뜁니다.
                """
                )
                st.caption("아래 주소 오른쪽의 복사 버튼을 누른 뒤 네이버 앱 주소창에 붙여넣을 수 있습니다.")
                st.code(app_public_url().rstrip("/") + "/", language=None)

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
                if not app_password():
                    st.error("관리자가 Streamlit Secrets에 APP_PASSWORD를 먼저 등록해야 합니다.")
                elif hmac.compare_digest(entered_password, app_password()):
                    st.session_state["paseru_authenticated"] = True
                    if remember_login:
                        browser_storage.setItem(
                            AUTH_REMEMBER_KEY,
                            make_auth_remember_token(),
                            key="save_paseru_auth_remember",
                        )
                        st.success("자동로그인을 이 기기에 저장했습니다. 다음 접속부터 비밀번호 입력을 건너뜁니다.")
                    else:
                        browser_storage.eraseItem(AUTH_REMEMBER_KEY, key="erase_paseru_auth_remember")
                        st.info("자동로그인을 사용하지 않고 이번 접속만 인증합니다.")
                else:
                    st.error("비밀번호가 올바르지 않습니다.")
        if not st.session_state.get("paseru_authenticated", False):
            st.stop()

    # 최근 작업은 서버가 아니라 현재 기기의 브라우저 저장소에만 7일간 보관한다.
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
        if st.session_state.get("route_execution_mode"):
            st.success(
                "✅ PC에서 만든 최종 노선을 이 휴대폰으로 가져왔습니다. "
                "아래의 현장 노선 이어가기에서 카카오맵을 바로 실행하세요."
            )
        else:
            st.success(
                "✅ PC 작업을 이 휴대폰으로 가져왔습니다. "
                "이 휴대폰의 현재 브라우저에 최대 3개 중 하나로 7일간 자동 보관됩니다."
            )

    if st.session_state.pop("browser_draft_deleted_notice", False):
        st.success("✅ 선택한 저장 작업을 이 PC에서 삭제했습니다.")

    if st.session_state.get("route_execution_mode") and st.session_state.get("route_results"):
        render_route_execution_panel(
            st.session_state.get("station"),
            st.session_state.get("route_results"),
            st.session_state.get("meta", {}),
        )

    with st.expander("💡 처음 사용하시나요? 사용 순서와 조건을 설정하는 이유", expanded=False):
        st.markdown(
            """
        **파세루 오리진은 다음 순서로 사용합니다.**

        **접속** — 앱 안내와 개인정보 주의사항을 확인하고 비밀번호로 접속합니다.
        1. **기본정보·대상목록** — 순찰 제목·출발 부서를 입력하고 대상명과 주소만 업로드해 좌표를 확인합니다.
           소화전 1건은 엑셀 없이 **소화전 현장안내**에서 현장주소와 소화전 주소만 입력해 바로 연결할 수 있습니다.
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
                "- **소화전 현장안내:** 화재 현장에서 소화전 1개 위치만 바로 안내할 때 엑셀 없이 카카오맵으로 연결합니다.\n"
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
    return locals()
