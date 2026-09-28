"""화면 공통 요소(단계 제목 · 경고 · 단계 이동 버튼 · 공유 메타 주입)."""
import html
import json
import re
from datetime import datetime

import streamlit as st
import streamlit.components.v1 as components

from paseru.exports import kakao_route_links
from paseru.settings import ROOT_DIR, app_public_url
from paseru.styles import PASERU_ICON_FALLBACK_URL


def is_mobile_request():
    """현재 접속 브라우저가 휴대폰·태블릿인지 User-Agent로 구분한다."""
    try:
        headers = st.context.headers
        user_agent = str(
            headers.get("User-Agent", "") or headers.get("user-agent", "")
        ).lower()
    except (AttributeError, RuntimeError):
        user_agent = ""
    return bool(re.search(r"android|iphone|ipad|ipod|mobile|tablet", user_agent))


def render_route_execution_panel(station, route_results, meta=None):
    """휴대폰으로 넘긴 최종 노선을 탭 이동 없이 바로 실행할 수 있게 보여준다."""
    if not station or not route_results:
        return
    title = (meta or {}).get("title") or "현장 노선"
    total_segments = sum(len(kakao_route_links(station, rr.get("legs", []))) for rr in route_results)
    st.markdown(
        """
        <style>
          .paseru-route-handoff {
            margin: .25rem 0 1rem;
            padding: 1rem 1.05rem;
            border: 2px solid #0b2f5f;
            border-left: 9px solid #0b2f5f;
            border-radius: 14px;
            background: linear-gradient(135deg, #eff6ff 0%, #ffffff 100%);
            box-shadow: 0 10px 26px rgba(11, 47, 95, .14);
          }
          .paseru-route-handoff-title {
            color:#0b2f5f;
            font-size:1.18rem;
            font-weight:900;
            line-height:1.35;
          }
          .paseru-route-handoff-sub {
            margin-top:.25rem;
            color:#344054;
            font-size:.94rem;
            font-weight:700;
            line-height:1.45;
            word-break:keep-all;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="paseru-route-handoff">'
        '<div class="paseru-route-handoff-title">📱 현장 노선 이어가기</div>'
        f'<div class="paseru-route-handoff-sub">{html.escape(str(title))}<br>'
        f'PC에서 생성한 노선을 그대로 불러왔습니다. 아래 버튼을 순서대로 눌러 카카오맵 안내를 이어가세요. '
        f'총 {int(total_segments)}개 실행 구간입니다.</div></div>',
        unsafe_allow_html=True,
    )
    for rr in route_results:
        route_links = kakao_route_links(station, rr.get("legs", []))
        if not route_links:
            continue
        st.markdown(f"**노선 {rr.get('route_no', '')}**")
        for link_no, (kurl, origin, destinations) in enumerate(route_links, start=1):
            suffix = "" if len(route_links) == 1 else f"-{link_no}"
            sequence = " → ".join([str(origin.get("name", "출발"))] + [str(p.get("name", "")) for p in destinations])
            st.link_button(
                f"🚗 노선 {rr.get('route_no', '')}{suffix} 카카오맵 열기",
                kurl,
                use_container_width=True,
            )
            st.caption(sequence)


def card_title(step, text):
    st.markdown(
        f'<div class="paseru-card-title"><span class="paseru-step">{step}</span>{text}</div>',
        unsafe_allow_html=True,
    )


def sub_label(text):
    st.markdown(f'<div class="paseru-sub">{text}</div>', unsafe_allow_html=True)


def route_generation_spinner(slot, title="노선 생성 중입니다", detail="실제 도로 기준 거리와 경유 순서를 계산하고 있습니다. 화면을 닫지 마세요."):
    slot.markdown(
        f"""
        <style>
          @keyframes paseru-route-search-spin {{
            0% {{ transform: rotate(-18deg) scale(1); }}
            45% {{ transform: rotate(18deg) scale(1.08); }}
            100% {{ transform: rotate(342deg) scale(1); }}
          }}
          @keyframes paseru-route-card-pulse {{
            0%, 100% {{ box-shadow: 0 12px 28px rgba(163, 58, 63, .18); }}
            50% {{ box-shadow: 0 16px 34px rgba(163, 58, 63, .30); }}
          }}
          .paseru-route-running {{
            display:flex;
            align-items:center;
            gap:16px;
            margin: 0.45rem 0 1rem;
            padding: 1rem 1.1rem;
            border: 2px solid #ef8b36;
            border-left: 9px solid #c74732;
            border-radius: 14px;
            background: linear-gradient(135deg, #fff7ed 0%, #fff1f2 100%);
            animation: paseru-route-card-pulse 1.25s ease-in-out infinite;
          }}
          .paseru-route-running .route-lens {{
            flex: 0 0 52px;
            width: 52px;
            height: 52px;
            border-radius: 50%;
            display:flex;
            align-items:center;
            justify-content:center;
            background:#ffffff;
            border: 3px solid #f97316;
            font-size: 2rem;
            animation: paseru-route-search-spin .9s linear infinite;
          }}
          .paseru-route-running .route-title {{
            color:#8a2d14;
            font-size:1.12rem;
            font-weight:900;
            line-height:1.35;
          }}
          .paseru-route-running .route-detail {{
            margin-top:.18rem;
            color:#344054;
            font-size:.95rem;
            font-weight:700;
            line-height:1.45;
            word-break:keep-all;
          }}
        </style>
        <div class="paseru-route-running" role="status" aria-live="polite">
          <div class="route-lens">🔎</div>
          <div>
            <div class="route-title">{html.escape(title)}</div>
            <div class="route-detail">{html.escape(detail)}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def safety_warning(text, title="주의 · 활용 범위 안내"):
    """재난대응 활용범위를 일반 안내와 구분해 보여주는 전용 경고 상자."""
    st.markdown(
        '<div class="paseru-safety-warning">'
        '<div class="warning-icon" aria-hidden="true">⚠</div>'
        f'<div class="warning-body"><span class="warning-title">{html.escape(title)}</span>'
        f'{html.escape(text)}</div></div>',
        unsafe_allow_html=True,
    )


def inspection_capacity_warning(capacity_formula, total_targets, omitted_targets):
    """예방검사 처리용량 부족을 성공 안내와 확실히 구분해 표시한다."""
    st.markdown(
        '<div class="paseru-safety-warning paseru-capacity-warning">'
        '<div class="warning-icon" aria-hidden="true">⚠</div>'
        '<div class="warning-body"><span class="warning-title">검사기간 내 미완료 예상</span>'
        f'{html.escape(capacity_formula)}<br>'
        f'전체 {int(total_targets)}개소 중 '
        f'<span class="warning-count">{int(omitted_targets)}개소 누락 예정</span>입니다.<br>'
        '검사 대상 수 또는 검사일수를 조정하세요.</div></div>',
        unsafe_allow_html=True,
    )



SOCIAL_PREVIEW_TITLE = "파세루 오리진 (FireSafe Route Origin)"
SOCIAL_PREVIEW_DESCRIPTION = "방문·점검·순찰 주소 목록을 올리면 내 핸드폰 카카오맵으로 온다"
SOCIAL_PREVIEW_IMAGE = "https://raw.githubusercontent.com/emtmisung/patrol-route-origin-app/main/assets/social-preview.jpg"


def inject_social_preview_meta():
    """카카오톡 등 링크 공유 미리보기에 쓰일 제목·설명·이미지를 부모 문서에 주입한다."""
    title = json.dumps(SOCIAL_PREVIEW_TITLE, ensure_ascii=False)
    description = json.dumps(SOCIAL_PREVIEW_DESCRIPTION, ensure_ascii=False)
    image_url = json.dumps(SOCIAL_PREVIEW_IMAGE)
    app_url = json.dumps(app_public_url() + "/")
    components.html(
        f"""
<script>
try {{
  const d = window.parent.document;
  const setMeta = (key, value, attr = 'property') => {{
    let el = d.head.querySelector('meta[' + attr + '="' + key + '"]');
    if (!el) {{
      el = d.createElement('meta');
      el.setAttribute(attr, key);
      d.head.appendChild(el);
    }}
    el.setAttribute('content', value);
  }};

  d.title = {title};
  setMeta('description', {description}, 'name');
  setMeta('og:type', 'website');
  setMeta('og:site_name', '파세루 오리진');
  setMeta('og:title', {title});
  setMeta('og:description', {description});
  setMeta('og:url', {app_url});
  setMeta('og:image', {image_url});
  setMeta('og:image:secure_url', {image_url});
  setMeta('og:image:type', 'image/jpeg');
  setMeta('og:image:width', '1536');
  setMeta('og:image:height', '774');
  setMeta('twitter:card', 'summary_large_image', 'name');
  setMeta('twitter:title', {title}, 'name');
  setMeta('twitter:description', {description}, 'name');
  setMeta('twitter:image', {image_url}, 'name');
}} catch (e) {{ /* 공유 메타 주입 실패 시 앱 실행에는 영향 없음 */ }}
</script>
""",
        height=0,
    )
PWA_ICON_URL = "https://raw.githubusercontent.com/emtmisung/patrol-route-origin-app/main/assets/paseru-icon.png"
PWA_MANIFEST_URL = "https://raw.githubusercontent.com/emtmisung/patrol-route-origin-app/main/assets/site.webmanifest"


def inject_pwa_links():
    """PWA: 홈 화면에 앱처럼 추가할 수 있도록 매니페스트·아이콘을 부모 문서에 주입(가능한 환경에서)."""
    components.html(
        f"""
<script>
try {{
  const d = window.parent.document;
  if (d) {{
    const iconUrl = "{PWA_ICON_URL}";
    const manifestUrl = "{PWA_MANIFEST_URL}";
    d.querySelectorAll('link[rel~="manifest"], link[rel~="icon"], link[rel="apple-touch-icon"], link[rel="apple-touch-icon-precomposed"]').forEach((el) => el.remove());

    const link = d.createElement('link');
    link.id = 'paseru-manifest';
    link.rel = 'manifest';
    link.href = manifestUrl;
    d.head.prepend(link);

    const favicon = d.createElement('link');
    favicon.id = 'paseru-favicon';
    favicon.rel = 'icon';
    favicon.type = 'image/png';
    favicon.sizes = '512x512';
    favicon.href = iconUrl;
    d.head.prepend(favicon);

    const shortcutIcon = d.createElement('link');
    shortcutIcon.id = 'paseru-shortcut-icon';
    shortcutIcon.rel = 'shortcut icon';
    shortcutIcon.type = 'image/png';
    shortcutIcon.href = iconUrl;
    d.head.prepend(shortcutIcon);

    const appleIcon = d.createElement('link');
    appleIcon.id = 'paseru-apple-touch-icon';
    appleIcon.rel = 'apple-touch-icon';
    appleIcon.sizes = '512x512';
    appleIcon.href = iconUrl;
    d.head.prepend(appleIcon);

    if (!d.querySelector('meta[name="apple-mobile-web-app-capable"]')) {{
      const meta = d.createElement('meta');
      meta.name = 'apple-mobile-web-app-capable'; meta.content = 'yes';
      d.head.appendChild(meta);
    }}
    let theme = d.querySelector('meta[name="theme-color"]');
    if (!theme) {{
      theme = d.createElement('meta');
      theme.name = 'theme-color';
      d.head.appendChild(theme);
    }}
    theme.content = '#0b2f5f';
  }}
}} catch (e) {{ /* 환경상 주입이 막히면 조용히 무시 */ }}
</script>
""",
        height=0,
    )


def render_login_hero():
    """접속 화면 표지(마스코트·4단계 안내)와 공통 로그인 화면 스타일."""
    st.markdown(
        """
    <style>
      .paseru-login-hero {
        margin: 0.15rem 0 1.25rem;
        padding: 1.05rem 1.05rem 1.15rem;
        border: 1px solid #d4dbe5;
        border-radius: 14px;
        background:
          radial-gradient(circle at 88% 18%, rgba(239, 78, 78, .13), transparent 30%),
          linear-gradient(135deg, #ffffff 0%, #f8fafc 100%);
        box-shadow: 0 5px 18px rgba(23, 38, 58, .07);
        overflow: hidden;
        position: relative;
      }
      .paseru-brand-row {
        display: flex;
        align-items: flex-start;
        gap: .82rem;
        margin-top: .45rem;
      }
      .paseru-brand-copy {
        min-width: 0;
        flex: 1 1 auto;
      }
      .paseru-mascot-icon {
        flex: 0 0 auto;
        width: clamp(86px, 16vw, 112px);
        height: clamp(86px, 16vw, 112px);
        margin-top: 0;
        padding: 0;
        border-radius: 22px;
        object-fit: contain;
        object-position: center;
        background: #ffffff;
        box-shadow: 0 8px 22px rgba(11, 47, 95, .17);
        image-rendering: auto;
      }
      .paseru-login-kicker {
        color: #a33a3f;
        font-size: clamp(.94rem, 3.5vw, 1.15rem);
        font-weight: 750;
        line-height: 1.45;
        word-break: keep-all;
      }
      .paseru-login-title {
        margin-top: .28rem;
        color: #17263a;
        font-family: 'Noto Serif KR', serif;
        font-size: clamp(2.05rem, 9vw, 3.05rem);
        font-weight: 750;
        line-height: 1.05;
        letter-spacing: -0.035em;
      }
      .paseru-login-copy {
        margin-top: .95rem;
        color: #17263a;
        font-size: clamp(.98rem, 2vw, 1.18rem);
        font-weight: 760;
        line-height: 1.6;
        word-break: keep-all;
        overflow-wrap: normal;
        text-align: left;
        max-width: 100%;
        white-space: nowrap;
      }
      .paseru-login-copy > div {
        display: inline;
        white-space: nowrap;
      }
      .paseru-login-copy > div + div::before { content: " "; }
      .paseru-login-copy .nowrap { white-space: nowrap; }
      .paseru-flow-card {
        margin-top: 1.05rem;
        padding: .95rem .75rem .85rem;
        border-radius: 13px;
        border: 1px solid #e2e8f0;
        background: rgba(255,255,255,.86);
        box-shadow: inset 0 1px 0 rgba(255,255,255,.75);
      }
      .paseru-flow-steps {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        align-items: start;
        gap: .35rem;
        position: relative;
      }
      .paseru-flow-step {
        position: relative;
        z-index: 1;
        display: grid;
        justify-items: center;
        gap: .42rem;
        min-width: 0;
      }
      .paseru-flow-step:not(:last-child)::after {
        content: "";
        position: absolute;
        top: 28px;
        left: calc(50% + 32px);
        width: calc(100% - 28px);
        border-top: 5px dotted #df6c70;
        opacity: .72;
      }
      .paseru-flow-icon {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 58px;
        height: 58px;
        border-radius: 50%;
        background: #ffffff;
        border: 3px solid #ffffff;
        box-shadow: 0 6px 16px rgba(23,38,58,.15);
        font-size: 1.85rem;
      }
      .paseru-flow-step.file .paseru-flow-icon { background: #fff7ed; }
      .paseru-flow-step.route .paseru-flow-icon { background: #eff6ff; }
      .paseru-flow-step.phone .paseru-flow-icon { background: #f0fdf4; }
      .paseru-flow-step.go .paseru-flow-icon { background: #fff1f2; }
      .paseru-flow-label {
        color: #17263a;
        font-size: .88rem;
        font-weight: 800;
        line-height: 1.25;
        text-align: center;
        word-break: keep-all;
      }
      @media (max-width: 560px) {
        .paseru-login-hero { padding: .95rem .95rem 1.05rem; }
        .paseru-brand-row {
          align-items: flex-start;
          gap: .52rem;
          margin-top: .35rem;
        }
        .paseru-mascot-icon {
          width: 82px;
          height: 82px;
          margin-top: 0;
          border-radius: 17px;
        }
        .paseru-login-kicker {
          font-size: .92rem;
          line-height: 1.35;
          text-align: left;
        }
        .paseru-login-title {
          font-size: clamp(1.88rem, 8.5vw, 2.68rem);
        }
        .paseru-login-copy {
          text-align: center;
          white-space: normal;
        }
        .paseru-login-copy > div {
          display: block;
          white-space: nowrap;
        }
        .paseru-login-copy > div + div::before { content: ""; }
        .paseru-flow-card { padding: .78rem .45rem .72rem; }
        .paseru-flow-steps { gap: .15rem; }
        .paseru-flow-step:not(:last-child)::after {
          top: 24px;
          left: calc(50% + 25px);
          width: calc(100% - 18px);
          border-top-width: 4px;
        }
        .paseru-flow-icon {
          width: 48px;
          height: 48px;
          font-size: 1.52rem;
        }
        .paseru-flow-label { font-size: .75rem; }
      }
    </style>
    <div class="paseru-login-hero">
      <div class="paseru-login-kicker">소방 현장 노선 편성 자동화의 시작</div>
      <div class="paseru-brand-row">
        <img class="paseru-mascot-icon" src="__PASERU_ICON_URL__" alt="파세루 캐릭터" onerror="this.onerror=null;this.src='__PASERU_ICON_FALLBACK_URL__';">
        <div class="paseru-brand-copy">
          <div class="paseru-login-title">FireSafe Route Origin</div>
          <div class="paseru-login-copy">
            <div>방문·점검·순찰 주소 목록을 올리면</div>
            <div>내 핸드폰 카카오맵으로 온다</div>
          </div>
        </div>
      </div>
      <div class="paseru-flow-card" aria-label="파세루 이용 흐름">
        <div class="paseru-flow-steps">
          <div class="paseru-flow-step file">
            <div class="paseru-flow-icon">📄</div>
            <div class="paseru-flow-label">주소목록<br>파일</div>
          </div>
          <div class="paseru-flow-step route">
            <div class="paseru-flow-icon">📍</div>
            <div class="paseru-flow-label">자동<br>노선</div>
          </div>
          <div class="paseru-flow-step phone">
            <div class="paseru-flow-icon">📱</div>
            <div class="paseru-flow-label">핸드폰<br>전송</div>
          </div>
          <div class="paseru-flow-step go">
            <div class="paseru-flow-icon">🚒</div>
            <div class="paseru-flow-label">카카오맵<br>출발</div>
          </div>
        </div>
      </div>
    </div>
    """.replace("__PASERU_ICON_URL__", PWA_ICON_URL)
            .replace("__PASERU_ICON_FALLBACK_URL__", PASERU_ICON_FALLBACK_URL),
        unsafe_allow_html=True,
    )


# 휴대폰 GPS 현재위치 컴포넌트
geolocation_component = components.declare_component(
    "paseru_geolocation",
    path=str(ROOT_DIR / "geolocation_component"),
)


# ----------------------------------------------------------------------------
# 단계(탭) 이동 — 자바스크립트 없이 Streamlit 상태만으로 처리
# ----------------------------------------------------------------------------
# 예전에는 버튼 안의 자바스크립트가 부모 화면에서 탭 글자를 찾아 대신 눌렀다.
# 브라우저·보안정책에 따라 실패할 수 있어서, 이제는 st.tabs의 key 상태를 직접 바꾼다.
STEP_LABELS = ["1단계 기본정보", "2단계 순찰방법", "3단계 노선 생성 · 결과"]
STEP_TABS_KEY = "paseru_step_tab"
_SCROLL_FLAG = "_paseru_scroll_to_steps"


def _go_to_step(target_index):
    st.session_state[STEP_TABS_KEY] = STEP_LABELS[target_index]
    st.session_state[_SCROLL_FLAG] = True


def next_tab_button(label, target_index, enabled=True):
    """다음 단계 이동 버튼. 입력 전에는 회색, 완료 후에는 초록색으로 표시한다."""
    key = f"paseru_next_step_{target_index}"
    st.markdown(
        f"""
        <style>
          .st-key-{key} {{
            width:33.333%!important; min-width:240px!important;
          }}
          .st-key-{key} button {{
            width:100%!important; min-height:3.4rem!important;
            padding:8px 10px 8px 22px!important; border-radius:999px!important;
            display:flex!important; align-items:center!important; justify-content:space-between!important;
            gap:12px!important;
            transition:transform .16s ease,box-shadow .16s ease,filter .16s ease;
          }}
          .st-key-{key} button p {{
            font-size:18px!important; font-weight:800!important; margin:0!important;
          }}
          .st-key-{key} button::after {{
            content:"→"; width:34px; height:34px; flex:0 0 34px; border-radius:50%;
            display:flex; align-items:center; justify-content:center;
            background:#fff; font-size:20px; font-weight:900;
          }}
          .st-key-{key} button:not(:disabled) {{
            border:2px solid #207447!important; color:#fff!important;
            -webkit-text-fill-color:#fff!important;
            background:linear-gradient(135deg,#238553,#17663e)!important;
            box-shadow:0 8px 20px -9px rgba(23,102,62,.8)!important;
          }}
          .st-key-{key} button:not(:disabled)::after {{
            color:#17663e; -webkit-text-fill-color:#17663e;
          }}
          .st-key-{key} button:not(:disabled):hover {{
            transform:translateY(-2px); filter:brightness(1.06);
            box-shadow:0 12px 24px -10px rgba(23,102,62,.9)!important;
          }}
          .st-key-{key} button:disabled {{
            border:2px solid #aeb6c0!important; background:#aeb6c0!important;
            color:#f8fafc!important; -webkit-text-fill-color:#f8fafc!important;
            opacity:1!important; cursor:not-allowed!important; box-shadow:none!important;
          }}
          .st-key-{key} button:disabled::after {{
            color:#7b8490; -webkit-text-fill-color:#7b8490;
          }}
          @media (max-width: 640px) {{
            .st-key-{key} {{ width:100%!important; min-width:0!important; }}
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.button(
        label,
        key=key,
        disabled=not enabled,
        on_click=_go_to_step,
        args=(target_index,),
        help="입력 완료 · 다음 단계로 이동" if enabled else "필수 입력을 완료하면 이동할 수 있습니다",
    )


def scroll_to_steps_if_requested():
    """다음 단계 버튼을 누른 직후 한 번만 화면을 단계 탭 위치로 올린다.

    화면 이동(스크롤)만 담당하므로 실패해도 단계 전환 자체에는 영향이 없다."""
    if not st.session_state.pop(_SCROLL_FLAG, False):
        return
    components.html(
        f"""
<script>
// {datetime.now().timestamp()}
try {{
  const d = window.parent.document;
  const tabs = d.querySelector('[data-testid="stTabs"]');
  if (tabs) {{ tabs.scrollIntoView({{behavior:'smooth', block:'start'}}); }}
  else {{ window.parent.scrollTo({{top:0, behavior:'smooth'}}); }}
}} catch (e) {{ /* 스크롤만 실패, 단계 전환에는 영향 없음 */ }}
</script>
""",
        height=0,
    )
