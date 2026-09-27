"""카카오맵 링크 · QR · 엑셀 · 인쇄용 HTML 만들기."""
import base64
import html
import io
import json
import zipfile
from urllib.parse import quote

import qrcode
import streamlit as st


def kakao_url(name, lat, lng):
    """카카오맵 길안내 링크 (공백·괄호가 있어도 깨지지 않도록 인코딩)."""
    return ("https://map.kakao.com/link/to/"
            f"{quote(str(name), safe='')},{lat},{lng}")


KAKAO_MAX_ROUTE_POINTS = 5  # 카카오맵 한 번 실행에 담을 최대 목적지·경유지 수(출발지 제외)


def kakao_route_url(origin, destinations):
    """카카오맵 자동차 길찾기 링크를 만든다.

    origin은 출발지, destinations의 마지막 항목은 목적지이며 그 앞 항목은
    경유지로 전달된다. 현장 혼선을 줄이기 위해 한 번에 최대 5개 목적지만 묶는다.
    """
    if not destinations:
        return ""

    def place(p):
        name = quote(str(p["name"]), safe="")
        return f"{name},{float(p['lat']):.7f},{float(p['lng']):.7f}"

    points = [origin] + list(destinations)
    return "https://map.kakao.com/link/by/car/" + "/".join(place(p) for p in points)


def kakao_route_links(station, legs):
    """소방서 → 경유지 순서 → 소방서 귀소까지 카카오맵 링크 목록.

    카카오맵 실행 한 번에 목적지·경유지를 5개까지 담고, 다음 구간은
    앞 구간의 마지막 지점을 출발점으로 겹쳐 이어간다.
    예: 출발→1→2→3→4→5 / 5→6→귀소.
    반환: [(URL, 출발지, 구간 목적지 목록), ...]
    """
    stops = [{"name": lg["to"], "lat": lg["lat"], "lng": lg["lng"]} for lg in legs]
    if not stops:
        return []

    destinations_all = stops + [{
        "name": f"{station.get('name', '출발지')} 귀소",
        "lat": station["lat"],
        "lng": station["lng"],
    }]
    links = []
    origin = station
    start_index = 0

    while start_index < len(destinations_all):
        destinations = destinations_all[start_index:start_index + KAKAO_MAX_ROUTE_POINTS]
        if not destinations:
            break
        links.append((kakao_route_url(origin, destinations), origin, destinations))
        origin = destinations[-1]
        start_index += KAKAO_MAX_ROUTE_POINTS

    return links


def make_qr_png(data):
    """링크를 휴대폰으로 넘길 수 있는 QR코드 PNG 바이트로 만든다."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def build_qr_zip(station, route_results):
    """모든 노선의 카카오맵 QR PNG와 경로 목록 엑셀을 ZIP으로 묶는다."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for rr in route_results:
            links = kakao_route_links(station, rr["legs"])
            for li, (url, origin, destinations) in enumerate(links, start=1):
                suffix = "" if len(links) == 1 else f"_구간{li}"
                zf.writestr(f"노선_{rr['route_no']}{suffix}_QR.png", make_qr_png(url))
        zf.writestr("노선별_경로와_링크.xlsx", build_route_links_excel(station, route_results))
    return buffer.getvalue()


def build_route_links_excel(station, route_results):
    """노선 순서와 클릭 가능한 카카오맵 링크를 엑셀로 만든다."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "노선별 경로와 링크"
    ws.sheet_view.showGridLines = False

    headers = ["노선", "구간", "출발지", "경유지 및 목적지 순서", "거리(km)", "시간(분)", "카카오맵"]
    ws.append(headers)

    for rr in route_results:
        links = kakao_route_links(station, rr["legs"])
        for li, (url, origin, destinations) in enumerate(links, start=1):
            sequence = " → ".join([origin["name"]] + [p["name"] for p in destinations])
            ws.append([
                rr["route_no"],
                li if len(links) > 1 else 1,
                origin["name"],
                sequence,
                round(rr["total_km"], 1),
                round(rr["total_min"]),
                "카카오맵에서 열기",
            ])
            link_cell = ws.cell(row=ws.max_row, column=7)
            link_cell.hyperlink = url
            link_cell.style = "Hyperlink"

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    thin = Side(style="thin", color="D9D9D9")
    bottom_border = Border(bottom=thin)

    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    ws.append([
        "안내", "", "",
        "지도와 예상거리·시간은 입력한 출발지 기준입니다. 카카오맵 실행 후 실제 내비는 휴대폰 현재 위치 기준으로 다시 안내될 수 있습니다.",
        "", "", "",
    ])
    for cell in ws[2]:
        cell.fill = PatternFill("solid", fgColor="FFF2CC")
        cell.font = Font(color="7C2D12", bold=True)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = bottom_border

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.border = bottom_border
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        row[0].alignment = Alignment(horizontal="center", vertical="center")
        row[1].alignment = Alignment(horizontal="center", vertical="center")
        row[4].alignment = Alignment(horizontal="right", vertical="center")
        row[5].alignment = Alignment(horizontal="right", vertical="center")
        row[6].alignment = Alignment(horizontal="center", vertical="center")

    widths = {"A": 9, "B": 9, "C": 20, "D": 75, "E": 13, "F": 13, "G": 19}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    ws.row_dimensions[1].height = 26
    ws.row_dimensions[2].height = 42
    for row_no in range(3, ws.max_row + 1):
        ws.row_dimensions[row_no].height = 42
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_printable_qr_html(station, route_results, meta):
    """브라우저에서 열어 A4로 인쇄할 수 있는 노선별 QR 문서를 만든다."""
    cards = []
    title = html.escape(str(meta.get("title") or "순찰노선"))
    period = html.escape(str(meta.get("period") or ""))

    for rr in route_results:
        team_name = html.escape(st.session_state.get(f"team_name_{rr['route_no']}", ""))
        team_members = html.escape(st.session_state.get(f"team_members_{rr['route_no']}", ""))
        qr_blocks = []
        links = kakao_route_links(station, rr["legs"])
        for li, (url, origin, destinations) in enumerate(links, start=1):
            suffix = "" if len(links) == 1 else f"-{li} ({li}/{len(links)}구간)"
            seq = " → ".join([origin["name"]] + [p["name"] for p in destinations])
            qr_b64 = base64.b64encode(make_qr_png(url)).decode("ascii")
            qr_blocks.append(
                f'<section class="qr-block"><h2>노선 {rr["route_no"]}{suffix}</h2>'
                f'<img src="data:image/png;base64,{qr_b64}" alt="노선 QR코드">'
                f'<p class="scan">휴대폰 카메라로 스캔하면 이 구간의 카카오맵 코스가 열립니다.</p>'
                f'<p class="sequence">{html.escape(seq)}</p>'
                f'<p class="note">지도와 예상거리·시간은 입력한 출발지 기준입니다. '
                f'카카오맵 실행 후 실제 내비는 휴대폰 현재 위치 기준으로 다시 안내될 수 있습니다.</p></section>'
            )
        people = ""
        if team_name or team_members:
            people = f'<p class="people">담당 조 {team_name or "-"}　 조원 {team_members or "-"}</p>'
        cards.append(
            f'<article class="route"><header><div>{title}</div><strong>노선 {rr["route_no"]}</strong>'
            f'<span>{len(rr["stops"])}개소 · {rr["total_km"]:.1f}km · 약 {rr["total_min"]:.0f}분</span>'
            f'</header>{people}{"".join(qr_blocks)}</article>'
        )

    return f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} QR 인쇄</title>
<style>
@page {{ size: A4; margin: 14mm; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; color: #111827; font-family: "Malgun Gothic", "Apple SD Gothic Neo", sans-serif; }}
.print-button {{ position: fixed; right: 18px; top: 18px; padding: 12px 18px; border: 0; border-radius: 8px;
  background:#a33a3f; color:white; font-size:16px; font-weight:700; cursor:pointer; }}
.route {{ min-height: 267mm; page-break-after: always; text-align: center; padding: 8mm 5mm; }}
.route:last-child {{ page-break-after: auto; }}
header div {{ font-size: 17px; margin-bottom: 8px; }}
header strong {{ display: block; font-size: 28px; margin-bottom: 6px; }}
header span, .people {{ font-size: 14px; color: #4b5563; }}
.people {{ margin: 10px 0; }}
.qr-block {{ margin-top: 18px; }}
.qr-block h2 {{ font-size: 20px; margin: 0 0 8px; }}
.qr-block img {{ width: 88mm; max-width: 82vw; height: auto; }}
.scan {{ font-size: 14px; font-weight: 700; margin: 4px 0 12px; }}
.sequence {{ font-size: 15px; line-height: 1.7; overflow-wrap: anywhere; border-top: 1px solid #d1d5db;
  padding-top: 12px; margin: 0 auto; max-width: 170mm; }}
.note {{ max-width: 170mm; margin: 10px auto 0; padding: 8px 10px; border-radius: 8px;
  background: #fff7ed; color: #7c2d12; font-size: 13px; line-height: 1.55; font-weight: 700; }}
.period {{ text-align: center; color: #4b5563; margin: 0 0 8px; }}
@media print {{ .print-button {{ display: none; }} }}
</style></head><body>
<button class="print-button" onclick="window.print()">🖨 인쇄하기</button>
<p class="period">{period}</p>{"".join(cards)}
</body></html>'''.encode("utf-8")


def build_center_route_print_html(station, route_results, meta):
    """센터 지리조사용: 지도·방문순서·확인란을 한 문서로 만든다."""
    title = html.escape(str(meta.get("title") or "센터 지리조사 노선 결과"))
    pages, map_scripts = [], []

    for rr in route_results:
        route_no = rr["route_no"]
        map_id = f"route_map_{route_no}"
        team_name = st.session_state.get(f"team_name_{route_no}", "")
        auto_members = ", ".join(rr.get("assigned_members") or [])
        team_members = st.session_state.get(f"team_members_{route_no}", "") or auto_members
        vehicle = f"{rr.get('vehicle_no')}호차" if rr.get("vehicle_no") else ""

        stop_rows = []
        for index, leg in enumerate(rr["legs"], start=1):
            assignee = leg.get("assigned_to") or ""
            stop_rows.append(
                '<li><span class="order">{}</span><div><strong>{}</strong><small>{}{}</small></div>'
                '<span class="check">□</span></li>'.format(
                    index,
                    html.escape(str(leg["to"])),
                    html.escape(str(leg.get("to_address") or "")),
                    f" · 담당 {html.escape(str(assignee))}" if assignee else "",
                )
            )

        people = " · ".join(v for v in (vehicle, team_name, team_members) if v)
        pages.append(f'''
<section class="route-page">
  <header>
    <p class="doc-title">{title}</p>
    <div class="route-title"><strong>노선 {route_no}</strong>
      <span>{len(rr['stops'])}개소 · 총 {rr['total_km']:.1f}km · 약 {rr['total_min']:.0f}분</span>
    </div>
    <p class="team">{html.escape(people)}</p>
  </header>
  <div class="route-body">
    <div id="{map_id}" class="route-map"></div>
    <div class="stops"><h2>방문 순서</h2><ol>{''.join(stop_rows)}</ol></div>
  </div>
  <footer>출발·복귀: {html.escape(station['name'])}　　확인자: ____________________</footer>
</section>''')

        path = rr.get("path") or [[station["lat"], station["lng"]]]
        markers = [{"lat": station["lat"], "lng": station["lng"], "label": "출발·복귀"}]
        markers += [
            {"lat": leg["lat"], "lng": leg["lng"], "label": f"{i}. {leg['to']}"}
            for i, leg in enumerate(rr["legs"], start=1)
        ]
        map_scripts.append(f'''
const map{route_no}=L.map('{map_id}',{{zoomControl:true}});
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'© OpenStreetMap'}}).addTo(map{route_no});
const path{route_no}={json.dumps(path, ensure_ascii=False)};
const line{route_no}=L.polyline(path{route_no},{{color:'#a33a3f',weight:5}}).addTo(map{route_no});
{json.dumps(markers, ensure_ascii=False)}.forEach((p,i)=>L.marker([p.lat,p.lng]).addTo(map{route_no}).bindTooltip(p.label));
map{route_no}.fitBounds(line{route_no}.getBounds(),{{padding:[20,20]}});
''')

    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
@page {{ size:A4 landscape; margin:12mm; }} *{{box-sizing:border-box}}
body{{margin:0;color:#17263a;font-family:"Malgun Gothic",sans-serif;background:#eef1f4}}
.print-button{{position:fixed;right:18px;top:18px;z-index:9999;padding:12px 20px;border:0;border-radius:9px;background:#a33a3f;color:#fff;font-weight:700;cursor:pointer}}
.route-page{{width:273mm;min-height:186mm;margin:10mm auto;padding:8mm;background:#fff;page-break-after:always}}
.route-page:last-of-type{{page-break-after:auto}} .doc-title{{margin:0;text-align:center;font-size:17px}}
.route-title{{display:flex;align-items:baseline;gap:18px;border-bottom:3px solid #a33a3f;padding:5px 0 8px}}
.route-title strong{{font-size:27px}} .route-title span{{font-size:16px;font-weight:700}} .team{{height:20px;margin:7px 0;color:#4b5563}}
.route-body{{display:grid;grid-template-columns:58% 42%;gap:8mm;height:130mm}} .route-map{{width:100%;height:100%;border:1px solid #9aa5b1}}
.stops{{overflow:hidden}} .stops h2{{font-size:18px;margin:0 0 7px}} ol{{list-style:none;padding:0;margin:0}}
li{{display:grid;grid-template-columns:28px 1fr 28px;align-items:center;gap:7px;border-bottom:1px solid #d8dee7;padding:6px 2px}}
.order{{display:flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:50%;background:#315d78;color:#fff;font-weight:700}}
li strong{{display:block;font-size:14px}} li small{{display:block;color:#4b5563;font-size:10px;margin-top:2px}} .check{{font-size:25px;text-align:center}}
footer{{margin-top:7px;padding-top:5px;border-top:1px solid #9aa5b1;font-size:12px;color:#4b5563}}
@media print{{body{{background:#fff}}.print-button{{display:none}}.route-page{{margin:0;box-shadow:none}}}}
</style></head><body><button class="print-button" onclick="window.print()">🖨 인쇄하기</button>
{''.join(pages)}<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>{''.join(map_scripts)}</script>
</body></html>'''.encode("utf-8")
