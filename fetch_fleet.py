#!/usr/bin/env python3
"""从 MTD v3 API 下载车队列表和车型，保存到 fleet/ 目录。

API key 读取顺序：环境变量 MTD_API_KEY，其次脚本同目录的 .mtd_api_key 文件。

输出：
  fleet/vehicles.json        /vehicles 原始结果
  fleet/configurations.json  /vehicles/configurations 原始结果
  fleet/fleet.csv            按车号合并车型信息后的表格
"""

import csv
import json
import os
import urllib.request
from pathlib import Path

API_BASE = "https://api.mtd.dev"
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "fleet"


def load_api_key():
    key = os.environ.get("MTD_API_KEY")
    if not key:
        key_file = HERE / ".mtd_api_key"
        if not key_file.exists():
            raise SystemExit("找不到 API key：请设置 MTD_API_KEY 或创建 .mtd_api_key")
        key = key_file.read_text()
    return key.strip()


def api_get(path, key):
    req = urllib.request.Request(API_BASE + path, headers={"X-ApiKey": key, "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    if data.get("error"):
        raise SystemExit(f"{path} 返回错误: {data['error']}")
    return data["result"]


def main():
    key = load_api_key()
    vehicles = api_get("/vehicles", key)
    configs = api_get("/vehicles/configurations", key)

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "vehicles.json").write_text(json.dumps(vehicles, ensure_ascii=False, indent=2))
    (OUT_DIR / "configurations.json").write_text(json.dumps(configs, ensure_ascii=False, indent=2))

    config_by_id = {c["vehicleConfigurationId"]: c for c in configs}
    fields = ["vehicleId", "isActive", "dateInService", "year", "make", "model",
              "lengthFeet", "vehicleType", "powertrain", "vehicleConfigurationId"]
    rows = []
    for v in vehicles:
        c = config_by_id.get(v["vehicleConfigurationId"], {})
        rows.append({**{f: c.get(f, "") for f in fields}, **v})
    rows.sort(key=lambda r: (int(r["vehicleId"]) if r["vehicleId"].isdigit() else 10**9, r["vehicleId"]))

    # utf-8-sig 让 Excel 直接打开不乱码
    with open(OUT_DIR / "fleet.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in fields} for r in rows)

    print(f"车辆 {len(vehicles)} 辆，车型 {len(configs)} 种，已保存到 {OUT_DIR}/")


if __name__ == "__main__":
    main()
