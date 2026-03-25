#!/usr/bin/env python3
"""
GPX Elevation Profile Resampler — Trail & Co Pipeline
======================================================
Decisions locked in session: 2026-03-07

PIPELINE (in order):
  1. Rolling median smooth  (window=5)  — kills GPS elevation jitter
  2. Pin named WHW waypoints            — coord-snapped to nearest GPX point
  3. Pin prominent peaks & troughs      — prominence ≥ 30m, separation ≥ 0.8 km
  4. Douglas-Peucker on smoothed curve  — shape-preserving downsample
  5. Force pins into DP result          — summit/waypoint indices always retained
  6. Restore original (raw) elevation   — at pinned points only, to preserve true heights
  7. Binary-search epsilon              — to hit target point count (default: 30–40)
  8. Clip to truncation distance        — if hike is shortened to an intermediate stop
  9. Compute ascent/descent metrics     — on final resampled profile

OUTPUT: {input_stem}_resampled.gpx  per input file
  Standard GPX 1.1 route file (<rte>/<rtept>).
  Named/prominent pins carry a <name> tag.
  Pipeline metrics written into <desc> on the <rte> element.

USAGE:
  # Single file, full route
  python3 gpx_resample.py drymen-rowardennan.gpx

  # Single file, truncated at 15 km
  python3 gpx_resample.py drymen-rowardennan.gpx --truncate 15.0

  # Multiple files, some truncated
  python3 gpx_resample.py file1.gpx file2.gpx --truncate 0 18.5
  # (0 = full route for file1, 18.5 km for file2)

  # Override target point count
  python3 gpx_resample.py drymen-rowardennan.gpx --target-min 15 --target-max 19
"""

import xml.etree.ElementTree as ET
import math
import sys
import os
import argparse


# ─── WHW Named Waypoints ──────────────────────────────────────────────────────
# Add/edit stages as needed. Each entry is snapped to the nearest GPX point at
# runtime, so coordinates only need to be approximately correct (~500m is fine).
# Structure: stage_key → list of waypoints in order along the route.

WHW_WAYPOINTS = {
    # Stage 1: Milngavie → Drymen
    "milngavie-drymen": [
        {"name": "Milngavie",          "lat": 55.94200, "lon": -4.31400},
        {"name": "Craigallian Loch",   "lat": 55.97200, "lon": -4.33600},
        {"name": "Carbeth",            "lat": 55.98900, "lon": -4.36800},
        {"name": "Dumgoyne",           "lat": 56.01500, "lon": -4.38900},
        {"name": "Gartness",           "lat": 56.04500, "lon": -4.42800},
        {"name": "Drymen",             "lat": 56.07080, "lon": -4.44510},
    ],
    # Stage 2: Drymen → Rowardennan
    "drymen-rowardennan": [
        {"name": "Drymen",             "lat": 56.07080, "lon": -4.44510},
        {"name": "Garadhban Forest",   "lat": 56.08500, "lon": -4.47500},
        {"name": "Conic Hill",         "lat": 56.09855, "lon": -4.52395},
        {"name": "Balmaha",            "lat": 56.08820, "lon": -4.55940},
        {"name": "Milarrochy Bay",     "lat": 56.10200, "lon": -4.58200},
        {"name": "Rowardennan",        "lat": 56.14150, "lon": -4.63600},
    ],
    # Stage 3: Rowardennan → Inversnaid
    "rowardennan-inversnaid": [
        {"name": "Rowardennan",        "lat": 56.14150, "lon": -4.63600},
        {"name": "Ptarmigan Lodge",    "lat": 56.17500, "lon": -4.65200},
        {"name": "Rob Roy's Cave",     "lat": 56.22500, "lon": -4.68000},
        {"name": "Inversnaid",         "lat": 56.24400, "lon": -4.69500},
    ],
    # Stage 4: Inversnaid → Inverarnan
    "inversnaid-inverarnan": [
        {"name": "Inversnaid",         "lat": 56.24400, "lon": -4.69500},
        {"name": "Doune Byre",         "lat": 56.28500, "lon": -4.69000},
        {"name": "Inverarnan",         "lat": 56.32500, "lon": -4.71500},
    ],
    # Combined: Rowardennan → Inverarnan (long day skipping Inversnaid stop)
    "rowardennan-inverarnan": [
        {"name": "Rowardennan",        "lat": 56.14953, "lon": -4.64109},
        {"name": "Ptarmigan Lodge",    "lat": 56.17500, "lon": -4.65200},
        {"name": "Rob Roy's Cave",     "lat": 56.22500, "lon": -4.68000},
        {"name": "Inversnaid",         "lat": 56.24400, "lon": -4.69500},
        {"name": "Doune Byre",         "lat": 56.28500, "lon": -4.69000},
        {"name": "Inverarnan",         "lat": 56.32986, "lon": -4.71685},
    ],
    # Stage 5: Inverarnan → Tyndrum
    "inverarnan-tyndrum": [
        {"name": "Inverarnan",         "lat": 56.32500, "lon": -4.71500},
        {"name": "Crianlarich",        "lat": 56.38900, "lon": -4.61500},
        {"name": "Kirkton Farm",       "lat": 56.39500, "lon": -4.60000},
        {"name": "Tyndrum",            "lat": 56.43400, "lon": -4.72200},
    ],
    # Stage 6: Tyndrum → Bridge of Orchy
    "tyndrum-bridge-of-orchy": [
        {"name": "Tyndrum",            "lat": 56.43400, "lon": -4.72200},
        {"name": "Bridge of Orchy",    "lat": 56.53400, "lon": -4.76800},
    ],
    # Sub-stage: Tyndrum → Inveroran
    "tyndrum-inveroran": [
        {"name": "Tyndrum",            "lat": 56.43838, "lon": -4.71356},
        {"name": "Bridge of Orchy",    "lat": 56.53400, "lon": -4.76800},
        {"name": "Inveroran",          "lat": 56.53273, "lon": -4.80760},
    ],
    # Sub-stage: Inveroran → Kingshouse
    "inveroran-kingshouse": [
        {"name": "Inveroran",          "lat": 56.53273, "lon": -4.80762},
        {"name": "Victoria Bridge",    "lat": 56.54800, "lon": -4.86200},
        {"name": "Ba Bridge",          "lat": 56.57500, "lon": -4.88200},
        {"name": "Kingshouse Hotel",   "lat": 56.65152, "lon": -4.84053},
    ],
    # Stage 7: Bridge of Orchy → Kingshouse
    "bridge-of-orchy-kingshouse": [
        {"name": "Bridge of Orchy",    "lat": 56.53400, "lon": -4.76800},
        {"name": "Inveroran",          "lat": 56.54500, "lon": -4.82200},
        {"name": "Victoria Bridge",    "lat": 56.54800, "lon": -4.86200},
        {"name": "Ba Bridge",          "lat": 56.57500, "lon": -4.88200},
        {"name": "Kingshouse Hotel",   "lat": 56.65152, "lon": -4.84053},
    ],
    # Combined: Tyndrum → Kingshouse
    "tyndrum-kingshouse": [
        {"name": "Tyndrum",            "lat": 56.43838, "lon": -4.71356},
        {"name": "Bridge of Orchy",    "lat": 56.53400, "lon": -4.76800},
        {"name": "Inveroran",          "lat": 56.53273, "lon": -4.80760},
        {"name": "Victoria Bridge",    "lat": 56.54800, "lon": -4.86200},
        {"name": "Ba Bridge",          "lat": 56.57500, "lon": -4.88200},
        {"name": "Kingshouse Hotel",   "lat": 56.65152, "lon": -4.84053},
    ],
    # Stage 8: Kingshouse → Kinlochleven
    "kingshouse-kinlochleven": [
        {"name": "Kingshouse Hotel",   "lat": 56.65152, "lon": -4.84053},
        {"name": "Devil's Staircase",  "lat": 56.67200, "lon": -4.93500},
        {"name": "Kinlochleven",       "lat": 56.71300, "lon": -4.95500},
    ],
    # Stage 9: Kinlochleven → Fort William
    "kinlochleven-fort-william": [
        {"name": "Kinlochleven",       "lat": 56.71300, "lon": -4.95500},
        {"name": "Lairigmor",          "lat": 56.75500, "lon": -5.02500},
        {"name": "Lundavra",           "lat": 56.79500, "lon": -5.06000},
        {"name": "Fort William",       "lat": 56.81900, "lon": -5.10500},
    ],
}

# Fallback: if no stage key matches, use all unique waypoints across all stages
_ALL_WHW_WAYPOINTS = [wp for stage in WHW_WAYPOINTS.values() for wp in stage]


# ─── Core helpers ────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def rolling_median(values, window=5):
    half = window // 2
    result = []
    for i in range(len(values)):
        lo, hi = max(0, i - half), min(len(values), i + half + 1)
        w = sorted(values[lo:hi])
        result.append(w[len(w) // 2])
    return result


def douglas_peucker(points, epsilon):
    """
    points: list of (x, y) tuples.
    Returns sorted list of indices into points[] to retain.
    """
    def perp_dist(p, ls, le):
        x0, y0 = p
        x1, y1 = ls
        x2, y2 = le
        dx, dy = x2 - x1, y2 - y1
        if dx == 0 and dy == 0:
            return math.hypot(x0 - x1, y0 - y1)
        t = max(0.0, min(1.0, ((x0-x1)*dx + (y0-y1)*dy) / (dx*dx + dy*dy)))
        return math.hypot(x0 - (x1 + t*dx), y0 - (y1 + t*dy))

    def dpr(s, e):
        if e - s < 2:
            return []
        max_d, max_i = 0, s
        for i in range(s + 1, e):
            d = perp_dist(points[i], points[s], points[e])
            if d > max_d:
                max_d, max_i = d, i
        if max_d > epsilon:
            return dpr(s, max_i) + [max_i] + dpr(max_i, e)
        return []

    return sorted([0] + dpr(0, len(points) - 1) + [len(points) - 1])


def find_prominent_peaks(elevs, dist_km, min_prominence=30, min_sep_km=0.8):
    n = len(elevs)
    candidates = []
    for i in range(1, n - 1):
        if elevs[i] <= elevs[i-1] or elevs[i] <= elevs[i+1]:
            continue
        min_l = elevs[i]
        for j in range(i-1, -1, -1):
            if elevs[j] > elevs[i]: break
            min_l = min(min_l, elevs[j])
        min_r = elevs[i]
        for j in range(i+1, n):
            if elevs[j] > elevs[i]: break
            min_r = min(min_r, elevs[j])
        prom = elevs[i] - max(min_l, min_r)
        if prom >= min_prominence:
            candidates.append((i, prom))
    candidates.sort(key=lambda x: -x[1])
    filtered = []
    for idx, prom in candidates:
        if not any(abs(dist_km[idx] - dist_km[fi]) < min_sep_km for fi, _ in filtered):
            filtered.append((idx, prom))
    return filtered  # [(index, prominence), ...]


def find_prominent_troughs(elevs, dist_km, min_prominence=30, min_sep_km=0.8):
    n = len(elevs)
    candidates = []
    for i in range(1, n - 1):
        if elevs[i] >= elevs[i-1] or elevs[i] >= elevs[i+1]:
            continue
        max_l = elevs[i]
        for j in range(i-1, -1, -1):
            if elevs[j] < elevs[i]: break
            max_l = max(max_l, elevs[j])
        max_r = elevs[i]
        for j in range(i+1, n):
            if elevs[j] < elevs[i]: break
            max_r = max(max_r, elevs[j])
        prom = min(max_l, max_r) - elevs[i]
        if prom >= min_prominence:
            candidates.append((i, prom))
    candidates.sort(key=lambda x: -x[1])
    filtered = []
    for idx, prom in candidates:
        if not any(abs(dist_km[idx] - dist_km[fi]) < min_sep_km for fi, _ in filtered):
            filtered.append((idx, prom))
    return filtered


def ascent_descent(elev_list):
    a = sum(max(0, elev_list[i+1] - elev_list[i]) for i in range(len(elev_list)-1))
    d = sum(max(0, elev_list[i] - elev_list[i+1]) for i in range(len(elev_list)-1))
    return round(a), round(d)


# ─── Main pipeline ───────────────────────────────────────────────────────────

def load_gpx(path):
    tree = ET.parse(path)
    root = tree.getroot()
    ns = {'gpx': 'http://www.topografix.com/GPX/1/1'}
    pts = root.findall('.//gpx:trkpt', ns) or root.findall('.//gpx:rtept', ns)
    if not pts:
        raise ValueError(f"No trkpt or rtept points found in {path}")
    elevs = [float(p.find('gpx:ele', ns).text) for p in pts]
    lats  = [float(p.get('lat')) for p in pts]
    lons  = [float(p.get('lon')) for p in pts]
    return lats, lons, elevs


def build_cum_dist(lats, lons):
    cum = [0.0]
    for i in range(len(lats) - 1):
        cum.append(cum[-1] + haversine(lats[i], lons[i], lats[i+1], lons[i+1]))
    return cum  # in metres


def process_gpx(
    path,
    truncate_km=None,
    target_min=30,
    target_max=40,
    smooth_window=5,
    peak_prominence=30,
    peak_sep_km=0.8,
    verbose=True,
):
    stem = os.path.splitext(os.path.basename(path))[0]
    lats, lons, elevs_raw = load_gpx(path)
    cum_m = build_cum_dist(lats, lons)
    dist_km = [d / 1000 for d in cum_m]
    n = len(elevs_raw)

    # ── Clip to truncation point ─────────────────────────────────────────────
    if truncate_km and truncate_km > 0:
        clip_idx = next((i for i in range(n) if dist_km[i] >= truncate_km), n - 1)
        lats      = lats[:clip_idx+1]
        lons      = lons[:clip_idx+1]
        elevs_raw = elevs_raw[:clip_idx+1]
        dist_km   = dist_km[:clip_idx+1]
        n         = len(elevs_raw)
        effective_km = dist_km[-1]
        if verbose:
            print(f"  Truncated at {truncate_km} km → using {n} points ({effective_km:.2f} km)")
    else:
        effective_km = dist_km[-1]
        truncate_km = None

    # ── Step 1: Smooth ───────────────────────────────────────────────────────
    elevs_smooth = rolling_median(elevs_raw, window=smooth_window)

    # ── Step 2: Build pin set ────────────────────────────────────────────────
    # Attempt to match stage key from filename
    stage_key = stem.lower().replace('_', '-')
    waypoints = WHW_WAYPOINTS.get(stage_key, None)
    if waypoints is None:
        # Fall back: try any stage whose key is a substring of the filename
        for key, wps in WHW_WAYPOINTS.items():
            if all(part in stage_key for part in key.split('-')[:2]):
                waypoints = wps
                break
    if waypoints is None:
        waypoints = _ALL_WHW_WAYPOINTS
        if verbose:
            print(f"  No exact WHW stage match for '{stem}'; using all WHW waypoints as fallback")

    pin_labels = {}  # idx → label

    for wp in waypoints:
        idx = min(range(n), key=lambda i: haversine(wp['lat'], wp['lon'], lats[i], lons[i]))
        snap_m = haversine(wp['lat'], wp['lon'], lats[idx], lons[idx])
        # Only pin if snap distance is reasonable (< 2 km — avoids nonsensical snaps)
        if snap_m < 2000:
            pin_labels[idx] = wp['name']

    for idx, prom in find_prominent_peaks(elevs_smooth, dist_km, peak_prominence, peak_sep_km):
        if idx not in pin_labels:
            pin_labels[idx] = f"peak_{dist_km[idx]:.1f}km_{elevs_raw[idx]:.0f}m"

    for idx, prom in find_prominent_troughs(elevs_smooth, dist_km, peak_prominence, peak_sep_km):
        if idx not in pin_labels:
            pin_labels[idx] = f"col_{dist_km[idx]:.1f}km_{elevs_raw[idx]:.0f}m"

    # Always pin start and end
    pin_labels[0]   = pin_labels.get(0,   f"start_{elevs_raw[0]:.0f}m")
    pin_labels[n-1] = pin_labels.get(n-1, f"end_{elevs_raw[n-1]:.0f}m")

    if verbose:
        print(f"  Pins ({len(pin_labels)}): {', '.join(v for _, v in sorted(pin_labels.items()))}")

    # ── Steps 3–4: DP with binary-search epsilon to hit target count ─────────
    # DP coordinate space: x = dist_km (km), y = elev * 0.01 (so 100m elev ≈ 1 km)
    # This scaling gives elevation changes appropriate visual weight vs. distance.
    dp_points = list(zip(dist_km, [e * 0.01 for e in elevs_smooth]))
    pin_set = set(pin_labels.keys())

    def run_dp(epsilon):
        dp_idx = set(douglas_peucker(dp_points, epsilon))
        return sorted(dp_idx | pin_set)

    # Binary search for epsilon that yields point count in [target_min, target_max]
    lo, hi = 1e-6, 1.0
    best_idx = run_dp(lo)
    for _ in range(40):
        mid = (lo + hi) / 2
        idx_candidate = run_dp(mid)
        cnt = len(idx_candidate)
        if target_min <= cnt <= target_max:
            best_idx = idx_candidate
            break
        elif cnt < target_min:
            hi = mid
        else:
            lo = mid
        best_idx = idx_candidate  # keep closest attempt

    # ── Step 5: Restore raw elevations at pinned points ──────────────────────
    # DP ran on smoothed elevations; for named/prominent points we want true heights.
    profile_elevs = []
    for idx in best_idx:
        if idx in pin_set:
            profile_elevs.append(elevs_raw[idx])   # true elevation
        else:
            profile_elevs.append(elevs_smooth[idx]) # smoothed (noise-reduced)

    # ── Step 6: Metrics ──────────────────────────────────────────────────────
    total_asc, total_desc = ascent_descent(profile_elevs)

    # ── Step 7: Assemble GPX output ──────────────────────────────────────────
    point_count = len(best_idx)
    desc = (
        f"Resampled from {os.path.basename(path)} | "
        f"{effective_km:.2f} km"
        + (f" (truncated at {truncate_km:.2f} km)" if truncate_km else "")
        + f" | ascent={total_asc}m descent={total_desc}m | {point_count} pts"
    )

    gpx_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="gpx_resample.py"',
        '     xmlns="http://www.topografix.com/GPX/1/1"',
        '     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '     xsi:schemaLocation="http://www.topografix.com/GPX/1/1 '
        'http://www.topografix.com/GPX/1/1/gpx.xsd">',
        '  <rte>',
        f'    <name>{stem}_resampled</name>',
        f'    <desc>{desc}</desc>',
    ]

    for i, idx in enumerate(best_idx):
        elev = round(profile_elevs[i], 1)
        lat  = lats[idx]
        lon  = lons[idx]
        label = pin_labels.get(idx)
        gpx_lines.append(f'    <rtept lat="{lat}" lon="{lon}">')
        gpx_lines.append(f'      <ele>{elev}</ele>')
        if label:
            gpx_lines.append(f'      <name>{label}</name>')
        gpx_lines.append('    </rtept>')

    gpx_lines += ['  </rte>', '</gpx>']
    gpx_content = '\n'.join(gpx_lines)

    if verbose:
        print(f"  → {point_count} pts | ascent={total_asc}m | descent={total_desc}m | {effective_km:.2f} km")

    return stem, gpx_content


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Resample GPX elevation profiles.")
    parser.add_argument("files", nargs="+", help="GPX file paths")
    parser.add_argument(
        "--truncate", nargs="*", type=float, default=None,
        metavar="KM",
        help="Truncation distance(s) in km, one per file. Use 0 for full route."
    )
    parser.add_argument("--target-min", type=int, default=30)
    parser.add_argument("--target-max", type=int, default=40)
    parser.add_argument("--output-dir", default=".", help="Directory for output JSON files")
    args = parser.parse_args()

    truncations = args.truncate or []
    # Pad truncations list to match number of files (0 = full route)
    while len(truncations) < len(args.files):
        truncations.append(0)

    os.makedirs(args.output_dir, exist_ok=True)

    for fpath, trunc_km in zip(args.files, truncations):
        print(f"\nProcessing: {fpath}" + (f" (truncate at {trunc_km} km)" if trunc_km else " (full route)"))
        try:
            stem, gpx_content = process_gpx(
                fpath,
                truncate_km=trunc_km if trunc_km > 0 else None,
                target_min=args.target_min,
                target_max=args.target_max,
            )
            out_name = f"{stem}_resampled.gpx"
            out_path = os.path.join(args.output_dir, out_name)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(gpx_content)
            print(f"  Saved → {out_path}")
        except Exception as e:
            print(f"  ERROR: {e}")
            raise

    print("\nDone.")


if __name__ == "__main__":
    main()
