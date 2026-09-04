
def build_qr_zip(station, route_results):
    """모든 노선의 카카오맵 QR PNG와 경로 목록을 ZIP으로 묶는다."""
    """모든 노선의 카카오맵 QR PNG와 경로 목록 엑셀을 ZIP으로 묶는다."""
    buffer = io.BytesIO()
    route_lines = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(f"노선_{rr['route_no']}{suffix}_QR.png", make_qr_png(url))
                seq = " → ".join([origin["name"]] + [p["name"] for p in destinations])
                route_lines.append(f"노선 {rr['route_no']}{suffix}: {seq}\n{url}\n")
        zf.writestr("노선별_경로와_링크.txt", "\n".join(route_lines).encode("utf-8-sig"))
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
    for row_no in range(2, ws.max_row + 1):
        ws.row_dimensions[row_no].height = 42
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_printable_qr_html(station, route_results, meta):

    qr_all_col, print_all_col = st.columns(2)
    links_col, qr_all_col, print_all_col = st.columns(3)
    with links_col:
        st.download_button(
            "📊 노선별 경로와 링크 엑셀",
            data=build_route_links_excel(station, route_results),
            file_name=f"{safe_title}_노선별_경로와_링크.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    with qr_all_col:
