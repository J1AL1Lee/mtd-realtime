#!/usr/bin/env python3
"""抓取 MTD 官网所有站点的实时到站数据，按车号汇总。

数据来源：
  站点列表  https://developer.mtd.org/gtfs/google_transit.zip  (stops.txt)
  实时数据  https://mtd.org/umbraco/api/realtime/info/?stopId=<父站点ID>

uniqueId 格式：v-<车号>_t-<trip>_b-<block>_sd-<YYYYMMDD-HHMMSS 计划时间>

用法：
  python3 mtd_vehicles.py                   # 抓一次，打印汇总并写 vehicles.json
  python3 mtd_vehicles.py --loop 120        # 每 120 秒抓一次
  python3 mtd_vehicles.py --stops IT,INDIGO # 只抓指定站点（调试用）
"""

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

GTFS_URL = "https://developer.mtd.org/gtfs/google_transit.zip"
INFO_URL = "https://mtd.org/umbraco/api/realtime/info/?stopId={}"
USER_AGENT = "Mozilla/5.0 (mtd_vehicles.py)"
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
STOPS_CACHE = CACHE_DIR / "stops.json"
STOPS_CACHE_MAX_AGE = 7 * 24 * 3600

UNIQUE_ID_RE = re.compile(
    r"^v-(?P<vehicle>[^_]*)_t-(?P<trip>.*?)_b-(?P<block>.*)_sd-(?P<sched>\d{8}-\d{6})$"
)


def http_get(url, timeout=20, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def load_stops(refresh=False):
    """返回父站点列表 [{id, name, code, lat, lon}]，带一周缓存。"""
    if (
        not refresh
        and STOPS_CACHE.exists()
        and time.time() - STOPS_CACHE.stat().st_mtime < STOPS_CACHE_MAX_AGE
    ):
        return json.loads(STOPS_CACHE.read_text())

    print("下载 GTFS 站点列表…", file=sys.stderr)
    with zipfile.ZipFile(io.BytesIO(http_get(GTFS_URL, timeout=60))) as zf:
        rows = list(csv.DictReader(io.TextIOWrapper(zf.open("stops.txt"), "utf-8-sig")))

    stops = {}
    for r in rows:
        parent = r["stop_id"].split(":")[0]
        if parent not in stops:
            stops[parent] = {
                "id": parent,
                # 子站点名带 "(NE Corner)" 之类的后缀，去掉
                "name": re.sub(r"\s*\([^)]*\)$", "", r["stop_name"]),
                "code": r["stop_code"],
                "lat": float(r["stop_lat"]),
                "lon": float(r["stop_lon"]),
            }
    result = sorted(stops.values(), key=lambda s: s["id"])
    CACHE_DIR.mkdir(exist_ok=True)
    STOPS_CACHE.write_text(json.dumps(result, ensure_ascii=False))
    return result


def fetch_stop(stop_id):
    data = http_get(INFO_URL.format(urllib.parse.quote(stop_id)))
    return json.loads(data)


def collect(stops, workers):
    """并发抓取所有站点，返回 (按车号汇总的 dict, 失败站点列表)。"""
    stop_by_id = {s["id"]: s for s in stops}
    vehicles = {}
    failed = []

    def add(stop, group, dep):
        m = UNIQUE_ID_RE.match(dep.get("uniqueId", ""))
        if not m or not m["vehicle"]:
            return  # 非实时班次没有车号
        v = vehicles.setdefault(
            m["vehicle"], {"vehicle": m["vehicle"], "blocks": set(), "routes": set(), "trips": {}}
        )
        v["blocks"].add(m["block"])
        v["routes"].add(f'{group["number"]} {group["name"]}')
        trip = v["trips"].setdefault(
            m["trip"],
            {
                "trip": m["trip"],
                "route": f'{group["number"]} {group["name"]}',
                "direction": group["direction"],
                "headsign": dep.get("headsign", ""),
                "block": m["block"],
                "stops": [],
            },
        )
        trip["stops"].append(
            {
                "stop_id": stop["id"],
                "boarding_point": group["stopId"],
                "stop_name": stop["name"],
                "lat": stop["lat"],
                "lon": stop["lon"],
                "minutes_out": dep["minutesOut"],
                "clock_time": dep["clockTime"],
                "scheduled": datetime.strptime(m["sched"], "%Y%m%d-%H%M%S").isoformat(),
            }
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_stop, s["id"]): s["id"] for s in stops}
        for i, fut in enumerate(as_completed(futures), 1):
            sid = futures[fut]
            try:
                info = fut.result()
            except Exception as e:
                failed.append({"stop_id": sid, "error": str(e)})
                continue
            if info.get("hasError"):
                failed.append({"stop_id": sid, "error": "hasError"})
            for group in info.get("departureGroups") or []:
                for dep in group.get("departures") or []:
                    if dep.get("isRealtime"):
                        add(stop_by_id[sid], group, dep)
            if i % 100 == 0:
                print(f"  {i}/{len(stops)}", file=sys.stderr)

    # 整理：trip 按最早到站排序，每个 trip 内站点按 minutes_out 排序
    out = []
    for v in vehicles.values():
        trips = sorted(v["trips"].values(), key=lambda t: min(s["minutes_out"] for s in t["stops"]))
        for t in trips:
            t["stops"].sort(key=lambda s: (s["minutes_out"], s["scheduled"]))
        nxt = trips[0]["stops"][0]
        out.append(
            {
                "vehicle": v["vehicle"],
                "routes": sorted(v["routes"]),
                "blocks": sorted(v["blocks"]),
                "current_trip": trips[0]["trip"],
                "current_route": trips[0]["route"],
                "current_direction": trips[0]["direction"],
                "current_headsign": trips[0]["headsign"],
                "next_stop": nxt,
                "trips": trips,
            }
        )
    out.sort(key=lambda v: (int(v["vehicle"]) if v["vehicle"].isdigit() else 10**9, v["vehicle"]))
    return out, failed


def print_summary(vehicles, failed, fetched_at):
    print(f"\n{fetched_at:%Y-%m-%d %H:%M:%S}  共 {len(vehicles)} 辆车有实时数据"
          + (f"，{len(failed)} 个站点抓取失败" if failed else ""))
    print(f'{"车号":<6}{"线路":<16}{"方向":<12}{"block":<22}{"下一站":<35}{"分钟":>5}  后续班次')
    for v in vehicles:
        n = v["next_stop"]
        print(
            f'{v["vehicle"]:<8}{v["current_route"]:<18}{v["current_direction"]:<14}'
            f'{", ".join(v["blocks"])[:20]:<22}{n["stop_name"][:34]:<38}{n["minutes_out"]:>5}  {len(v["trips"]) - 1}'
        )


def run_once(args):
    stops = load_stops(args.refresh_stops)
    if args.stops:
        wanted = {s.strip().upper() for s in args.stops.split(",")}
        stops = [s for s in stops if s["id"] in wanted] or [
            {"id": w, "name": w, "code": "", "lat": None, "lon": None} for w in wanted
        ]
    fetched_at = datetime.now()
    t0 = time.time()
    vehicles, failed = collect(stops, args.workers)
    print(f"抓取 {len(stops)} 个站点用时 {time.time() - t0:.1f}s", file=sys.stderr)

    result = {
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
        "stop_count": len(stops),
        "vehicle_count": len(vehicles),
        "failed_stops": failed,
        "vehicles": vehicles,
    }
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    if args.history:
        hist = Path(args.history)
        hist.mkdir(parents=True, exist_ok=True)
        (hist / f"vehicles_{fetched_at:%Y%m%d_%H%M%S}.json").write_text(
            json.dumps(result, ensure_ascii=False)
        )
    print_summary(vehicles, failed, fetched_at)


def main():
    p = argparse.ArgumentParser(description="按车号汇总 MTD 全部站点的实时到站数据")
    p.add_argument("-o", "--output", default="vehicles.json", help="输出 JSON 文件 (默认 vehicles.json)")
    p.add_argument("-w", "--workers", type=int, default=6, help="并发请求数 (默认 6)")
    p.add_argument("--loop", type=int, metavar="SECONDS", help="循环抓取间隔秒数")
    p.add_argument("--history", metavar="DIR", help="每次抓取另存一份带时间戳的快照到该目录")
    p.add_argument("--stops", help="只抓这些父站点 ID，逗号分隔")
    p.add_argument("--refresh-stops", action="store_true", help="强制重新下载 GTFS 站点列表")
    args = p.parse_args()

    if not args.loop:
        run_once(args)
        return
    while True:
        start = time.time()
        try:
            run_once(args)
        except Exception as e:
            print(f"本轮失败: {e}", file=sys.stderr)
        time.sleep(max(0, args.loop - (time.time() - start)))


if __name__ == "__main__":
    main()
