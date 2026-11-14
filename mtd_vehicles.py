#!/usr/bin/env python3
"""抓取 MTD 官网所有站点的实时到站数据，按车号汇总。

数据来源：
  站点列表  https://developer.mtd.org/gtfs/google_transit.zip  (stops.txt)
  实时数据  https://mtd.org/umbraco/api/realtime/info/?stopId=<父站点ID>

uniqueId 格式：v-<车号>_t-<trip>_b-<block>_sd-<YYYYMMDD-HHMMSS 计划时间>

用法：
  python3 mtd_vehicles.py                   # 抓一次，打印汇总并写 vehicles.json
  python3 mtd_vehicles.py --loop 120        # 每 120 秒抓一次
  python3 mtd_vehicles.py --gap 10          # 只抓抽样站点（约 130 个），见 sample_stops
  python3 mtd_vehicles.py --stops IT,INDIGO # 只抓指定站点（调试用）
"""

import argparse
import csv
import heapq
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
GTFS_CACHE = CACHE_DIR / "google_transit.zip"
GTFS_CACHE_MAX_AGE = 7 * 24 * 3600

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


def load_gtfs(refresh=False):
    """返回 GTFS 压缩包（ZipFile），本地缓存一周。"""
    if (
        refresh
        or not GTFS_CACHE.exists()
        or time.time() - GTFS_CACHE.stat().st_mtime > GTFS_CACHE_MAX_AGE
    ):
        print("下载 GTFS…", file=sys.stderr)
        data = http_get(GTFS_URL, timeout=60)
        CACHE_DIR.mkdir(exist_ok=True)
        tmp = GTFS_CACHE.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(GTFS_CACHE)  # 先写临时文件再替换，下载中断不会留下坏的缓存
    return zipfile.ZipFile(GTFS_CACHE)


def read_gtfs_csv(zf, name):
    with zf.open(name) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")))


def load_stops(refresh=False):
    """返回父站点列表 [{id, name, code, lat, lon}]。"""
    with load_gtfs(refresh) as zf:
        rows = read_gtfs_csv(zf, "stops.txt")

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
    return sorted(stops.values(), key=lambda s: s["id"])


def sample_stops(stops, gap):
    """从 stops 里挑一小部分站点来抓，仍能看到所有在跑的车。

    mtd.org 每个站点只预测约 60 分钟内到站的车。只要每个班次沿途每 gap 分钟内
    都有一个被选中的站（外加终点站），任何一辆在跑的车前方 2*gap 分钟内必有一个
    被选中的站，也就一定会出现在抓取结果里。gap 越大抓的站越少，但“下一站”
    显示的是前方最近的被选站点，离车的实际位置也越远。

    用贪心集合覆盖：每次选能补上最多“班次 × 时间段”空缺的站点。
    """

    def minutes(hms):
        h, m, _ = hms.split(":")
        return int(h) * 60 + int(m)

    with load_gtfs() as zf:
        rows = read_gtfs_csv(zf, "stop_times.txt")
    trips = {}
    for r in rows:
        t = r["arrival_time"] or r["departure_time"]
        if t:
            trips.setdefault(r["trip_id"], []).append(
                (int(r["stop_sequence"]), minutes(t), r["stop_id"].split(":")[0])
            )

    covers = {}  # 站点 -> 它能覆盖的 {(班次序号, 时间段)}
    for i, visits in enumerate(trips.values()):
        visits.sort()
        start = visits[0][1]
        for _, t, sid in visits:
            covers.setdefault(sid, set()).add((i, (t - start) // gap))
        covers[visits[-1][2]].add((i, "end"))  # 终点站：车跑完最后一段之前都能在这里看到它

    # 惰性贪心：堆里的覆盖数可能过期，取出时重算，仍是最大的才选
    left = set().union(*covers.values())
    heap = [(-len(c), sid) for sid, c in covers.items()]
    heapq.heapify(heap)
    chosen = set()
    while left:
        _, sid = heapq.heappop(heap)
        gain = len(covers[sid] & left)
        if heap and gain < -heap[0][0]:
            heapq.heappush(heap, (-gain, sid))
            continue
        chosen.add(sid)
        left -= covers[sid]
    return [s for s in stops if s["id"] in chosen]


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
    elif args.gap:
        stops = sample_stops(stops, args.gap)
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
    p.add_argument("--gap", type=int, metavar="MINUTES",
                   help="只抓抽样站点：每个班次沿途每这么多分钟至少一个站 (web.py 用 10)")
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
