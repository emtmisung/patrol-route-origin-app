"""folium 지도 그리기."""
import html
import re

import folium
from branca.element import Element

from paseru.geo import geocode_once


def stop_label(name, address):
    """대상명 + 주소 표기. 이름 안에 이미 주소(또는 번지)가 들어 있으면 중복 표기하지 않는다."""
    name = (name or "").strip()
    address = (address or "").strip()
    if not address or address == name:
        return name
    # "경상북도 성주군 월항면 인촌1리 606-1" -> 뒤쪽 핵심부("인촌1리 606-1")가 이름에 있으면 생략
    tail = " ".join(address.split()[-2:])
    if tail and tail in name:
        return name
    if address in name:
        return name
    return f"{name} ({address})"


def build_distribution_map(coords_df, station=None):
    """좌표검색에 성공한 전체 대상을 한 화면에 보여주는 분포지도."""
    valid = coords_df.dropna(subset=["위도", "경도"]).copy()
    if valid.empty:
        return None

    center_lat = float(valid["위도"].astype(float).mean())
    center_lng = float(valid["경도"].astype(float).mean())
    distribution_map = folium.Map(location=[center_lat, center_lng], zoom_start=12)
    bounds = []

    for row_index, row in valid.iterrows():
        lat, lng = float(row["위도"]), float(row["경도"])
        visit_no = int(row_index) + 1 if isinstance(row_index, (int, float)) else len(bounds) + 1
        name = str(row.get("대상명", "")).strip()
        address = str(row.get("주소", "")).strip()
        bounds.append([lat, lng])
        folium.Marker(
            [lat, lng],
            tooltip=f"{visit_no}. {name}",
            popup=folium.Popup(html.escape(stop_label(name, address)), max_width=360),
            icon=folium.DivIcon(
                icon_size=(30, 30), icon_anchor=(15, 15),
                html=(
                    '<div style="background:#1f6fb2;color:#ffffff;'
                    'width:26px;height:26px;border-radius:50%;border:2px solid #ffffff;'
                    'box-shadow:0 1px 5px rgba(0,0,0,.45);display:flex;align-items:center;'
                    'justify-content:center;font-family:sans-serif;font-weight:700;font-size:12px;'
                    f'line-height:1;">{visit_no}</div>'
                ),
            ),
        ).add_to(distribution_map)

    if station and station.get("lat") is not None and station.get("lng") is not None:
        station_lat, station_lng = float(station["lat"]), float(station["lng"])
        bounds.append([station_lat, station_lng])
        folium.Marker(
            [station_lat, station_lng],
            tooltip=f"출발지: {station.get('name', '')}",
            icon=folium.DivIcon(
                icon_size=(66, 28), icon_anchor=(33, 14),
                html=(
                    '<div style="background:#a33a3f;color:#ffffff;padding:4px 9px;'
                    'border-radius:14px;border:2px solid #ffffff;box-shadow:0 1px 5px rgba(0,0,0,.45);'
                    'text-align:center;font-family:sans-serif;font-weight:700;font-size:12px;'
                    'line-height:1.2;white-space:nowrap;">🚒 출발</div>'
                ),
            ),
        ).add_to(distribution_map)

    if len(bounds) > 1:
        distribution_map.fit_bounds(bounds, padding=(30, 30))
    return distribution_map


def manual_map_start(coords_df, failed_address, station_lat=None, station_lng=None):
    """실패 주소의 도로·읍면동·시군구와 같은 확인 좌표를 찾아 지도 시작점을 정한다."""
    valid = coords_df.dropna(subset=["위도", "경도"]).copy()
    normalized_address = re.sub(r"\s+", " ", str(failed_address or "")).strip()
    address_tokens = normalized_address.split()
    road_tokens = list(dict.fromkeys(
        [token for token in address_tokens if token.endswith(("로", "길"))]
        + re.findall(r"[가-힣0-9]+(?:로|길)(?=\s*\d|\s|$)", normalized_address)
    ))
    search_levels = [
        ("도로명", road_tokens, 15),
        ("읍·면·동", [token for token in address_tokens if token.endswith(("읍", "면", "동", "리"))], 14),
        ("시·군·구", [token for token in address_tokens if token.endswith(("시", "군", "구"))], 12),
    ]
    broad_search_queries = []
    for level_name, tokens, zoom in search_levels:
        for token in reversed(tokens):
            token_index = normalized_address.find(token)
            if token_index >= 0:
                query = normalized_address[:token_index + len(token)]
            else:
                query = " ".join(address_tokens[: address_tokens.index(token) + 1]) if token in address_tokens else token
            query = re.sub(r"\s+", " ", query).strip()
            if query and all(query != existing[0] for existing in broad_search_queries):
                broad_search_queries.append((query, token, level_name, zoom))

    if not valid.empty:
        valid_addresses = valid["주소"].fillna("").astype(str)
        for level_name, tokens, zoom in search_levels:
            for token in reversed(tokens):
                matched = valid[valid_addresses.str.contains(re.escape(token), regex=True)]
                if not matched.empty:
                    return (
                        float(matched["위도"].astype(float).mean()),
                        float(matched["경도"].astype(float).mean()),
                        zoom,
                        f"{token} 주변({level_name} 일치 대상 기준)",
                    )

        return (
            float(valid["위도"].astype(float).mean()),
            float(valid["경도"].astype(float).mean()),
            12,
            "좌표 확인 대상의 전체 분포 중심",
        )

    for query, token, level_name, zoom in broad_search_queries:
        lat, lng, status = geocode_once(query)
        if status == "ok":
            return lat, lng, zoom, f"{token} 주변({level_name} 주소 기준)"

    if station_lat is not None and station_lng is not None:
        return float(station_lat), float(station_lng), 12, "출발지 주변"
    return 36.0, 128.0, 7, "대한민국 중심"


def add_manual_location_layer_buttons(map_obj, satellite_layer, normal_layer, road_layer, label_layer):
    """위성찾기 지도에서 현장 사용자가 보기 방식을 크게 바꿀 수 있게 한다."""
    map_name = map_obj.get_name()
    satellite_name = satellite_layer.get_name()
    normal_name = normal_layer.get_name()
    road_name = road_layer.get_name()
    label_name = label_layer.get_name()
    control_style = """
    <style>
      .manual-map-switch {
        background: rgba(255,255,255,.96);
        border: 1px solid #9aa7b3;
        border-radius: 10px;
        box-shadow: 0 2px 10px rgba(0,0,0,.22);
        padding: 7px;
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .manual-map-switch button {
        appearance: none;
        border: 1px solid #6f7d8a;
        border-radius: 8px;
        background: #ffffff;
        color: #1f2d3d;
        font-family: Arial, 'Noto Sans KR', sans-serif;
        font-size: 14px;
        font-weight: 800;
        line-height: 1.2;
        padding: 9px 10px;
        min-width: 118px;
        cursor: pointer;
      }
      .manual-map-switch button.active {
        background: #1f6fb2;
        border-color: #18598f;
        color: #ffffff;
      }
    </style>
    """
    control_script = f"""
    <script>
      (function() {{
        var map = {map_name};
        var satellite = {satellite_name};
        var normal = {normal_name};
        var roads = {road_name};
        var labels = {label_name};
        var buttons = {{}};

        function setActive(mode) {{
          Object.keys(buttons).forEach(function(key) {{
            buttons[key].classList.toggle('active', key === mode);
          }});
        }}

        function setManualMapMode(mode) {{
          if (map.hasLayer(normal)) map.removeLayer(normal);
          if (map.hasLayer(satellite)) map.removeLayer(satellite);
          if (map.hasLayer(roads)) map.removeLayer(roads);
          if (map.hasLayer(labels)) map.removeLayer(labels);

          if (mode === 'normal') {{
            map.addLayer(normal);
          }} else {{
            map.addLayer(satellite);
            if (mode === 'hybrid') {{
              map.addLayer(roads);
              map.addLayer(labels);
            }}
          }}
          setActive(mode);
        }}

        var Control = L.Control.extend({{
          options: {{ position: 'topright' }},
          onAdd: function() {{
            var box = L.DomUtil.create('div', 'manual-map-switch');
            L.DomEvent.disableClickPropagation(box);
            [
              ['hybrid', '위성+도로명'],
              ['normal', '일반지도'],
              ['satellite', '위성만 보기']
            ].forEach(function(item) {{
              var button = L.DomUtil.create('button', '', box);
              button.type = 'button';
              button.textContent = item[1];
              buttons[item[0]] = button;
              L.DomEvent.on(button, 'click', function(event) {{
                L.DomEvent.stop(event);
                setManualMapMode(item[0]);
              }});
            }});
            return box;
          }}
        }});
        map.addControl(new Control());
        setManualMapMode('hybrid');
      }})();
    </script>
    """
    map_obj.get_root().html.add_child(Element(control_style))
    map_obj.get_root().script.add_child(Element(control_script))
