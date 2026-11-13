#!/usr/bin/env python3
"""实时查车号网页：按线路列出在线车辆及最新位置。纯文本，无依赖。

数据来源：
  mtd.org 各站点实时到站预测（mtd_vehicles.collect）  车号、线路、方向、下一站
  MTD v3 API /vehicles/locations                    GPS 坐标，仅在数据新鲜时显示
    （该接口有时会卡在旧快照上，lastUpdated 超过 GPS_MAX_AGE 就不用）

后台线程每 SCRAPE_INTERVAL 秒抓一轮全部站点（约 20 秒），网页只读内存里的最新结果。
车型取自 fleet/fleet.csv。

用法：
  python3 web.py                   # http://localhost:8000
  python3 web.py --host 0.0.0.0    # 局域网/手机可访问
"""

import argparse
import csv
import html
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import mtd_vehicles
from fetch_fleet import HERE, api_get, load_api_key

SCRAPE_INTERVAL = 60      # 两轮抓取的间隔秒数
WORKERS = 12              # 抓站点的并发数
GPS_MAX_AGE = 300         # v3 GPS 超过这么多秒就视为过期
REFRESH = 30              # 页面自动刷新秒数

API_KEY = load_api_key()
state = {"vehicles": [], "gps": {}, "fetched_at": None, "error": None}


def load_models():
    try:
        with open(HERE / "fleet" / "fleet.csv", encoding="utf-8-sig") as f:
            return {r["vehicleId"]: f'{r["year"]} {r["make"]} {r["model"]}' for r in csv.DictReader(f)}
    except OSError:
        return {}


MODELS = load_models()


def fresh_gps():
    """返回 {车号: (lat, lon)}，只含 lastUpdated 在 GPS_MAX_AGE 内的车。"""
    now = datetime.now(timezone.utc)
    out = {}
    try:
        for v in api_get("/vehicles/locations", API_KEY):
            if not v["location"]:
                continue
            # 小数秒位数不定，旧版 Python 的 fromisoformat 不认，直接去掉
            ts = datetime.fromisoformat(re.sub(r"\.\d+", "", v["lastUpdated"]))
            if (now - ts).total_seconds() < GPS_MAX_AGE:
                out[v["vehicleId"]] = (v["location"]["latitude"], v["location"]["longitude"])
    except (Exception, SystemExit) as ex:  # GPS 只是附加信息，出错不影响主数据
        print(f"GPS 获取失败: {ex}", file=sys.stderr)
    return out


def scrape_loop():
    stops = mtd_vehicles.load_stops()
    while True:
        start = time.time()
        try:
            vehicles, _ = mtd_vehicles.collect(stops, WORKERS)
            state.update(vehicles=vehicles, gps=fresh_gps(), fetched_at=datetime.now(), error=None)
            print(f"{datetime.now():%H:%M:%S} {len(vehicles)} 辆，用时 {time.time() - start:.0f}s", file=sys.stderr)
        except Exception as ex:
            state["error"] = str(ex)
            print(f"抓取失败: {ex}", file=sys.stderr)
        time.sleep(max(5, SCRAPE_INTERVAL - (time.time() - start)))


def line_of(v):
    """'22 ILLINI' -> '22'"""
    return v["current_route"].split(" ", 1)[0]


def num_key(s):
    return (int(s) if s.isdigit() else 10**9, s)


# ---------- 页面 ----------

def e(s):
    return html.escape(str(s))


def page(title, body):
    t = state["fetched_at"]
    status = f"数据时间 {t:%H:%M:%S}" if t else "首次抓取中，约 20 秒后刷新"
    if state["error"]:
        status += f"，上一轮失败：{e(state['error'])}"
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH}">
<title>{e(title)}</title>
<style>
body {{ font-family: ui-monospace, Menlo, monospace; margin: 16px; line-height: 1.5; }}
table {{ border-collapse: collapse; }}
td, th {{ padding: 2px 12px 2px 0; text-align: left; vertical-align: top; }}
.dim {{ color: #888; }}
</style></head><body>
{body}
<p class="dim">{status}，每 {SCRAPE_INTERVAL} 秒更新</p>
</body></html>"""


def search_form(q=""):
    return f'<form action="/v"><input name="id" value="{e(q)}" placeholder="车号" size="8"> <button>查车</button></form>'


def render_index():
    lines = {}
    for v in state["vehicles"]:
        ln = lines.setdefault(line_of(v), {"names": set(), "n": 0})
        ln["names"].add(v["current_route"].split(" ", 1)[-1])
        ln["n"] += 1
    rows = [f'<tr><td>{e(k)}</td><td><a href="/route/{e(k)}">{e(" / ".join(sorted(lines[k]["names"])))}</a></td>'
            f'<td>{lines[k]["n"]} 辆</td></tr>' for k in sorted(lines, key=num_key)]
    body = (f"<h1>MTD 实时车辆</h1>{search_form()}"
            f"<p>运营中 {len(state['vehicles'])} 辆</p><table>{''.join(rows)}</table>")
    return page("MTD 实时车辆", body)


def vehicle_rows(vehicles):
    rows = ['<tr><th>车号</th><th>线路</th><th>方向 / 目的地</th><th>下一站</th><th>GPS</th><th>车型</th></tr>']
    for v in sorted(vehicles, key=lambda v: num_key(v["vehicle"])):
        n = v["next_stop"]
        mins = "到站" if n["minutes_out"] == 0 else f'{n["minutes_out"]} 分钟'
        gps = state["gps"].get(v["vehicle"])
        gps = f'<a href="https://maps.google.com/?q={gps[0]},{gps[1]}">{gps[0]:.5f},{gps[1]:.5f}</a>' if gps else "-"
        rows.append(f'<tr><td><a href="/v?id={e(v["vehicle"])}">{e(v["vehicle"])}</a></td>'
                    f'<td>{e(v["current_route"])}</td>'
                    f'<td>{e(v["current_direction"])} · {e(v["current_headsign"])}</td>'
                    f'<td>{e(n["stop_name"])} <span class=dim>{mins} ({e(n["clock_time"])})</span></td>'
                    f'<td>{gps}</td><td class=dim>{e(MODELS.get(v["vehicle"], ""))}</td></tr>')
    return f"<table>{''.join(rows)}</table>"


def render_route(line):
    vehicles = [v for v in state["vehicles"] if line_of(v) == line]
    title = f"{line} 路"
    body = (f'<p><a href="/">← 全部线路</a></p><h1>{e(title)}</h1>'
            f'<p>运营中 {len(vehicles)} 辆</p>{vehicle_rows(vehicles) if vehicles else "<p>当前没有车辆</p>"}')
    return page(title, body)


def render_vehicle(vid):
    vid = vid.strip()
    found = [v for v in state["vehicles"] if v["vehicle"] == vid]
    if found:
        result = vehicle_rows(found)
    elif vid in MODELS:
        result = f"<p>{e(vid)} 当前不在运营 <span class=dim>{e(MODELS[vid])}</span></p>"
    else:
        result = f"<p>没有车号 {e(vid)}</p>"
    return page(f"车号 {vid}", f'<p><a href="/">← 全部线路</a></p>{search_form(vid)}{result}')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            status, out = 200, render_index()
        elif url.path.startswith("/route/"):
            status, out = 200, render_route(url.path[len("/route/"):])
        elif url.path == "/v":
            status, out = 200, render_vehicle(parse_qs(url.query).get("id", [""])[0])
        else:
            status, out = 404, page("404", '<p>找不到页面 <a href="/">返回</a></p>')
        data = out.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def main():
    p = argparse.ArgumentParser(description="MTD 实时查车号网页")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    threading.Thread(target=scrape_loop, daemon=True).start()
    print(f"http://{args.host}:{args.port}  （车型 {len(MODELS)} 条）", file=sys.stderr)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
