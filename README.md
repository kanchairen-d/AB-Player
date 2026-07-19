# AB-Player · A站B站聚合播放器-一键抓取视频

A站（AcFun）与 B站（Bilibili）视频聚合管理与播放工具。支持订阅 UP 主、番剧/合辑，一键抓取最新视频，提供 TVBox 订阅接口与 M3U 播放列表，方便在各类播放器中观看。

## 功能

- **一键抓取** — 一键抓取 A 站 / B 站 UP 主最新视频
- **UP 主订阅** — 添加 A 站、B 站 UP 主，自动抓取更新
- **番剧/合辑管理** — 支持 B 站番剧系列、A 站合辑订阅
- **视频分类** — 按 UP 主、番剧、合辑自动归类，支持自定义标签
- **TVBox 订阅** — 生成 TVBox 格式订阅接口，可在各类 TVBox 壳中播放
- **M3U 播放列表** — 导出 M3U 格式，兼容 IPTV 播放器
- **后台管理面板** — Web 管理界面，添加/编辑/删除订阅
- **自动更新调度** — 定时后台抓取最新视频

## 部署

### 方式一：Docker Compose（推荐）

```yaml
services:
  ab-player:
    image: ghcr.io/kanchairen-d/ab-player:latest
    container_name: ab-player
    ports:
      - "5081:8080"
    volumes:
      - ./data:/app/data
    restart: unless-stopped
    environment:
      - TZ=Asia/Shanghai
```

启动：

```bash
docker compose up -d
```

### 方式二：直接拉取镜像

```bash
docker pull ghcr.io/kanchairen-d/ab-player:latest

docker run -d \
  --name ab-player \
  -p 5081:8080 \
  -v ./data:/app/data \
  -e TZ=Asia/Shanghai \
  --restart unless-stopped \
  ghcr.io/kanchairen-d/ab-player:latest
```

### 方式三：源码构建

```bash
git clone https://github.com/kanchairen-d/AB-Player.git
cd AB-Player
docker compose up -d
```

## 访问

- 管理面板：`http://localhost:5081/admin`
- API 接口：`http://localhost:5081/api`
- TVBox 订阅：`http://localhost:5081/api/tvbox?t=sub`
- M3U 播放列表：`http://localhost:5081/m3u`

## 配置

数据目录 `./data/` 挂载后可持久化配置，主要文件：

- `config.json` — 订阅配置
- `schedule.json` — 调度计划
- `cache/` — 视频缓存

## 许可证

MIT