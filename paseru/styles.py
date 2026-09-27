"""화면 공통 CSS와 아이콘 그림."""
from urllib.parse import quote


# ----------------------------------------------------------------------------
# UI — 파세루 데모(웹 프로토타입)와 같은 카드+칩 스타일
# ----------------------------------------------------------------------------
PASERU_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700;800&family=Noto+Serif+KR:wght@600;700&family=IBM+Plex+Mono:wght@500;700&display=swap');

:root{
  --accent:#a33a3f; --accent-button:#b8464b; --accent-hover:#8f3035; --accent-soft:#f7e9ea;
  --navy:#17263a; --ink:#263442; --muted:#344054;
  --line:#cbd3dd; --divider:#d8dee7; --surface:#ffffff; --bg:#f7f8fa;
  --focus:#d8898d;
  color-scheme: light;   /* 휴대폰 다크모드에서도 밝은 화면으로 고정 */
}
.stApp{ background:var(--bg); color:var(--ink); font-size:14px; line-height:1.55; }

/* 휴대폰 다크모드에서 '흰 배경 + 흰 글씨'가 되는 문제를 막기 위해
   본문 글자색을 어두운 색으로 명시적으로 고정한다. */
.stApp, .stApp p, .stApp span, .stApp label, .stApp li, .stApp div,
.stMarkdown, .stMarkdown *, [data-testid="stWidgetLabel"] *,
[data-testid="stMetricLabel"] *, [data-testid="stMetricValue"],
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *{
  color:var(--ink);
}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *,
[data-testid="stMetricLabel"] *{ color: var(--muted) !important; }

/* 알림 박스(노란색·파란색 등) 안 글씨도 항상 검정 계열로 */
div[data-testid="stAlert"], div[data-testid="stAlert"] *,
div[data-testid="stNotification"], div[data-testid="stNotification"] *{
  color:var(--ink) !important;
}

/* 입력창·표를 밝은 배경 + 어두운 글씨로 고정 */
input, textarea, select,
[data-baseweb="input"] input, [data-baseweb="base-input"] input,
[data-baseweb="select"] div{
  background-color:#ffffff !important; color:var(--ink) !important;
  border-color:#aab4c1 !important;
}
[data-testid="stDataFrame"], [data-testid="stDataEditor"],
[data-testid="stTable"]{ background:#ffffff !important; }

html, body, [class*="css"], .stMarkdown, .stTextInput, .stNumberInput{
  font-family:'Noto Sans KR', -apple-system, 'Malgun Gothic', sans-serif;
}
h1, h2, h3, h4, h5, h6{
  font-family:'Noto Serif KR', serif !important; color:var(--navy) !important; font-weight:700 !important;
}
h1{ font-size:32px !important; }
h2{ font-size:24px !important; }
h3{ font-size:20px !important; }
.block-container{ padding-top: 2.2rem; max-width: 1180px; }

/* ---- 카드 컨테이너(border=True) ---- */
div[data-testid="stVerticalBlockBorderWrapper"]{
  background: var(--surface);
  border-radius: 14px !important;
  border:1px solid var(--line) !important;
  border-left:4px solid var(--accent) !important;
  box-shadow:0 2px 10px rgba(23,38,58,.06);
  padding:12px 18px 16px;
  margin-bottom:8px;
}

/* ---- 카드 제목 + 번호 뱃지 ---- */
.paseru-card-title{
  display:flex; align-items:center; gap:9px;
  font-family:'Noto Serif KR', serif; font-size:18px; font-weight:700; color:var(--navy);
  margin: 2px 0 10px;
}
.paseru-step{
  display:inline-flex; align-items:center; justify-content:center;
  width:23px; height:23px; border-radius:50%;
  background:var(--accent); color:#fff !important;
  font-family:'IBM Plex Mono', monospace; font-size:12px; font-weight:700; flex:none;
}
.paseru-sub{ font-weight:650; font-size:14px; color:#22324a; margin:14px 0 6px; }
.paseru-eyebrow{
  font-family:'IBM Plex Mono', monospace; font-size:12px; letter-spacing:.08em;
  text-transform:uppercase; color:var(--accent); font-weight:700; margin-bottom:2px;
}

/* ---- 선택 칩: 미선택도 선명한 테두리, 선택 시 차분한 딥 레드 ---- */
button[data-variant="pills"]{
  border-radius: 999px !important;
  border: 1px solid var(--line) !important;
  background:#f4f6f8 !important;
  color: var(--ink) !important;
  border-color:#aab4c1 !important;
  font-weight:600 !important;
  padding: 0.42em 1.05em !important;
  transition: background .12s, border-color .12s, color .12s;
}
button[data-variant="pills"]:hover{ background:#f7e9ea !important; border-color:var(--accent) !important; }
button[data-variant="pills"][data-selected="true"],
button[data-variant="pills"][aria-checked="true"],
button[data-variant="pills"][aria-pressed="true"]{
  background: var(--accent) !important;
  border-color: var(--accent) !important;
  color: #ffffff !important;
  font-weight: 700 !important;
  box-shadow:0 4px 12px -7px rgba(163,58,63,.42);
}
button[data-variant="pills"][data-selected="true"] *,
button[data-variant="pills"][aria-checked="true"] *,
button[data-variant="pills"][aria-pressed="true"] *{ color:#ffffff !important; }
div[data-testid="stButtonGroup"]{ gap: 8px !important; }

/* 인증 카드 제목은 모바일에서도 한 줄로 유지 */
.paseru-auth-title{
  margin:0 0 .55rem;
  color:var(--navy) !important;
  font-family:'Noto Serif KR', serif !important;
  font-size:clamp(1.16rem, 5.2vw, 1.9rem);
  font-weight:700;
  line-height:1.25;
  letter-spacing:-0.12em;
  white-space:nowrap;
}

/* ---- 버튼 ---- */
div.stButton > button, .stDownloadButton > button, div.stFormSubmitter > button{
  background-color:var(--accent-button) !important;
  color:#fff !important; border:none !important; border-radius:10px !important;
  font-weight:700 !important; padding:0.98em 1.1em !important;
  box-shadow:0 5px 14px -8px rgba(143,48,53,.48);
}
div.stButton > button:hover, .stDownloadButton > button:hover{ background-color: var(--accent-hover) !important; }
/* 폼 제출 전에도 입력 여부에 따라 즉시 반응하며 hover에서도 색을 유지한다. */
[data-testid="stForm"]:has(input[placeholder="비밀번호를 입력하세요"]) button[kind],
[data-testid="stForm"]:has(input[placeholder="비밀번호를 입력하세요"]) button[kind]:is(:hover, :focus, :active){
  background:#b8464b !important;
  border-color:#b8464b !important;
}
[data-testid="stForm"]:has(input[placeholder="비밀번호를 입력하세요"]:not(:placeholder-shown)) button[kind],
[data-testid="stForm"]:has(input[placeholder="비밀번호를 입력하세요"]:not(:placeholder-shown)) button[kind]:is(:hover, :focus, :active){
  background:#238553 !important;
  border-color:#238553 !important;
}
[data-testid="stForm"]:has(input[placeholder="비밀번호를 입력하세요"]) button[kind],
[data-testid="stForm"]:has(input[placeholder="비밀번호를 입력하세요"]) button[kind] *{
  color:#ffffff !important;
  -webkit-text-fill-color:#ffffff !important;
}
/* 전역 글자색 규칙보다 우선해 주요 빨간 버튼의 글자를 항상 흰색으로 표시 */
div.stButton > button:not([kind="secondary"]),
div.stButton > button:not([kind="secondary"]) *,
.stDownloadButton > button, .stDownloadButton > button *,
div.stFormSubmitter > button, div.stFormSubmitter > button *{
  color:#ffffff !important;
  -webkit-text-fill-color:#ffffff !important;
  opacity:1 !important;
}
div.stButton > button[kind="secondary"]{
  background:#ffffff !important; color:var(--ink) !important;
  border:1px solid #aab4c1 !important; box-shadow:none !important;
}
button:focus-visible, input:focus-visible, textarea:focus-visible,
[tabindex]:focus-visible{ outline:2px solid var(--focus) !important; outline-offset:2px; }

/* ---- 결과 영역 ---- */
div[data-testid="stExpander"]{
  border:1px solid var(--line) !important; border-radius:12px !important;
  background:var(--surface); overflow:hidden;
  box-shadow:0 1px 6px rgba(23,38,58,.04);
}
/* 긴 화면에서도 단계 탭을 잃지 않도록 상단에 고정 */
div[data-testid="stTabs"] [data-baseweb="tab-list"]{
  position:sticky; top:.25rem; z-index:50;
  background:rgba(255,255,255,.96); backdrop-filter:blur(8px);
  padding-top:.25rem;
}

/* ---- 대비 강화: 지표·표·라벨이 흐리게 보이지 않도록 ---- */
div[data-testid="stMetricValue"]{
  font-family:'IBM Plex Mono', monospace;
  color:#315d78 !important; font-weight:700 !important;
}
div[data-testid="stMetricLabel"], div[data-testid="stMetricLabel"] *{
  color:var(--ink) !important; font-weight:600 !important;
}
[data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label{
  color:var(--ink) !important; font-weight:600 !important;
}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *{
  color:var(--muted) !important;
  -webkit-text-fill-color:var(--muted) !important;
  opacity:1 !important;
  font-weight:500 !important;
}
/* 안내문·업로더 보조문이 연한 회색으로 흐려지지 않도록 대비 확보 */
.stApp small, .stApp small *,
[data-testid="stFileUploader"] small,
[data-testid="stFileUploader"] small *,
[data-testid="stFileUploaderDropzoneInstructions"],
[data-testid="stFileUploaderDropzoneInstructions"] *{
  color:var(--muted) !important;
  -webkit-text-fill-color:var(--muted) !important;
  opacity:1 !important;
}
/* 표(데이터프레임·편집표) 글씨와 테두리를 진하게 */
[data-testid="stDataFrame"] *, [data-testid="stDataEditor"] *{
  color:var(--ink) !important;
}
[data-testid="stDataFrame"], [data-testid="stDataEditor"]{
  border:1px solid var(--line) !important; border-radius:8px;
}
[data-testid="stDataFrame"] [role="columnheader"],
[data-testid="stDataEditor"] [role="columnheader"]{
  background:#eef2f6 !important; color:var(--navy) !important; font-weight:700 !important;
}
/* 알림 박스에 색 띠를 넣어 눈에 잘 띄게 */
div[data-testid="stAlert"]{
  border:1px solid #a9bfce !important;
  border-left:4px solid #557f99 !important; border-radius:10px !important;
  background:#eef4f8 !important;
}
/* 재난대응 활용범위 경고: 일반 안내와 혼동되지 않도록 전용 주황색 사용 */
.paseru-safety-warning{
  display:flex; align-items:flex-start; gap:14px;
  margin:.65rem 0 .8rem; padding:15px 17px;
  border:1.5px solid #e28713; border-left:7px solid #d96f00;
  border-radius:11px; background:#fff1d6;
  box-shadow:0 3px 10px rgba(217,111,0,.12);
  color:#4a2b00 !important;
}
.paseru-safety-warning .warning-icon{
  flex:none; font-size:34px; line-height:1; color:#d96f00 !important;
  margin-top:1px;
}
.paseru-safety-warning .warning-body,
.paseru-safety-warning .warning-body *{ color:#4a2b00 !important; }
.paseru-safety-warning .warning-title{
  display:block; margin-bottom:3px; font-size:16px; font-weight:800;
  color:#9a4700 !important;
}
.paseru-capacity-warning{
  border:2px solid #e05a16; border-left:9px solid #c93f12;
  background:#fff0dc;
  box-shadow:0 4px 12px rgba(201,63,18,.18);
}
.paseru-capacity-warning .warning-icon{
  color:#c93f12 !important; font-size:38px;
}
.paseru-capacity-warning .warning-title{
  color:#a52d0b !important; font-size:17px;
}
.paseru-capacity-warning .warning-count{
  color:#a52d0b !important; font-size:18px; font-weight:900;
}
.paseru-sub{ color:#22324a !important; }

/* 완료된 핵심 작업은 기존 실행 버튼 자리에 초록색 상태 버튼처럼 표시 */
.paseru-complete-action{
  width:100%; padding:.72rem 1rem; border-radius:10px;
  background:#2f6b49; border:1px solid #24563b;
  color:#ffffff !important; text-align:center; font-weight:750;
  box-shadow:0 4px 12px -8px rgba(36,86,59,.55);
}
.paseru-complete-action *{ color:#ffffff !important; }

/* ---- 내비게이션 버튼: 링크 기본색(파랑)에 밀리지 않도록 클래스로 고정 ---- */
a.paseru-navbtn, a.paseru-navbtn:link, a.paseru-navbtn:visited,
a.paseru-navbtn:hover, a.paseru-navbtn:active,
a.paseru-navbtn *{
  color:#ffffff !important;
  text-decoration:none !important;
}
a.paseru-navbtn{
  display:inline-block; padding:12px 16px; border-radius:10px;
  font-weight:700 !important; font-size:17px; margin:4px 8px 4px 0;
  box-shadow:0 3px 10px -4px rgba(0,0,0,.35);
}
a.paseru-navbtn.nav-and{ background:#03C75A !important; }   /* 안드로이드 (네이버 초록) */
a.paseru-navbtn.nav-ios{ background:#0a8f45 !important; }   /* 아이폰 */
a.paseru-navbtn.nav-pc{  background:#2563eb !important; }   /* PC 웹 (밝은 파랑) */

/* ---- 전체 글자 크기 약 20% 확대 ---- */
.stApp{ font-size:16.8px; }
h1{ font-size:38px !important; }
h2{ font-size:29px !important; }
h3{ font-size:24px !important; }
.paseru-card-title{ font-size:22px; }
.paseru-sub{ font-size:17px; }
.paseru-eyebrow, .paseru-step{ font-size:14px; }
.stApp p, .stApp label, .stApp li,
[data-testid="stWidgetLabel"] p,
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *,
div[data-testid="stMetricLabel"] *, div[data-testid="stMetricValue"],
div[data-testid="stExpander"] summary p,
[data-baseweb="tab"]{
  font-size:1rem !important;
}
button, input, textarea, select,
[data-baseweb="select"] div,
div.stButton > button, .stDownloadButton > button,
div.stFormSubmitter > button, [data-testid="stLinkButton"] a{
  font-size:1rem !important;
}
</style>
"""


PASERU_ICON_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="10" stdDeviation="10" flood-color="#0b2f5f" flood-opacity=".18"/>
    </filter>
    <linearGradient id="pin" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0b56a3"/>
      <stop offset="1" stop-color="#08264a"/>
    </linearGradient>
  </defs>
  <rect x="38" y="38" width="436" height="436" rx="82" fill="#fff" stroke="#08264a" stroke-width="18" filter="url(#shadow)"/>
  <path d="M256 72c-86 0-156 68-156 151 0 111 128 199 151 214a10 10 0 0 0 10 0c23-15 151-103 151-214 0-83-70-151-156-151z" fill="url(#pin)" stroke="#fff" stroke-width="16"/>
  <circle cx="256" cy="218" r="106" fill="#fff" opacity=".96"/>
  <rect x="159" y="179" width="194" height="92" rx="24" fill="#ef233c"/>
  <rect x="185" y="199" width="142" height="48" rx="13" fill="#f8fbff"/>
  <rect x="144" y="217" width="24" height="45" rx="10" fill="#111827"/>
  <rect x="344" y="217" width="24" height="45" rx="10" fill="#111827"/>
  <rect x="196" y="158" width="28" height="24" rx="8" fill="#ef233c"/>
  <rect x="288" y="158" width="28" height="24" rx="8" fill="#ef233c"/>
  <rect x="220" y="139" width="72" height="17" rx="7" fill="#cbd5e1"/>
  <rect x="237" y="121" width="38" height="17" rx="7" fill="#e5edf7"/>
  <circle cx="218" cy="224" r="11" fill="#111827"/>
  <circle cx="294" cy="224" r="11" fill="#111827"/>
  <path d="M238 242c10 13 26 13 36 0" fill="none" stroke="#111827" stroke-width="8" stroke-linecap="round"/>
  <rect x="179" y="256" width="42" height="19" rx="7" fill="#ffd166"/>
  <rect x="291" y="256" width="42" height="19" rx="7" fill="#ffd166"/>
  <rect x="188" y="275" width="136" height="16" rx="5" fill="#e5e7eb"/>
  <text x="256" y="386" text-anchor="middle" font-family="Arial, sans-serif" font-size="54" font-weight="900" fill="#08264a" stroke="#fff" stroke-width="9" paint-order="stroke">파세루</text>
</svg>
""".strip()
PASERU_ICON_FALLBACK_URL = "data:image/svg+xml;charset=utf-8," + quote(PASERU_ICON_SVG, safe="")
