

NAVER_MAX_VIA = 5   # 네이버지도 URL Scheme이 지원하는 경유지 최대 개수


def naver_route_url(station, stops, app_name="faseru-origin.streamlit.app"):
    """네이버지도 자동차 길찾기 링크 — 출발(센터) → 경유지들 → 도착(센터).

    경유지는 최대 5개까지 지원하므로, 그보다 많으면 호출부에서 나눠서 만든다.
    """
    q = lambda s: quote(str(s), safe="")
    parts = [
        f"slat={station['lat']}", f"slng={station['lng']}", f"sname={q(station['name'])}",
    ]
    for i, s in enumerate(stops[:NAVER_MAX_VIA], start=1):
        parts += [f"v{i}lat={s['lat']}", f"v{i}lng={s['lng']}", f"v{i}name={q(s['name'])}"]
    # 마지막은 다시 센터로 복귀
    parts += [f"dlat={station['lat']}", f"dlng={station['lng']}", f"dname={q(station['name'])}"]
    parts.append(f"appname={app_name}")
    return "nmap://route/car?" + "&".join(parts)


def naver_intent_url(nmap_url):
    """안드로이드 크롬은 nmap:// 같은 커스텀 스킴을 막기 때문에 intent:// 형식으로 바꿔준다.
    (앱이 없으면 플레이스토어로 이동)"""
    body = nmap_url[len("nmap://"):]
    return f"intent://{body}#Intent;scheme=nmap;package=com.nhn.android.nmap;end"


def naver_web_route_url(station, stops):
    """네이버지도 웹 길찾기 — 앱이 없거나 PC에서 경로를 눈으로 확인할 때 쓰는 대체 수단.
    (웹은 경유지 지정이 제한적이라 출발지 → 마지막 지점 기준으로 열린다)"""
    last = stops[-1]
    q = lambda s: quote(str(s), safe="")
    return ("https://map.naver.com/p/directions/"
            f"{station['lng']},{station['lat']},{q(station['name'])},,/"
            f"{last['lng']},{last['lat']},{q(last['name'])},,/-/car")


def naver_route_links(station, legs):
    """노선 전체를 경유지 포함 링크로 만든다. 경유지가 5개를 넘으면 구간을 나눠 여러 개로.
    반환: [(앱링크, 웹대체링크, 구간지점들), ...]"""
    stops = [{"name": lg["to"], "lat": lg["lat"], "lng": lg["lng"]} for lg in legs]
    if not stops:
        return []
    chunks = [stops[i:i + NAVER_MAX_VIA] for i in range(0, len(stops), NAVER_MAX_VIA)]
    return [(naver_route_url(station, c), naver_web_route_url(station, c), c) for c in chunks]


def card_title(step, text):
def card_title(step, text):
    st.markdown(
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                st.markdown("**📱 내비게이션 — 경유지 포함 전체 경로**")
                links = naver_route_links(station, rr["legs"])
                for li, (nurl, wurl, chunk) in enumerate(links, start=1):
                    seq = " → ".join([station["name"]] + [c["name"] for c in chunk] + [station["name"]])
                    suffix = "" if len(links) == 1 else f" ({li}/{len(links)}구간)"
                    iurl = naver_intent_url(nurl)
                    st.markdown(
                        f'<a class="paseru-navbtn nav-and" href="{iurl}" target="_top">'
                        f'📱 안드로이드폰에서 경유지 안내{suffix}</a>'
                        f'<a class="paseru-navbtn nav-ios" href="{nurl}" target="_top">'
                        f'📱 아이폰에서 경유지 안내{suffix}</a>'
                        f'<a class="paseru-navbtn nav-pc" href="{wurl}" target="_blank">'
                        f'💻 PC에서 지도로 보기{suffix}</a>',
                        unsafe_allow_html=True,
                    )
                    st.caption(f"경로: {seq}")
                    with st.expander("🔧 버튼이 안 열릴 때 — 주소를 복사해 직접 열어보기", expanded=False):
                        st.markdown("아래 주소를 **복사해서 휴대폰 브라우저 주소창에 붙여넣고 이동**해 보세요. "
                                    "버튼은 막혀도 주소창 입력은 열리는 경우가 많습니다.")
                        st.markdown("**① 안드로이드폰용 (intent 방식)**")
                        st.code(iurl, language=None)
                        st.markdown("**② 안드로이드·아이폰 공용 (nmap 방식)**")
                        st.code(nurl, language=None)
                        st.markdown("**경유지 좌표 목록** (다른 내비 앱에 직접 입력할 때)")
                        st.code("\n".join(
                            [f"출발  {station['name']}  {station['lat']:.6f}, {station['lng']:.6f}"]
                            + [f"경유{n}  {c['name']}  {c['lat']:.6f}, {c['lng']:.6f}"
                               for n, c in enumerate(chunk, start=1)]
                            + [f"도착  {station['name']}  {station['lat']:.6f}, {station['lng']:.6f}"]
                        ), language=None)
                if len(links) > 1:
                    st.caption(f"※ 네이버지도는 경유지를 최대 {NAVER_MAX_VIA}개까지 지원해서 "
                               "구간을 나눴습니다. 한 구간씩 순서대로 눌러 주세요.")
                st.caption("※ **경유지를 한 번에 안내받는 것은 휴대폰에서만 됩니다** "
                           "(네이버지도 앱 필요). 안드로이드폰은 첫 번째, 아이폰은 두 번째 버튼을 "
                           "누르세요. PC에서는 앱을 열 수 없어 세 번째 버튼으로 경로만 확인됩니다.")
