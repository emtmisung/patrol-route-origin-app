"""파세루 오리진 자동 점검(스모크 테스트).

실제 NCP·카카오 API를 부르지 않고(가짜 응답, 비용 0원) 로그인 → 예시 18건 좌표검색 →
소화전 현장안내 → 5종 순찰 노선 생성까지 끝까지 실행해 오류가 없는지 확인한다.

사용법 (저장소 최상위 폴더에서):
    pip install -r requirements.txt
    python tests/smoke_test.py            # 결과 요약만 출력
    python tests/smoke_test.py . out.txt  # 화면 요소 전체를 out.txt에 기록(수정 전후 비교용)
    FAIL_ONE=1 python tests/smoke_test.py # 주소 1건 좌표 실패 상황 점검
"""
import hashlib
import math
import os
import sys
import time

import requests

APP_DIR = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
OUT = sys.argv[2] if len(sys.argv) > 2 else os.devnull
ONLY = sys.argv[3].split(",") if len(sys.argv) > 3 else None
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)

FAIL_ONE = os.environ.get("FAIL_ONE") == "1"
CALLS = {"geocode": 0, "directions": 0, "place": 0}


class FakeResp:
    def __init__(self, data, status=200):
        self._d = data
        self.status_code = status

    def json(self):
        return self._d


def fake_coord(q):
    h = hashlib.sha256(q.encode()).digest()
    return 35.80 + h[0] / 255 * 0.15, 128.20 + h[1] / 255 * 0.18


def hav(a, b):
    R = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dphi = math.radians(b[0] - a[0]); dl = math.radians(b[1] - a[1])
    x = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))


def fake_get(url, params=None, headers=None, timeout=None, **kw):
    params = params or {}
    if "geocode" in url:
        CALLS["geocode"] += 1
        q = params.get("query", "")
        # 번지 없는 '리' 단위까지 줄여야 찾히는 주소를 흉내: 원본에 '없는' 이 있으면 실패
        if "없는" in q or "오류" in q or (FAIL_ONE and "칠선" in q):
            return FakeResp({"addresses": []})
        lat, lng = fake_coord(q)
        return FakeResp({"addresses": [{"x": str(lng), "y": str(lat), "roadAddress": q + " (가짜)"}]})
    if "direction" in url:
        CALLS["directions"] += 1
        s = [float(v) for v in params["start"].split(",")]
        g = [float(v) for v in params["goal"].split(",")]
        d = hav((s[1], s[0]), (g[1], g[0])) * 1.3
        return FakeResp({"route": {"trafast": [{"summary": {"distance": d * 1000, "duration": d / 40 * 3600000},
                                                "path": [s, g]}]}})
    if "kakao" in url:
        CALLS["place"] += 1
        return FakeResp({"place": [{"name": params.get("q"), "new_address": "경상북도 성주군 성주읍 주산로 193",
                                    "lat": "35.933968", "lon": "128.293301"}]})
    raise RuntimeError("unexpected url " + url)


requests.get = fake_get

from streamlit.testing.v1 import AppTest  # noqa: E402


def snapshot(at):
    lines = []

    def walk(node, depth):
        name = type(node).__name__
        bits = []
        for attr in ("label", "value", "body", "proto_value"):
            try:
                v = getattr(node, attr)
            except Exception:
                continue
            if callable(v):
                continue
            s = repr(v)
            if len(s) > 300:
                s = s[:300] + "…"
            bits.append(f"{attr}={s}")
        if name == "UnknownElement":
            try:
                raw = str(node.proto)
                raw = __import__("re").sub(r'(id|widget_id|component_instance_id|form_id|element_id)[:=] ?"[^"]*"', "", raw)
                raw = __import__("re").sub(r"// [0-9.]+", "", raw)
                raw = __import__("re").sub(r"[0-9a-f]{32,64}", "<hex>", raw)
                raw = __import__("re").sub(r"v1:[0-9]+:", "v1:<ts>:", raw)
                raw = raw.replace("__main__.paseru_geolocation", "paseru.ui.paseru_geolocation")
                bits.append("proto#" + hashlib.sha256(raw.encode()).hexdigest()[:12] + " len=" + str(len(raw)))
            except Exception as exc:
                bits.append("proto?" + type(exc).__name__)
        if name not in ("Block", "ElementTree"):
            lines.append("  " * depth + name + " " + " ".join(bits))
        for child in getattr(node, "children", {}).values():
            walk(child, depth + 1)

    walk(at._tree, 0)
    return lines


def new_app():
    at = AppTest.from_file(os.path.join(APP_DIR, "app.py"), default_timeout=180)
    at.secrets["APP_PASSWORD"] = "pw"
    at.secrets["NCP_CLIENT_ID"] = "id"
    at.secrets["NCP_CLIENT_SECRET"] = "secret"
    at.session_state["paseru_browser_storage"] = {}
    return at


def by_label(widgets, label):
    for w in widgets:
        if w.label == label:
            return w
    raise KeyError(label + " not in " + str([w.label for w in widgets]))


def login(at):
    at.run()
    by_label(at.text_input, "비밀번호").input("pw")
    by_label(at.button, "앱 시작하기").click()
    at.run()
    return at


def prepare_targets(at):
    # 출발부서 조회
    at.button(key="search_departure_department_btn").click().run()
    # 예시 불러오기
    at.button(key="load_sample_targets_button").click().run()
    # 좌표 검색 시작
    by_label(at.button, "🔴 좌표 검색 시작").click().run()
    for _ in range(40):
        time.sleep(0.25)
        at.run()
        if any(b.label == "🔄 대상 좌표 다시 검색" for b in at.button):
            break
    return at


def fail_check(at, tag, log):
    if at.exception:
        for e in at.exception:
            log.append(f"!! EXCEPTION [{tag}]: {e.value}\n{''.join(e.stack_trace) if hasattr(e,'stack_trace') else ''}")


PURPOSES = ["① 지휘관 현장방문", "② 특별경계근무용", "③ 계절순찰", "④ 예방검사", "⑤ 지리조사(센터용)"]


def main():
    out = []
    at = new_app()
    login(at)
    fail_check(at, "login", out)
    out.append("=== AFTER LOGIN ===")
    out += snapshot(at)
    prepare_targets(at)
    fail_check(at, "coords", out)
    out.append("=== AFTER COORDS ===")
    out += snapshot(at)
    if FAIL_ONE:
        with open(OUT, "w") as f:
            f.write("\n".join(out))
        errors = [l for l in out if l.startswith("!!")]
        shown = any("실패 1건" in l for l in out)
        print(f"좌표 실패 상황 점검 완료: 실패 안내 표시 {'예' if shown else '아니오'} · 오류 {len(errors)}건")
        for e in errors:
            print(e)
        sys.exit(1 if errors or not shown else 0)

    # 소화전 현장안내
    at.button(key="open_hydrant_direct_panel").click().run()
    at.text_input(key="hydrant_direct_target_address").input("경상북도 성주군 성주읍 경산리 100").run()
    at.text_input(key="hydrant_direct_no").input("114").run()
    at.button(key="build_hydrant_direct_route").click().run()
    fail_check(at, "hydrant", out)
    out.append("=== HYDRANT DIRECT ===")
    out += snapshot(at)
    # 긴급 노선안내 화면에서 나가고 다시 예시로
    at.button(key="close_hydrant_direct_panel").click().run()
    at.button(key="load_sample_targets_button").click().run()
    for _ in range(40):
        if any(b.label == "🔄 대상 좌표 다시 검색" for b in at.button):
            break
        if any(b.label == "🔴 좌표 검색 시작" for b in at.button):
            by_label(at.button, "🔴 좌표 검색 시작").click().run()
        time.sleep(0.25)
        at.run()

    for p in PURPOSES:
        if ONLY and p[0] not in ONLY:
            continue
        at.pills[0].set_value(p).run()
        fail_check(at, p + " select", out)
        out.append(f"=== PURPOSE {p} (settings) ===")
        out += snapshot(at)
        run_btn = [b for b in at.button if "노선 생성" in (b.label or "") and not b.disabled]
        out.append("build buttons: " + repr([b.label for b in at.button if "노선" in (b.label or "")]))
        if run_btn:
            run_btn[0].click().run()
            fail_check(at, p + " build", out)
        out.append(f"=== PURPOSE {p} (result) ===")
        try:
            rr = at.session_state["route_results"]
            out.append("ROUTES " + repr([[(s.get("name") if isinstance(s, dict) else s) for s in r.get("stops", r.get("points", []))] if isinstance(r, dict) else r for r in rr])[:3000])
            out.append("ROUTEKEYS " + repr([sorted(r.keys()) for r in rr][:1]))
            out.append("META " + repr(at.session_state["meta"])[:3000])
        except Exception as exc:
            out.append("no route_results " + repr(exc))
        out += snapshot(at)
    out.append("CALLS " + repr(CALLS))
    with open(OUT, "w") as f:
        f.write("\n".join(out))
    errors = [l for l in out if l.startswith("!!")]
    routes = [l for l in out if l.startswith("ROUTES")]
    print(f"점검 완료: 노선 생성 {len(routes)}/5종 · 오류 {len(errors)}건 · 가짜 API 호출 {CALLS}")
    for e in errors:
        print(e)
    sys.exit(1 if errors or len(routes) < 5 else 0)


main()
