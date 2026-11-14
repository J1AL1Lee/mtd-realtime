# MTD 实时查车号

查看 Champaign-Urbana MTD 公交当前每条线路上有哪些车（车号）、开往哪里、下一站是哪。纯 Python 标准库，无第三方依赖。

## 文件

| 文件 | 作用 |
| --- | --- |
| `web.py` | 网页：按线路列出运营中的车辆，点进去看每辆车的方向、下一站、车型；支持按车号查询 |
| `mtd_vehicles.py` | 命令行：抓取全部站点（或加 `--gap 10` 只抓抽样站点）的实时到站数据，按车号汇总写入 `vehicles.json` |
| `fetch_fleet.py` | 下载车队列表和车型，生成 `fleet/fleet.csv`（网页里的车型列用它） |
| `deploy/` | 部署用的 Caddy 配置和 systemd 服务，见下面“部署到 AWS” |

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

## 部署到 AWS（HTTPS）

`web.py` 只监听本机 8000 端口，前面用 Caddy 做 HTTPS（自动申请 Let's Encrypt 证书）。配置文件在 `deploy/`。下面以 us-east-2 的 Ubuntu 24.04 EC2（t4g.micro 足够）为例。

**1. 准备机器和域名**

- 给实例绑一个弹性 IP。
- 安全组入站：22（只放你自己的 IP）、80、443。8000 **不要**开。80 必须开，Let's Encrypt 验证和 HTTP→HTTPS 跳转都要用。
- 域名加一条 A 记录指向弹性 IP。没有域名可以直接用 `<IP 用横线连接>.sslip.io`，例如 `3-14-15-92.sslip.io`。

**2. 装 Caddy 和代码**

```sh
sudo apt update && sudo apt install -y python3 git debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

sudo useradd --system --home /opt/mtd-realtime --shell /usr/sbin/nologin mtd
sudo git clone https://github.com/J1AL1Lee/mtd-realtime.git /opt/mtd-realtime
sudo chown -R mtd:mtd /opt/mtd-realtime
```

**3. 放 API key**

```sh
sudo install -m 600 /dev/null /etc/mtd-realtime.env
sudo nano /etc/mtd-realtime.env      # 写一行 MTD_API_KEY=你的key，不加引号
```

**4. 启动**

```sh
sudo cp /opt/mtd-realtime/deploy/mtd-realtime.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mtd-realtime

sudo cp /opt/mtd-realtime/deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile       # 把 your-domain.example.com 换成你的域名
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

**5. 检查**

```sh
curl -I https://你的域名                   # 应该是 200
sudo journalctl -u mtd-realtime -f        # 每轮一行：车辆数、失败站点数、用时
sudo journalctl -u caddy -n 50            # 证书申请失败时看这里
```

失败站点数持续很高，说明可能被 mtd.org 限速或拦截了。

**更新代码**

```sh
cd /opt/mtd-realtime && sudo -u mtd git pull && sudo systemctl restart mtd-realtime
```

## 数据来源

- **车号 / 线路 / 下一站**：mtd.org 各站点的实时到站预测。后台每 3 分钟抓一轮，从中提取每辆车的当前班次。为了少给 mtd.org 添负担，只抓按时刻表抽样出来的约 130 个站点（全部约 1000 个），保证每个班次沿途每 10 分钟内至少有一个被抓的站，所以在跑的车一辆都不会漏；代价是“下一站”显示的是前方最近的抽样站点，比实际下一站平均晚 2 分钟左右。命令行想抓全部站点就不加 `--gap`。
- **GPS 坐标**：MTD v3 API `/vehicles/locations`。这个接口有时会停在旧快照上，所以只在 `lastUpdated` 是 5 分钟内的时候才显示。
- **车型**：MTD v3 API `/vehicles` 和 `/vehicles/configurations`。

只有正在跑载客班次、有实时预测的车才会出现在列表里，回场或停在车库的车不显示。

本项目与 MTD 官方无关。
