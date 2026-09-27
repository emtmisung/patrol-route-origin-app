"""노선 편성 알고리즘(최근접 이웃 · 장거리 분리 · 지리조사 배정)."""
import math

from paseru.geo import haversine_km, road_route
from paseru.settings import AVG_SPEED_KMH, ROAD_FACTOR


# ----------------------------------------------------------------------------
# 경로 편성 알고리즘 (최근접 이웃 기반, 소방서 출발/복귀)
# 거리·시간 판단은 전부 NCP Directions5의 실제 도로거리를 사용한다.
# (직선거리는 API 호출이 실패했을 때만 비상 대체값으로 쓰인다)
# ----------------------------------------------------------------------------
def real_leg(a, b, on_call=None):
    """a, b: dict(lat, lng). 실도로 거리(km)/시간(분) 반환 (실패 시 직선거리 보정값)."""
    km, mins, _ = road_route(a["lat"], a["lng"], b["lat"], b["lng"])
    if on_call:
        on_call()
    if km is None:
        km = haversine_km((a["lat"], a["lng"]), (b["lat"], b["lng"])) * ROAD_FACTOR
        mins = km / AVG_SPEED_KMH * 60
    return km, mins


def nearest_by_straight_line(cur, candidates, k):
    """직선거리로 가까운 순 k개만 추린다. (실제 API 호출 횟수를 줄이기 위한 1차 필터)

    도로망은 직선거리와 순서가 크게 다르지 않으므로, 가까운 후보 몇 개만
    실제 도로거리로 확인해도 결과는 거의 동일하면서 API 호출은 크게 줄어든다.
    """
    if k <= 0 or k >= len(candidates):
        return candidates
    ranked = sorted(
        candidates,
        key=lambda p: haversine_km((cur["lat"], cur["lng"]), (p["lat"], p["lng"])),
    )
    return ranked[:k]


def build_routes(points, station, mode, max_per_route, seg_max_km, seg_max_min,
                 target_min_high, max_routes_cap, basis="distance", on_call=None,
                 candidate_k=5, should_stop=None, service_min_per_stop=0,
                 strict_route_cap=False, route_variant=0):
    """points: list of dict(name, address, lat, lng)
    반환: routes(list of list of point dict), unassigned(장거리/미배정)

    mode:
      "segment"     — 구간당 거리·시간 제한
      "target_time" — 노선 전체 왕복 목표시간 제한
      "fixed"       — 노선당 구간 수(max_per_route)를 그대로 채움 (노선 수 = 상한까지)
    basis: "distance"(거리 기준) | "time"(소요시간 기준)
    candidate_k: 다음 지점 후보를 직선거리로 몇 개까지 좁혀서 실제 API로 확인할지 (0=전수)
    should_stop: 호출 한도 초과 등으로 중단해야 하는지 판단하는 함수.
                 중단되면 그때까지 편성된 노선만 반환한다(진행분 보존).
    strict_route_cap: 노선 수 상한에 도달했을 때 남은 대상을 마지막 노선에
                      합치지 않고 미배정으로 반환한다.
    route_variant: 같은 조건에서 다른 후보 순서로 재탐색할 때 쓰는 번호.
    """
    remaining = points[:]
    routes = []

    guard = 0
    while remaining and guard < 500:
        if should_stop and should_stop():
            break
        guard += 1
        cur = station
        route = []
        acc_min = 0.0

        while remaining:
            if should_stop and should_stop():
                break
            # 1차: 직선거리로 후보 좁히기 → 2차: 좁혀진 후보만 실도로 거리/시간 확인
            candidates = nearest_by_straight_line(cur, remaining, candidate_k)
            legs = [(p, *real_leg(cur, p, on_call)) for p in candidates]
            legs.sort(key=(lambda t: t[2]) if basis == "time" else (lambda t: t[1]))
            if route_variant and len(legs) > 1:
                variant_window = min(len(legs), max(2, min(candidate_k or len(legs), 4)))
                offset = (int(route_variant) + guard + len(route)) % variant_window
                legs = legs[offset:variant_window] + legs[:offset] + legs[variant_window:]
            nxt, leg_km, leg_min = legs[0]

            # 노선의 첫 지점은 제한값을 적용하지 않는다.
            # (소방서에서 가장 가까운 대상까지의 거리가 이미 제한값보다 크면
            #  어떤 노선도 못 만들고 전부 '장거리'로 빠지는 문제를 막기 위함)
            first_stop = not route

            if mode == "fixed":
                if len(route) >= max_per_route:
                    break
            elif mode == "segment":
                if not first_stop and (leg_km > seg_max_km or leg_min > seg_max_min):
                    break
                if len(route) >= max_per_route:
                    break
            else:  # target_time
                back_km, back_min = real_leg(nxt, station, on_call)
                projected = acc_min + leg_min + service_min_per_stop + back_min
                if not first_stop and projected > target_min_high:
                    break
                if len(route) >= max_per_route:
                    break

            route.append(nxt)
            acc_min += leg_min + service_min_per_stop
            cur = nxt
            remaining.remove(nxt)

        if not route:
            # 어떤 조건도 만족 못하는 경우(예: 첫 지점부터 원거리) -> 강제 배정 방지, 장거리로 이관
            break
        routes.append(route)

        if max_routes_cap and len(routes) >= max_routes_cap and remaining:
            if not strict_route_cap:
                # 일반 순찰은 기존 동작 유지: 남은 지점을 마지막 노선에 이어붙인다.
                for p in remaining[:]:
                    route.append(p)
                    remaining.remove(p)
            break

    return routes, remaining


def allocate_hydrants_to_members(points, station, members):
    """개인별 개수 차이를 1개 이하로 유지하면서 인접 구역으로 배정한다.

    같은 차량의 팀원을 연속 배치한 뒤 센터 기준 방위각으로 정렬한 소화전을
    연속 구간으로 나눠, 같은 차량 팀원들의 담당 구역도 서로 가깝게 만든다.
    """
    if not points or not members:
        return points

    ordered_members = sorted(members, key=lambda m: (m["vehicle_no"], m["order"]))
    ordered_points = sorted(
        points,
        key=lambda p: (
            math.atan2(p["lat"] - station["lat"], p["lng"] - station["lng"]),
            haversine_km((station["lat"], station["lng"]), (p["lat"], p["lng"])),
        ),
    )
    base, extra = divmod(len(ordered_points), len(ordered_members))
    assigned = []
    cursor = 0
    for index, member in enumerate(ordered_members):
        count = base + (1 if index < extra else 0)
        for point in ordered_points[cursor:cursor + count]:
            assigned.append({
                **point,
                "assigned_to": member["name"],
                "vehicle_no": member["vehicle_no"],
            })
        cursor += count
    return assigned


def allocate_hydrants_by_distribution(points, station, vehicle_count, mode):
    """지리조사 대상을 사용자가 고른 기준으로 차량/팀에 먼저 배정한다."""
    vehicle_count = max(int(vehicle_count or 1), 1)
    if not points:
        return []
    if vehicle_count == 1:
        return [{**p, "vehicle_no": 1, "assigned_to": "1팀"} for p in points]

    enriched = []
    for point in points:
        straight_km = haversine_km((station["lat"], station["lng"]), (point["lat"], point["lng"]))
        est_km = straight_km * ROAD_FACTOR
        est_min = est_km / AVG_SPEED_KMH * 60
        angle = math.atan2(point["lat"] - station["lat"], point["lng"] - station["lng"])
        enriched.append({
            **point,
            "_straight_km": straight_km,
            "_est_km": est_km,
            "_est_min": est_min,
            "_angle": angle,
        })

    assigned = []
    if mode == "전체 개수 균등":
        ordered = sorted(enriched, key=lambda p: (p["_angle"], p["_straight_km"]))
        base, extra = divmod(len(ordered), vehicle_count)
        cursor = 0
        for vehicle_no in range(1, vehicle_count + 1):
            count = base + (1 if vehicle_no <= extra else 0)
            for point in ordered[cursor:cursor + count]:
                assigned.append({**point, "vehicle_no": vehicle_no, "assigned_to": f"{vehicle_no}팀"})
            cursor += count
    else:
        if mode == "거리 km 균등":
            weight_key = "_est_km"
        elif mode == "센터 가까운 곳 많이, 먼 곳 적게":
            weight_key = "_est_km"
        else:
            weight_key = "_est_min"

        buckets = [{"load": 0.0, "count": 0, "points": []} for _ in range(vehicle_count)]
        for point in sorted(enriched, key=lambda p: p[weight_key], reverse=True):
            bucket_index = min(
                range(vehicle_count),
                key=lambda idx: (buckets[idx]["load"], buckets[idx]["count"], idx),
            )
            buckets[bucket_index]["points"].append(point)
            buckets[bucket_index]["load"] += max(point[weight_key], 0.1)
            buckets[bucket_index]["count"] += 1

        for bucket_index, bucket in enumerate(buckets, start=1):
            for point in sorted(bucket["points"], key=lambda p: (p["_angle"], p["_straight_km"])):
                assigned.append({**point, "vehicle_no": bucket_index, "assigned_to": f"{bucket_index}팀"})

    for point in assigned:
        for private_key in ("_straight_km", "_est_km", "_est_min", "_angle"):
            point.pop(private_key, None)
    return assigned


def separate_long_distance(points, station, threshold_km, on_call=None, save_calls=True,
                           should_stop=None):
    """소방서에서 실도로거리가 기준을 넘는 대상을 분리한다.

    save_calls=True면 직선거리 추정값이 기준에서 충분히 멀리 떨어진(애매하지 않은)
    대상은 API를 호출하지 않고 추정값으로 판정해 호출 횟수를 줄인다.
    """
    normal, far = [], []
    for p in points:
        straight = haversine_km((station["lat"], station["lng"]), (p["lat"], p["lng"]))
        est = straight * ROAD_FACTOR

        if should_stop and should_stop():
            # 한도 초과 — 남은 대상은 추정값으로 분류하고 API 호출은 더 하지 않는다
            (far if est > threshold_km else normal).append(
                {**p, "도로거리_km": round(est, 1)} if est > threshold_km else p
            )
            continue

        if save_calls and est < threshold_km * 0.7:
            normal.append(p)          # 확실히 가까움 — API 호출 생략
            continue
        if save_calls and est > threshold_km * 1.5:
            far.append({**p, "도로거리_km": round(est, 1)})  # 확실히 멂 — 추정값 사용
            continue

        km, _ = real_leg(station, p, on_call)   # 애매한 구간만 실제 도로거리로 확인
        if km > threshold_km:
            far.append({**p, "도로거리_km": round(km, 1)})
        else:
            normal.append(p)
    return normal, far


def separate_long_time(points, station, threshold_min, delegate_to, on_call=None,
                       should_stop=None):
    """센터 기준 실제 편도시간으로 계절순찰 대상과 원거리 위임 대상을 나눈다."""
    normal, far = [], []
    for point in points:
        if should_stop and should_stop():
            straight_km = haversine_km(
                (station["lat"], station["lng"]), (point["lat"], point["lng"])
            )
            est_km = straight_km * ROAD_FACTOR
            est_min = est_km / AVG_SPEED_KMH * 60
            far.append({
                **point,
                "도로거리_km": round(est_km, 1),
                "편도시간_분": round(est_min),
                "권장수행": f"{delegate_to} (API 한도 도달로 재확인 필요)",
            })
            continue
        km, mins = real_leg(station, point, on_call)
        if mins > threshold_min:
            far.append({
                **point,
                "도로거리_km": round(km, 1),
                "편도시간_분": round(mins),
                "권장수행": delegate_to,
            })
        else:
            normal.append(point)
    return normal, far
