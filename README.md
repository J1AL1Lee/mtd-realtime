# MTD 实时查车号

查看 Champaign-Urbana MTD 公交当前每条线路上有哪些车（车号）、开往哪里、下一站是哪。纯 Python 标准库，无第三方依赖。

## 文件

| 文件 | 作用 |
| --- | --- |
| `web.py` | 网页：按线路列出运营中的车辆，点进去看每辆车的方向、下一站、车型；支持按车号查询 |
| `mtd_vehicles.py` | 命令行：抓取全部站点的实时到站数据，按车号汇总写入 `vehicles.json` |
| `fetch_fleet.py` | 下载车队列表和车型，生成 `fleet/fleet.csv`（网页里的车型列用它） |

## 用法

需要 Python 3.9+。

```sh
# 可选：下载车型数据（需要 MTD v3 API key）
echo 你的key > .mtd_api_key        # 或 export MTD_API_KEY=你的key
python3 fetch_fleet.py

# 启动网页，默认 http://localhost:8000
python3 web.py
python3 web.py --host 0.0.0.0     # 同一局域网的手机也能访问
```

## 数据来源

- **车号 / 线路 / 下一站**：mtd.org 各站点的实时到站预测。后台每 60 秒扫一遍全部约 1000 个站点（约 20 秒），从中提取每辆车的当前班次。
- **GPS 坐标**：MTD v3 API `/vehicles/locations`。这个接口有时会停在旧快照上，所以只在 `lastUpdated` 是 5 分钟内的时候才显示。
- **车型**：MTD v3 API `/vehicles` 和 `/vehicles/configurations`。

只有正在跑载客班次、有实时预测的车才会出现在列表里，回场或停在车库的车不显示。

本项目与 MTD 官方无关。
