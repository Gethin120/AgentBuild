from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt
import os
import time
from typing import Any, Dict, List, Optional, Protocol, Set, Tuple
from urllib.parse import urlencode
from urllib.request import urlopen


AMAP_MIN_INTERVAL_SEC = 0.40
_amap_last_request_monotonic = 0.0


def amap_global_rate_limit_wait() -> None:
    global _amap_last_request_monotonic
    now = time.monotonic()
    elapsed = now - _amap_last_request_monotonic
    if elapsed < AMAP_MIN_INTERVAL_SEC:
        time.sleep(AMAP_MIN_INTERVAL_SEC - elapsed)
    _amap_last_request_monotonic = time.monotonic()


@dataclass(frozen=True)
class Location:
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class PlanningConstraints:
    passenger_travel_max_min: Optional[int] = None
    driver_detour_max_min: Optional[int] = None
    max_wait_min: Optional[int] = None


@dataclass(frozen=True)
class ScoringWeights:
    arrival_weight: float = 0.55
    wait_weight: float = 0.25
    detour_weight: float = 0.20


@dataclass(frozen=True)
class RendezvousRequest:
    driver_origin: Location
    passenger_origin: Location
    destination: Location
    departure_time: datetime
    pickup_candidates: List[Location]
    driver_mode: str = "driving"
    passenger_mode: str = "transit"
    passenger_departure_time: Optional[datetime] = None
    constraints: PlanningConstraints = PlanningConstraints()
    weights: ScoringWeights = ScoringWeights()
    top_n: int = 3
    preference_profile: str = "balanced"
    preference_overrides: Tuple[str, ...] = ()
    max_departure_shift_min: int = 60
    prefer_pickup_tags: Tuple[str, ...] = ()
    avoid_pickup_tags: Tuple[str, ...] = ()
    exclude_pickup_points: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RendezvousOption:
    pickup_point: Location
    travel_time_driver_to_pickup: int
    travel_time_passenger_to_pickup: int
    travel_time_pickup_to_destination: int
    travel_time_driver_direct: int
    eta_driver_to_pickup: datetime
    eta_passenger_to_pickup: datetime
    pickup_wait_time: int
    raw_wait_time: int
    optimized_wait_time: int
    departure_shift_role: str
    departure_shift_min: int
    driver_detour_time: int
    fairness_gap_time: int
    passenger_transfer_count: int
    pickup_tags: Tuple[str, ...]
    total_arrival_time: datetime
    score: float


@dataclass(frozen=True)
class FilteredCandidate:
    pickup_point: Location
    reasons: List[str]


@dataclass(frozen=True)
class PlanningDiagnostics:
    filtered_candidates: List[FilteredCandidate]


class TravelTimeProvider(Protocol):
    def estimate_minutes(
        self, origin: Location, destination: Location, mode: str, depart_at: datetime
    ) -> int:
        ...

    def estimate_details(
        self, origin: Location, destination: Location, mode: str, depart_at: datetime
    ) -> Dict[str, int]:
        ...


class MockTravelTimeProvider:
    """
    MVP mock provider. Later you can replace it with AMap/Google/Mapbox adapters.
    """

    SPEED_KMH = {
        "driving": 42.0,
        "transit": 24.0,
        "taxi": 38.0,
        "walking": 4.8,
    }

    MODE_FIXED_OVERHEAD_MIN = {
        "driving": 4,
        "transit": 10,
        "taxi": 6,
        "walking": 0,
    }

    def estimate_minutes(
        self, origin: Location, destination: Location, mode: str, depart_at: datetime
    ) -> int:
        speed = self.SPEED_KMH.get(mode, 30.0)
        overhead = self.MODE_FIXED_OVERHEAD_MIN.get(mode, 5)
        distance_km = haversine_km(origin.lat, origin.lon, destination.lat, destination.lon)

        # Tiny time-of-day traffic factor for realism in MVP.
        peak_factor = 1.18 if depart_at.hour in {8, 9, 17, 18, 19} else 1.0

        drive_minutes = (distance_km / speed) * 60.0
        total = int(round(drive_minutes * peak_factor + overhead))
        return max(total, 1)

    def estimate_details(
        self, origin: Location, destination: Location, mode: str, depart_at: datetime
    ) -> Dict[str, int]:
        minutes = self.estimate_minutes(origin, destination, mode, depart_at)
        transfer_count = 0
        if mode == "transit":
            transfer_count = max(0, min(4, minutes // 35))
        return {"minutes": minutes, "transfer_count": transfer_count}


class AMapTravelTimeProvider:
    BASE_URL = "https://restapi.amap.com"

    def __init__(self, api_key: str, timeout_sec: int = 8):
        if not api_key:
            raise ValueError("AMap api_key is required.")
        self.api_key = api_key
        self.timeout_sec = timeout_sec
        self._city_cache: Dict[Tuple[float, float], str] = {}
        self._travel_cache: Dict[Tuple[str, float, float, float, float], int] = {}

    def estimate_minutes(
        self, origin: Location, destination: Location, mode: str, depart_at: datetime
    ) -> int:
        cache_key = (mode, origin.lat, origin.lon, destination.lat, destination.lon)
        if cache_key in self._travel_cache:
            return self._travel_cache[cache_key]

        if mode == "driving":
            minutes = self._driving_minutes(origin, destination)
        elif mode == "transit":
            minutes = self._transit_minutes(origin, destination)
        else:
            raise ValueError(f"Unsupported mode for AMap provider: {mode}")

        self._travel_cache[cache_key] = minutes
        return minutes

    def estimate_details(
        self, origin: Location, destination: Location, mode: str, depart_at: datetime
    ) -> Dict[str, int]:
        if mode == "driving":
            return {
                "minutes": self.estimate_minutes(origin, destination, mode, depart_at),
                "transfer_count": 0,
            }

        city = self._infer_city_code(origin)
        cityd = self._infer_city_code(destination)
        data = self._get_json(
            "/v3/direction/transit/integrated",
            {
                "origin": f"{origin.lon},{origin.lat}",
                "destination": f"{destination.lon},{destination.lat}",
                "city": city,
                "cityd": cityd,
                "strategy": "0",
                "nightflag": "0",
                "key": self.api_key,
            },
        )
        self._assert_ok(data)
        transits = (((data.get("route") or {}).get("transits")) or [])
        if not transits:
            raise RuntimeError("AMap transit returned no path.")
        transit = transits[0] or {}
        duration_sec = int(float(transit.get("duration", 0) or 0))
        segments = transit.get("segments") or []
        line_count = 0
        for segment in segments:
            bus = (segment.get("bus") or {}).get("buslines") or []
            railway = segment.get("railway") or {}
            if bus:
                line_count += 1
            elif railway and (railway.get("name") or railway.get("trip")):
                line_count += 1
        return {
            "minutes": max(round(duration_sec / 60), 1),
            "transfer_count": max(line_count - 1, 0),
        }

    def _driving_minutes(self, origin: Location, destination: Location) -> int:
        data = self._get_json(
            "/v3/direction/driving",
            {
                "origin": f"{origin.lon},{origin.lat}",
                "destination": f"{destination.lon},{destination.lat}",
                "extensions": "base",
                "strategy": "0",
                "key": self.api_key,
            },
        )
        self._assert_ok(data)
        paths = (((data.get("route") or {}).get("paths")) or [])
        if not paths:
            raise RuntimeError("AMap driving returned no path.")
        duration_sec = int(float(paths[0]["duration"]))
        return max(round(duration_sec / 60), 1)

    def _transit_minutes(self, origin: Location, destination: Location) -> int:
        city = self._infer_city_code(origin)
        cityd = self._infer_city_code(destination)
        data = self._get_json(
            "/v3/direction/transit/integrated",
            {
                "origin": f"{origin.lon},{origin.lat}",
                "destination": f"{destination.lon},{destination.lat}",
                "city": city,
                "cityd": cityd,
                "strategy": "0",
                "nightflag": "0",
                "key": self.api_key,
            },
        )
        self._assert_ok(data)
        transits = (((data.get("route") or {}).get("transits")) or [])
        if not transits:
            raise RuntimeError("AMap transit returned no path.")
        duration_sec = int(float(transits[0]["duration"]))
        return max(round(duration_sec / 60), 1)

    def _infer_city_code(self, location: Location) -> str:
        cache_key = (location.lat, location.lon)
        if cache_key in self._city_cache:
            return self._city_cache[cache_key]

        data = self._get_json(
            "/v3/geocode/regeo",
            {
                "location": f"{location.lon},{location.lat}",
                "extensions": "base",
                "key": self.api_key,
            },
        )
        self._assert_ok(data)
        adcode = (((data.get("regeocode") or {}).get("addressComponent")) or {}).get("adcode")
        if not adcode:
            raise RuntimeError(f"Cannot infer adcode from AMap regeo for {location.name}.")
        adcode_str = str(adcode)
        self._city_cache[cache_key] = adcode_str
        return adcode_str

    def _get_json(self, path: str, params: Dict[str, str]) -> Dict[str, Any]:
        attempt = 0
        while True:
            self._respect_qps_limit()
            query = urlencode(params)
            url = f"{self.BASE_URL}{path}?{query}"
            with urlopen(url, timeout=self.timeout_sec) as resp:
                payload = resp.read().decode("utf-8")
            data = json_loads(payload)

            # 10021 is often QPS/limit related; retry with backoff.
            if str(data.get("status")) == "0" and str(data.get("infocode")) == "10021" and attempt < 3:
                attempt += 1
                time.sleep(0.6 * attempt)
                continue
            return data

    def _respect_qps_limit(self) -> None:
        amap_global_rate_limit_wait()

    @staticmethod
    def _assert_ok(data: Dict[str, Any]) -> None:
        if str(data.get("status")) != "1":
            info = data.get("info", "unknown")
            infocode = data.get("infocode", "unknown")
            raise RuntimeError(f"AMap API error: info={info}, infocode={infocode}")


class AMapGeocoder:
    BASE_URL = "https://restapi.amap.com"

    def __init__(self, api_key: str, timeout_sec: int = 8, city_hint: Optional[str] = None):
        if not api_key:
            raise ValueError("AMap api_key is required for geocoding.")
        self.api_key = api_key
        self.timeout_sec = timeout_sec
        self.city_hint = city_hint
        self._address_cache: Dict[Tuple[str, str], Location] = {}

    def geocode(self, address: str, name: str) -> Location:
        cache_key = (address.strip(), self.city_hint or "")
        if cache_key in self._address_cache:
            cached = self._address_cache[cache_key]
            return Location(name=name, lat=cached.lat, lon=cached.lon)

        params = {
            "address": address,
            "key": self.api_key,
        }
        if self.city_hint:
            params["city"] = self.city_hint

        data = self._get_json(
            "/v3/geocode/geo",
            params,
        )
        if str(data.get("status")) != "1":
            info = data.get("info", "unknown")
            infocode = data.get("infocode", "unknown")
            raise RuntimeError(f"AMap geocode error: info={info}, infocode={infocode}")

        geocodes = data.get("geocodes") or []
        if not geocodes:
            raise RuntimeError(f"Address not found: {address}")

        chosen = self._choose_best_geocode(address, geocodes)
        location = chosen.get("location")
        if not location or "," not in location:
            raise RuntimeError(f"Invalid geocode location format: {address}")

        lon_str, lat_str = location.split(",", 1)
        resolved = Location(name=name, lat=float(lat_str), lon=float(lon_str))
        self._address_cache[cache_key] = resolved
        return Location(name=name, lat=resolved.lat, lon=resolved.lon)

    def _choose_best_geocode(self, address: str, geocodes: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self.city_hint:
            return geocodes[0]

        hint = self.city_hint.strip()
        strict_matches: List[Dict[str, Any]] = []
        loose_matches: List[Dict[str, Any]] = []

        for item in geocodes:
            city = stringify_city(item.get("city"))
            province = str(item.get("province") or "")
            district = str(item.get("district") or "")
            formatted = str(item.get("formatted_address") or "")

            fields = [city, province, district, formatted]
            if any(hint == f for f in fields if f):
                strict_matches.append(item)
                continue
            if any(hint in f for f in fields if f):
                loose_matches.append(item)

        if strict_matches:
            return strict_matches[0]
        if loose_matches:
            return loose_matches[0]

        sample = geocodes[0]
        sample_city = stringify_city(sample.get("city"))
        sample_province = str(sample.get("province") or "")
        raise RuntimeError(
            "Geocode city mismatch for address "
            f"'{address}'. city_hint='{hint}', "
            f"top_result_city='{sample_city}', province='{sample_province}'. "
            "Please use a more specific address (district/road/POI)."
        )

    def _get_json(self, path: str, params: Dict[str, str]) -> Dict[str, Any]:
        attempt = 0
        while True:
            self._respect_qps_limit()
            query = urlencode(params)
            url = f"{self.BASE_URL}{path}?{query}"
            with urlopen(url, timeout=self.timeout_sec) as resp:
                payload = resp.read().decode("utf-8")
            data = json_loads(payload)

            # 10021 is often QPS/limit related; retry with backoff.
            if str(data.get("status")) == "0" and str(data.get("infocode")) == "10021" and attempt < 3:
                attempt += 1
                time.sleep(0.6 * attempt)
                continue
            return data

    def _respect_qps_limit(self) -> None:
        amap_global_rate_limit_wait()


class AMapPickupCandidateGenerator:
    BASE_URL = "https://restapi.amap.com"

    def __init__(self, api_key: str, timeout_sec: int = 8):
        if not api_key:
            raise ValueError("AMap api_key is required for candidate generation.")
        self.api_key = api_key
        self.timeout_sec = timeout_sec

    def generate_candidates(
        self,
        driver_origin: Location,
        destination: Location,
        geocode_city: Optional[str],
        sample_km: float = 4.0,
        radius_m: int = 1000,
        max_candidates: int = 30,
        keywords: str = "地铁站|公交站|停车场|商场",
    ) -> List[Location]:
        route_points = self._fetch_route_polyline_points(driver_origin, destination)
        if not route_points:
            return []

        anchors = sample_points_by_distance(route_points, step_km=sample_km, max_points=8)
        dedup: Dict[str, Location] = {}
        seen_locations: Set[Tuple[int, int]] = set()
        city = geocode_city or ""
        per_anchor_limit = max(2, max_candidates // max(len(anchors), 1))

        for lat, lon in anchors:
            pois = self._search_around(lat=lat, lon=lon, keywords=keywords, radius_m=radius_m, city=city)
            accepted_this_anchor = 0
            for poi in pois:
                loc = poi_to_location(poi)
                if loc is None:
                    continue

                # Dedup by rough coordinate bucket + POI name.
                coord_bucket = (round(loc.lat, 4), round(loc.lon, 4))
                dedup_key = f"{loc.name}|{coord_bucket[0]}|{coord_bucket[1]}"
                if dedup_key in dedup:
                    continue
                if coord_bucket in seen_locations and loc.name in dedup:
                    continue
                dedup[dedup_key] = loc
                seen_locations.add(coord_bucket)
                accepted_this_anchor += 1

                if accepted_this_anchor >= per_anchor_limit:
                    break

        return list(dedup.values())[:max_candidates]

    def _fetch_route_polyline_points(
        self, origin: Location, destination: Location
    ) -> List[Tuple[float, float]]:
        data = self._get_json(
            "/v3/direction/driving",
            {
                "origin": f"{origin.lon},{origin.lat}",
                "destination": f"{destination.lon},{destination.lat}",
                "extensions": "base",
                "strategy": "0",
                "key": self.api_key,
            },
        )
        if str(data.get("status")) != "1":
            return []

        paths = (((data.get("route") or {}).get("paths")) or [])
        if not paths:
            return []

        steps = ((paths[0] or {}).get("steps")) or []
        all_points: List[Tuple[float, float]] = []
        for step in steps:
            polyline = str(step.get("polyline") or "")
            if not polyline:
                continue
            all_points.extend(parse_polyline_points(polyline))
        return all_points

    def _search_around(
        self, lat: float, lon: float, keywords: str, radius_m: int, city: str
    ) -> List[Dict[str, Any]]:
        params = {
            "location": f"{lon},{lat}",
            "keywords": keywords,
            "radius": str(radius_m),
            "sortrule": "distance",
            "offset": "20",
            "page": "1",
            "extensions": "base",
            "key": self.api_key,
        }
        if city:
            params["city"] = city

        data = self._get_json("/v3/place/around", params)
        if str(data.get("status")) != "1":
            return []
        return data.get("pois") or []

    def _get_json(self, path: str, params: Dict[str, str]) -> Dict[str, Any]:
        attempt = 0
        while True:
            amap_global_rate_limit_wait()
            query = urlencode(params)
            url = f"{self.BASE_URL}{path}?{query}"
            with urlopen(url, timeout=self.timeout_sec) as resp:
                payload = resp.read().decode("utf-8")
            data = json_loads(payload)
            if str(data.get("status")) == "0" and str(data.get("infocode")) == "10021" and attempt < 3:
                attempt += 1
                time.sleep(0.6 * attempt)
                continue
            return data


class RendezvousPlanner:
    def __init__(self, provider: TravelTimeProvider):
        self.provider = provider

    def plan(self, request: RendezvousRequest) -> List[RendezvousOption]:
        options, _ = self.plan_with_diagnostics(request)
        return options

    def plan_with_diagnostics(
        self, request: RendezvousRequest
    ) -> Tuple[List[RendezvousOption], PlanningDiagnostics]:
        passenger_departure = request.passenger_departure_time or request.departure_time
        direct_minutes = self.provider.estimate_minutes(
            request.driver_origin,
            request.destination,
            request.driver_mode,
            request.departure_time,
        )

        options: List[RendezvousOption] = []
        filtered_candidates: List[FilteredCandidate] = []
        for pickup_point in request.pickup_candidates:
            if pickup_point.name in request.exclude_pickup_points:
                filtered_candidates.append(
                    FilteredCandidate(
                        pickup_point=pickup_point,
                        reasons=["pickup_point_excluded_by_feedback"],
                    )
                )
                continue

            pickup_tags = infer_pickup_tags(pickup_point.name)
            if request.prefer_pickup_tags and not set(request.prefer_pickup_tags).intersection(pickup_tags):
                filtered_candidates.append(
                    FilteredCandidate(
                        pickup_point=pickup_point,
                        reasons=[f"pickup_tag_not_preferred ({'|'.join(sorted(pickup_tags))})"],
                    )
                )
                continue
            if request.avoid_pickup_tags and set(request.avoid_pickup_tags).intersection(pickup_tags):
                filtered_candidates.append(
                    FilteredCandidate(
                        pickup_point=pickup_point,
                        reasons=[f"pickup_tag_avoided ({'|'.join(sorted(pickup_tags))})"],
                    )
                )
                continue

            driver_to_pickup = self.provider.estimate_minutes(
                request.driver_origin,
                pickup_point,
                request.driver_mode,
                request.departure_time,
            )
            if hasattr(self.provider, "estimate_details"):
                passenger_details = self.provider.estimate_details(
                    request.passenger_origin,
                    pickup_point,
                    request.passenger_mode,
                    passenger_departure,
                )
            else:
                passenger_details = {
                    "minutes": self.provider.estimate_minutes(
                        request.passenger_origin,
                        pickup_point,
                        request.passenger_mode,
                        passenger_departure,
                    ),
                    "transfer_count": 0,
                }
            passenger_to_pickup = int(passenger_details.get("minutes", 0) or 0)
            passenger_transfer_count = int(passenger_details.get("transfer_count", 0) or 0)

            eta_driver = request.departure_time + timedelta(minutes=driver_to_pickup)
            eta_passenger = passenger_departure + timedelta(minutes=passenger_to_pickup)
            raw_wait_min = abs(int((eta_driver - eta_passenger).total_seconds() // 60))
            departure_shift_role, departure_shift_min, optimized_wait_min = compute_wait_optimization(
                eta_driver=eta_driver,
                eta_passenger=eta_passenger,
                max_departure_shift_min=request.max_departure_shift_min,
            )

            pickup_departure = max(eta_driver, eta_passenger)
            pickup_to_destination = self.provider.estimate_minutes(
                pickup_point,
                request.destination,
                request.driver_mode,
                pickup_departure,
            )

            detour = driver_to_pickup + pickup_to_destination - direct_minutes
            total_arrival = pickup_departure + timedelta(minutes=pickup_to_destination)
            fairness_gap = abs(driver_to_pickup - passenger_to_pickup)

            reject_reasons: List[str] = []
            passenger_limit = request.constraints.passenger_travel_max_min
            detour_limit = request.constraints.driver_detour_max_min
            wait_limit = request.constraints.max_wait_min

            if passenger_limit is not None and passenger_to_pickup > passenger_limit:
                reject_reasons.append(
                    "passenger_travel_exceeded "
                    f"({passenger_to_pickup} > {passenger_limit})"
                )
            if detour_limit is not None and detour > detour_limit:
                reject_reasons.append(
                    f"driver_detour_exceeded ({detour} > {detour_limit})"
                )
            if wait_limit is not None and optimized_wait_min > wait_limit:
                reject_reasons.append(
                    f"wait_time_exceeded ({optimized_wait_min} > {wait_limit})"
                )

            if reject_reasons:
                filtered_candidates.append(
                    FilteredCandidate(pickup_point=pickup_point, reasons=reject_reasons)
                )
                continue

            score = self._compute_score(
                request=request,
                total_arrival=total_arrival,
                raw_wait_min=raw_wait_min,
                optimized_wait_min=optimized_wait_min,
                detour=detour,
                fairness_gap=fairness_gap,
                passenger_transfer_count=passenger_transfer_count,
                departure_shift_min=departure_shift_min,
            )

            options.append(
                RendezvousOption(
                    pickup_point=pickup_point,
                    travel_time_driver_to_pickup=driver_to_pickup,
                    travel_time_passenger_to_pickup=passenger_to_pickup,
                    travel_time_pickup_to_destination=pickup_to_destination,
                    travel_time_driver_direct=direct_minutes,
                    eta_driver_to_pickup=eta_driver,
                    eta_passenger_to_pickup=eta_passenger,
                    pickup_wait_time=raw_wait_min,
                    raw_wait_time=raw_wait_min,
                    optimized_wait_time=optimized_wait_min,
                    departure_shift_role=departure_shift_role,
                    departure_shift_min=departure_shift_min,
                    driver_detour_time=detour,
                    fairness_gap_time=fairness_gap,
                    passenger_transfer_count=passenger_transfer_count,
                    pickup_tags=tuple(sorted(pickup_tags)),
                    total_arrival_time=total_arrival,
                    score=round(score, 2),
                )
            )

        options.sort(key=lambda x: (x.score, x.total_arrival_time))
        return options[: request.top_n], PlanningDiagnostics(
            filtered_candidates=filtered_candidates
        )

    def _compute_score(
        self,
        *,
        request: RendezvousRequest,
        total_arrival: datetime,
        raw_wait_min: int,
        optimized_wait_min: int,
        detour: int,
        fairness_gap: int,
        passenger_transfer_count: int,
        departure_shift_min: int,
    ) -> float:
        arrival_cost = minutes_since(request.departure_time, total_arrival)
        wait_cost = optimized_wait_min if request.preference_profile == "min_wait" else raw_wait_min
        wait_cost += round(0.3 * departure_shift_min, 2)
        score = (
            request.weights.arrival_weight * arrival_cost
            + request.weights.wait_weight * wait_cost
            + request.weights.detour_weight * detour
        )
        if "low_transfer" in request.preference_overrides:
            score += passenger_transfer_count * 8.0
        if "balanced_fairness" in request.preference_overrides:
            score += fairness_gap * 0.35
        if request.preference_profile == "min_wait":
            score -= min(raw_wait_min - optimized_wait_min, 60) * 0.2
        if request.preference_profile == "min_detour":
            score += detour * 0.15
        if request.preference_profile == "fast_arrival":
            score -= arrival_cost * 0.1
        return score


def minutes_since(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def compute_wait_optimization(
    *,
    eta_driver: datetime,
    eta_passenger: datetime,
    max_departure_shift_min: int,
) -> Tuple[str, int, int]:
    delta_min = abs(int((eta_driver - eta_passenger).total_seconds() // 60))
    if delta_min <= 0:
        return "", 0, 0
    role = "driver" if eta_driver < eta_passenger else "passenger"
    shift_min = min(delta_min, max(max_departure_shift_min, 0))
    optimized_wait = max(delta_min - shift_min, 0)
    return role, shift_min, optimized_wait


def infer_pickup_tags(name: str) -> Set[str]:
    text = str(name or "")
    tags: Set[str] = set()
    if any(keyword in text for keyword in ("地铁站", "地铁", "轨交", "站")):
        tags.add("metro")
    if any(keyword in text for keyword in ("商场", "广场", "万达", "mall")):
        tags.add("mall")
    if any(keyword in text for keyword in ("停车场", "停车", "P+R")):
        tags.add("parking")
    if not tags:
        tags.add("generic")
    if "mall" in tags and "parking" not in tags:
        tags.add("parking_unfriendly")
    if "generic" in tags:
        tags.add("low_landmark_confidence")
    return tags


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_km = 6371.0
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = (
        sin(d_lat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    )
    c = 2 * asin(sqrt(a))
    return earth_radius_km * c


def parse_polyline_points(polyline: str) -> List[Tuple[float, float]]:
    points: List[Tuple[float, float]] = []
    for part in polyline.split(";"):
        raw = part.strip()
        if not raw or "," not in raw:
            continue
        lon_str, lat_str = raw.split(",", 1)
        try:
            lat = float(lat_str)
            lon = float(lon_str)
        except ValueError:
            continue
        points.append((lat, lon))
    return points


def sample_points_by_distance(
    points: List[Tuple[float, float]], step_km: float, max_points: int
) -> List[Tuple[float, float]]:
    if not points:
        return []
    if len(points) == 1:
        return points

    sampled: List[Tuple[float, float]] = [points[0]]
    accum_km = 0.0
    last = points[0]
    for current in points[1:]:
        seg_km = haversine_km(last[0], last[1], current[0], current[1])
        accum_km += seg_km
        if accum_km >= step_km:
            sampled.append(current)
            accum_km = 0.0
            if len(sampled) >= max_points:
                break
        last = current

    if sampled[-1] != points[-1] and len(sampled) < max_points:
        sampled.append(points[-1])
    return sampled[:max_points]


def poi_to_location(poi: Dict[str, Any]) -> Optional[Location]:
    name = str(poi.get("name") or "").strip()
    location = str(poi.get("location") or "")
    if not name or "," not in location:
        return None
    lon_str, lat_str = location.split(",", 1)
    try:
        lat = float(lat_str)
        lon = float(lon_str)
    except ValueError:
        return None
    return Location(name=name, lat=lat, lon=lon)


def demo_request() -> RendezvousRequest:
    departure = datetime(2026, 3, 29, 9, 0, 0)
    return RendezvousRequest(
        driver_origin=Location("Driver Origin", 31.2304, 121.4737),
        passenger_origin=Location("Passenger Origin", 31.2983, 121.4971),
        destination=Location("Destination", 31.2058, 121.4324),
        departure_time=departure,
        passenger_departure_time=departure + timedelta(minutes=5),
        pickup_candidates=[
            Location("Pickup A", 31.2520, 121.4752),
            Location("Pickup B", 31.2678, 121.4606),
            Location("Pickup C", 31.2401, 121.4488),
            Location("Pickup D", 31.2249, 121.4562),
        ],
        driver_mode="driving",
        passenger_mode="transit",
        constraints=PlanningConstraints(
            passenger_travel_max_min=90,
            driver_detour_max_min=60,
            max_wait_min=35,
        ),
        weights=ScoringWeights(
            arrival_weight=0.55,
            wait_weight=0.25,
            detour_weight=0.20,
        ),
        top_n=3,
    )


def json_loads(payload: str) -> Dict[str, Any]:
    import json

    return json.loads(payload)


def stringify_city(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def build_provider(provider_name: str, amap_key: Optional[str]) -> TravelTimeProvider:
    if provider_name == "mock":
        return MockTravelTimeProvider()
    if provider_name == "amap":
        key = amap_key or os.getenv("AMAP_WEB_SERVICE_KEY")
        if not key:
            raise ValueError("AMap provider requires --amap-key or AMAP_WEB_SERVICE_KEY.")
        return AMapTravelTimeProvider(api_key=key)
    raise ValueError(f"Unknown provider: {provider_name}")


def resolve_request_from_addresses(
    base_request: RendezvousRequest,
    amap_key: str,
    driver_origin_address: str,
    passenger_origin_address: str,
    destination_address: str,
    pickup_candidate_addresses: List[str],
    geocode_city: Optional[str] = None,
) -> RendezvousRequest:
    geocoder = AMapGeocoder(api_key=amap_key, city_hint=geocode_city)
    driver_origin = geocoder.geocode(driver_origin_address, driver_origin_address)
    passenger_origin = geocoder.geocode(passenger_origin_address, passenger_origin_address)
    destination = geocoder.geocode(destination_address, destination_address)
    pickup_candidates = [
        geocoder.geocode(address, address)
        for address in pickup_candidate_addresses
    ]

    return replace(
        base_request,
        driver_origin=driver_origin,
        passenger_origin=passenger_origin,
        destination=destination,
        pickup_candidates=pickup_candidates,
    )


def resolve_request_with_auto_pickups(
    base_request: RendezvousRequest,
    amap_key: str,
    driver_origin_address: str,
    passenger_origin_address: str,
    destination_address: str,
    geocode_city: Optional[str],
    auto_pickup_limit: int,
    auto_pickup_radius_m: int,
    auto_pickup_sample_km: float,
    auto_pickup_keywords: str,
) -> RendezvousRequest:
    geocoder = AMapGeocoder(api_key=amap_key, city_hint=geocode_city)
    driver_origin = geocoder.geocode(driver_origin_address, driver_origin_address)
    passenger_origin = geocoder.geocode(passenger_origin_address, passenger_origin_address)
    destination = geocoder.geocode(destination_address, destination_address)

    generator = AMapPickupCandidateGenerator(api_key=amap_key)
    pickup_candidates = generator.generate_candidates(
        driver_origin=driver_origin,
        destination=destination,
        geocode_city=geocode_city,
        sample_km=auto_pickup_sample_km,
        radius_m=auto_pickup_radius_m,
        max_candidates=auto_pickup_limit,
        keywords=auto_pickup_keywords,
    )
    if not pickup_candidates:
        raise RuntimeError(
            "No pickup candidates generated automatically. "
            "Try increasing --auto-pickup-radius-m or using manual --pickup-addresses."
        )

    return replace(
        base_request,
        driver_origin=driver_origin,
        passenger_origin=passenger_origin,
        destination=destination,
        pickup_candidates=pickup_candidates,
    )


def print_options(options: List[RendezvousOption]) -> None:
    if not options:
        print("No feasible pickup point under current constraints.")
        return

    for idx, option in enumerate(options, start=1):
        print(f"[{idx}] {option.pickup_point.name}")
        print(f"  score: {option.score}")
        print(f"  eta_driver_to_pickup: {option.eta_driver_to_pickup.isoformat(timespec='minutes')}")
        print(
            f"  eta_passenger_to_pickup: {option.eta_passenger_to_pickup.isoformat(timespec='minutes')}"
        )
        print(f"  pickup_wait_time: {option.pickup_wait_time} min")
        print(f"  driver_detour_time: {option.driver_detour_time} min")
        print(
            f"  total_arrival_time: {option.total_arrival_time.isoformat(timespec='minutes')}"
        )
        print()


def print_diagnostics(diagnostics: PlanningDiagnostics) -> None:
    if not diagnostics.filtered_candidates:
        print("Diagnostics: no candidate filtered by constraints.")
        return

    print("Diagnostics: filtered candidates")
    for item in diagnostics.filtered_candidates:
        joined = "; ".join(item.reasons)
        print(f"  - {item.pickup_point.name}: {joined}")


def print_request_context(request: RendezvousRequest) -> None:
    print("Resolved locations:")
    print(
        f"  driver_origin: {request.driver_origin.name} ({request.driver_origin.lat:.6f}, {request.driver_origin.lon:.6f})"
    )
    print(
        f"  passenger_origin: {request.passenger_origin.name} ({request.passenger_origin.lat:.6f}, {request.passenger_origin.lon:.6f})"
    )
    print(
        f"  destination: {request.destination.name} ({request.destination.lat:.6f}, {request.destination.lon:.6f})"
    )
    for idx, pickup in enumerate(request.pickup_candidates, start=1):
        print(f"  pickup_{idx}: {pickup.name} ({pickup.lat:.6f}, {pickup.lon:.6f})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rendezvous pickup planner MVP")
    parser.add_argument(
        "--provider",
        choices=["mock", "amap"],
        default="mock",
        help="Travel-time provider",
    )
    parser.add_argument(
        "--amap-key",
        default=None,
        help="AMap Web Service API key (or set AMAP_WEB_SERVICE_KEY)",
    )
    parser.add_argument(
        "--passenger-travel-max-min",
        type=int,
        default=None,
        help="Max passenger travel minutes to pickup point",
    )
    parser.add_argument(
        "--driver-detour-max-min",
        type=int,
        default=None,
        help="Max driver detour minutes compared with direct route",
    )
    parser.add_argument(
        "--max-wait-min",
        type=int,
        default=None,
        help="Max wait minutes at pickup point",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=None,
        help="How many ranked pickup options to return",
    )
    parser.add_argument(
        "--fallback-to-mock",
        action="store_true",
        help="Fallback to mock provider if AMap call fails",
    )
    parser.add_argument(
        "--show-diagnostics",
        action="store_true",
        help="Show why candidates were filtered out",
    )
    parser.add_argument(
        "--driver-origin-address",
        default=None,
        help="Driver origin address in plain text",
    )
    parser.add_argument(
        "--passenger-origin-address",
        default=None,
        help="Passenger origin address in plain text",
    )
    parser.add_argument(
        "--destination-address",
        default=None,
        help="Destination address in plain text",
    )
    parser.add_argument(
        "--pickup-addresses",
        default=None,
        help="Pickup candidate addresses, separated by |",
    )
    parser.add_argument(
        "--auto-pickup",
        action="store_true",
        help="Auto-generate pickup candidates along driver route",
    )
    parser.add_argument(
        "--auto-pickup-limit",
        type=int,
        default=30,
        help="Max number of auto-generated pickup candidates",
    )
    parser.add_argument(
        "--auto-pickup-radius-m",
        type=int,
        default=1000,
        help="POI search radius around sampled route anchors",
    )
    parser.add_argument(
        "--auto-pickup-sample-km",
        type=float,
        default=4.0,
        help="Distance interval for route anchor sampling",
    )
    parser.add_argument(
        "--auto-pickup-keywords",
        default="地铁站|公交站|停车场|商场",
        help="AMap place keywords for auto pickup generation",
    )
    parser.add_argument(
        "--geocode-city",
        default=None,
        help="Optional city hint for geocoding, e.g. 上海",
    )
    args = parser.parse_args()

    planner = RendezvousPlanner(provider=build_provider(args.provider, args.amap_key))
    request = demo_request()

    request_constraints = replace(
        request.constraints,
        passenger_travel_max_min=(
            args.passenger_travel_max_min
            if args.passenger_travel_max_min is not None
            else request.constraints.passenger_travel_max_min
        ),
        driver_detour_max_min=(
            args.driver_detour_max_min
            if args.driver_detour_max_min is not None
            else request.constraints.driver_detour_max_min
        ),
        max_wait_min=(
            args.max_wait_min
            if args.max_wait_min is not None
            else request.constraints.max_wait_min
        ),
    )

    request = replace(
        request,
        constraints=request_constraints,
        top_n=args.top_n if args.top_n is not None else request.top_n,
    )

    has_core_address_inputs = all(
        [
            args.driver_origin_address,
            args.passenger_origin_address,
            args.destination_address,
        ]
    )
    has_manual_pickups = bool(args.pickup_addresses)
    has_address_inputs = has_core_address_inputs and (args.auto_pickup or has_manual_pickups)

    if has_address_inputs:
        if args.provider != "amap":
            raise ValueError("Address input requires --provider amap.")
        amap_key = args.amap_key or os.getenv("AMAP_WEB_SERVICE_KEY")
        if not amap_key:
            raise ValueError("Address input requires --amap-key or AMAP_WEB_SERVICE_KEY.")
        if args.auto_pickup:
            request = resolve_request_with_auto_pickups(
                base_request=request,
                amap_key=amap_key,
                driver_origin_address=args.driver_origin_address,
                passenger_origin_address=args.passenger_origin_address,
                destination_address=args.destination_address,
                geocode_city=args.geocode_city,
                auto_pickup_limit=args.auto_pickup_limit,
                auto_pickup_radius_m=args.auto_pickup_radius_m,
                auto_pickup_sample_km=args.auto_pickup_sample_km,
                auto_pickup_keywords=args.auto_pickup_keywords,
            )
        else:
            pickup_addresses = [x.strip() for x in args.pickup_addresses.split("|") if x.strip()]
            if not pickup_addresses:
                raise ValueError("--pickup-addresses must include at least one address.")
            request = resolve_request_from_addresses(
                base_request=request,
                amap_key=amap_key,
                driver_origin_address=args.driver_origin_address,
                passenger_origin_address=args.passenger_origin_address,
                destination_address=args.destination_address,
                pickup_candidate_addresses=pickup_addresses,
                geocode_city=args.geocode_city,
            )

    if args.provider == "amap" and args.fallback_to_mock:
        try:
            options, diagnostics = planner.plan_with_diagnostics(request)
            print_options(options)
            if args.show_diagnostics:
                if has_address_inputs:
                    print_request_context(request)
                print_diagnostics(diagnostics)
        except Exception as exc:
            print(f"AMap failed: {exc}")
            print("Fallback to mock provider.")
            fallback_planner = RendezvousPlanner(provider=MockTravelTimeProvider())
            options, diagnostics = fallback_planner.plan_with_diagnostics(request)
            print_options(options)
            if args.show_diagnostics:
                if has_address_inputs:
                    print_request_context(request)
                print_diagnostics(diagnostics)
    else:
        options, diagnostics = planner.plan_with_diagnostics(request)
        print_options(options)
        if args.show_diagnostics:
            if has_address_inputs:
                print_request_context(request)
            print_diagnostics(diagnostics)
