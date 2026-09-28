"""파세루 오리진 (FireSafe Route Origin) — 실동 앱 진입점.

화면 순서만 정하고, 실제 내용은 paseru/ 폴더의 모듈이 담당한다.
  - paseru/startup.py       앱 입구(로그인 · 저장 작업 복원 · 휴대폰 전달)
  - paseru/step1_basic.py   1단계 기본정보 · 대상목록 · 좌표 확인
  - paseru/step2_method.py  2단계 순찰방법 · 세부 조건
  - paseru/step3_build.py   3단계 노선 생성 · 결과
"""
import streamlit as st
from PIL import Image

from paseru import startup, step1_basic, step2_method, step3_build
from paseru.admin import maybe_render_admin_panel
from paseru.settings import ROOT_DIR
from paseru.styles import PASERU_CSS
from paseru.ui import (
    STEP_LABELS,
    STEP_TABS_KEY,
    inject_pwa_links,
    inject_social_preview_meta,
    is_mobile_request,
    render_login_hero,
    scroll_to_steps_if_requested,
)

APP_ICON_PATH = ROOT_DIR / "assets" / "paseru-icon.png"
try:
    APP_ICON = Image.open(APP_ICON_PATH)
except Exception:
    APP_ICON = "🚒"

st.set_page_config(
    page_title="파세루 오리진",
    page_icon=APP_ICON,
    layout="wide",
)

st.markdown(PASERU_CSS, unsafe_allow_html=True)
maybe_render_admin_panel()   # ?admin=1 이면 여기서 관리자 화면만 보여주고 멈춘다(st.stop)
inject_social_preview_meta()
inject_pwa_links()
render_login_hero()

# 화면 사이에 넘겨줄 값(대상목록·출발지·조건 등)을 담는 상자
ctx = {"IS_MOBILE_DEVICE": is_mobile_request()}
ctx.update(startup.render(ctx))       # 로그인 전에는 여기서 멈춘다(st.stop)

scroll_to_steps_if_requested()

# 단계 탭: key로 현재 단계를 기억하고, '다음 단계' 버튼이 이 값을 바꿔 이동한다.
page_basic, page_details, page_build = st.tabs(
    STEP_LABELS, key=STEP_TABS_KEY, on_change="rerun",
)

with page_basic:
    ctx.update(step1_basic.render(ctx))

with page_details:
    ctx.update(step2_method.render(ctx))   # 순찰방법 미선택 시 여기서 멈춘다(st.stop)

with page_build:
    step3_build.render(ctx)
