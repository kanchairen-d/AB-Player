"""FastAPI 入口"""

import time
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from .config import BASE_DIR

app = FastAPI(title="AB2 Player")

# Jinja2 模板
templates_dir = BASE_DIR / "app" / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


# 自定义模板过滤器：时间戳转日期
def timestamp_to_date(ts: int) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(ts))

templates.env.filters["timestamp_to_date"] = timestamp_to_date


# 静态文件
static_dir = BASE_DIR / "app" / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# 统一错误处理
@app.exception_handler(Exception)
async def global_exception(request: Request, exc: Exception):
    return Response(str(exc)[:500], status_code=500, media_type="text/plain")


# 注册路由
@app.on_event("startup")
async def startup():
    from . import admin, api, player, m3u, acfun
    admin.register(app)
    api.register(app)
    player.register(app)
    m3u.register(app)
    acfun.register(app)
    # 启动后台调度器
    from .scheduler import start_scheduler
    start_scheduler()


@app.get("/")
async def root():
    return {"name": "AB2 Player", "version": "1.0", "admin": "/admin", "api": "/api"}