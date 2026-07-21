# AB-Player

TVBox / UZ 订阅 API，支持 Bilibili、AcFun 等平台的视频源解析与播放。

## 功能

- Bilibili 视频解析（详情、搜索、UP 主视频、系列视频）
- AcFun 视频解析（专辑、用户视频）
- M3U 播放列表生成
- 多平台视频搜索
- 订阅源接口（TVBox / UZ 格式）
- 后台管理界面

## 快速部署

### Docker Compose（推荐）

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

```bash
docker compose up -d
```

### Docker 直接运行

```bash
docker run -d --name ab-player -p 5081:8080 -v ./data:/app/data -e TZ=Asia/Shanghai ghcr.io/kanchairen-d/ab-player:latest
```

## 访问后台

- **订阅接口**：`http://<你的IP>:5081/`
- **后台管理**：`http://<你的IP>:5081/admin`

## 订阅源配置

在 TVBox / UZ 等播放器中添加订阅源：

```
http://<你的IP>:5081/api
```

## 目录结构

```
app/         应用代码
  main.py    入口
  api.py     API 路由（TVBox 订阅接口）
  admin.py   后台管理
  bilibili.py    Bilibili 解析
  acfun.py       AcFun 解析
  player.py      播放器逻辑
  m3u.py         M3U 生成
  config.py      配置
  scheduler.py   任务调度
data/        数据目录（挂载卷）
Dockerfile
requirements.txt
```

## 更新

```bash
docker compose pull
docker compose up -d
```
