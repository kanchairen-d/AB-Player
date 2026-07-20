"""后台调度器 — 定时刷新合集/UP主/单视频/直播缓存"""

import json, threading, time as _time
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path("/app")
DATA_DIR = BASE_DIR / "data"
SCHEDULE_FILE = DATA_DIR / "schedule.json"

_scheduler_running = False
_refresh_lock = threading.Lock()


def load_schedule() -> dict:
    """读取调度配置"""
    if SCHEDULE_FILE.exists():
        try:
            return json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "enabled": True,
        "mode": "daily",
        "time": "04:00",
        "interval_hours": 4,
        "last_run": None,
        "last_status": "",
    }


def save_schedule(cfg: dict):
    SCHEDULE_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def start_scheduler():
    """启动调度器（后台线程）"""
    global _scheduler_running
    if _scheduler_running:
        return
    _scheduler_running = True
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="ab-scheduler")
    t.start()
    print("[Scheduler] 已启动")


def _scheduler_loop():
    """调度器主循环 — 每 60 秒检查一次"""
    cleanup_counter = 0
    while _scheduler_running:
        try:
            cfg = load_schedule()
            if cfg.get("enabled", True):
                _check_and_run(cfg)
            # 每 30 分钟清理一次过期缓存文件
            cleanup_counter += 1
            if cleanup_counter >= 30:  # 30 * 60s = 30分钟
                cleanup_counter = 0
                _cleanup_expired_cache()
        except Exception:
            pass
        _time.sleep(60)


def _check_and_run(cfg: dict):
    """检查是否该执行刷新"""
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    mode = cfg.get("mode", "daily")
    last_run = cfg.get("last_run")

    should_run = False

    if mode == "daily":
        target_time = cfg.get("time", "04:00")
        if time_str == target_time:
            if not last_run or not last_run.startswith(today_str):
                should_run = True

    elif mode == "interval":
        interval_h = int(cfg.get("interval_hours", 4))
        if last_run:
            try:
                last = datetime.fromisoformat(last_run)
                if (now - last) >= timedelta(hours=interval_h):
                    should_run = True
            except Exception:
                should_run = True
        else:
            should_run = True

    if should_run:
        _do_refresh(cfg)


def _cleanup_expired_cache():
    """清理过期的缓存文件（超过 24 小时无访问的）"""
    from .config import CACHE_DIR
    now = _time.time()
    count = 0
    for f in CACHE_DIR.iterdir():
        if f.is_file():
            if now - f.stat().st_mtime > 86400:
                f.unlink()
                count += 1
    if count:
        print(f"[Scheduler] 清理了 {count} 个过期缓存文件")


def _do_refresh(cfg: dict):
    """执行刷新所有合集、UP主、单视频、直播（B站 + A站）"""
    if not _refresh_lock.acquire(blocking=False):
        print("[Scheduler] 刷新已在运行中，跳过")
        return
    try:
        cfg["last_run"] = datetime.now().isoformat()
        cfg["last_status"] = "运行中..."
        save_schedule(cfg)

        import asyncio
        import httpx
        from .config import _HTTPX_LIMITS

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            results = loop.run_until_complete(_do_refresh_async())
            errors, total_series, total_ups, total_videos, total_rooms, total_albums, total_acfun_ups, total_acfun_videos = results
        finally:
            # 关闭全局 httpx 客户端连接，再重建，避免下次 Event loop is closed
            import app.config as config_module
            try:
                loop.run_until_complete(config_module._HTTPX_CLIENT.aclose())
            except Exception:
                pass
            loop.close()
            config_module._HTTPX_CLIENT = httpx.AsyncClient(
                limits=_HTTPX_LIMITS,
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=True,
            )

        # ═══ 状态更新 ═══
        parts = []
        if total_albums:
            parts.append(f"A站合辑: {total_albums}")
        if total_acfun_ups:
            parts.append(f"A站UP主: {total_acfun_ups}")
        if total_acfun_videos:
            parts.append(f"A站单视频: {total_acfun_videos}")
        if total_series:
            parts.append(f"B站合集: {total_series}")
        if total_ups:
            parts.append(f"B站UP主: {total_ups}")
        if total_videos:
            parts.append(f"B站单视频: {total_videos}")
        if total_rooms:
            parts.append(f"B站直播: {total_rooms}")

        if not parts:
            parts.append("无内容")

        status = f"✅ 刷新完成: {'; '.join(parts)}"
        if errors:
            status += f" ⚠️ {len(errors)}个错误"
            status += "\n" + "\n".join(errors[:5])
            if len(errors) > 5:
                status += f"\n...还有 {len(errors)-5} 个错误"
        cfg["last_status"] = status
        save_schedule(cfg)
        print(f"[Scheduler] {status}")

        # ═══ 推送通知 ═══
        try:
            _send_notification("AB-Player 自动刷新", parts, total_series, total_ups, total_videos, total_rooms,
                               total_albums, total_acfun_ups, total_acfun_videos, errors)
        except Exception as e:
            print(f"[Scheduler] 通知发送失败: {e}")
    finally:
        _refresh_lock.release()


async def _do_refresh_async():
    """异步刷新所有订阅，由 _do_refresh 通过 asyncio.run 调用"""
    from .config import load_config as load_app_config, cache_delete
    app_cfg = load_app_config()
    series_list = app_cfg.get("bili_series", [])
    up_list = app_cfg.get("bili_ups", [])
    video_list = app_cfg.get("bili_videos", [])
    room_list = app_cfg.get("bili_rooms", [])
    album_list = app_cfg.get("acfun_albums", [])
    acfun_up_list = app_cfg.get("acfun_ups", [])
    acfun_video_list = app_cfg.get("acfun_videos", [])

    errors = []
    total_series = 0
    total_ups = 0
    total_albums = 0
    total_acfun_ups = 0
    total_videos = 0
    total_rooms = 0
    total_acfun_videos = 0

    # ═══ B站合集 ═══
    for s in series_list:
        sid = s.get("series_id", "")
        s_mid = s.get("mid", "")
        s_fmt = s.get("fmt", "channel")
        if not sid:
            continue
        try:
            from .bilibili import series_videos
            result = await series_videos(int(sid), mid=s_mid, pn=1, ps=100, fmt=s_fmt, force=True)
            if result:
                total_series += 1
            else:
                errors.append(f"B站合集 {sid}: 获取失败")
        except Exception as e:
            errors.append(f"B站合集 {sid}: {e}")

    # ═══ B站UP主 ═══
    for u in up_list:
        uid = u.get("mid", "")
        if not uid:
            continue
        try:
            from .bilibili import up_videos
            result = await up_videos(int(uid), pn=1, ps=50, force=True)
            if result:
                total_ups += 1
            else:
                errors.append(f"B站UP主 {uid}: 获取失败")
        except Exception as e:
            errors.append(f"B站UP主 {uid}: {e}")

    # ═══ B站单视频 ═══
    for v in video_list:
        bvid = v.get("id", "")
        if not bvid:
            continue
        try:
            from .bilibili import video_info, video_playurl
            cache_delete(f"vi:{bvid}")
            info = await video_info(bvid)
            if info:
                pages = info.get("pages", [])
                if pages:
                    first_cid = pages[0].get("cid", 0)
                    if first_cid:
                        cache_delete(f"pu:{bvid}:{first_cid}:116")
                        await video_playurl(bvid, first_cid, qn=116, fnval=4048)
                total_videos += 1
            else:
                errors.append(f"B站单视频 {bvid}: 获取失败")
        except Exception as e:
            errors.append(f"B站单视频 {bvid}: {e}")

    # ═══ B站直播 ═══
    for r in room_list:
        rid = r.get("room_id", "")
        if not rid:
            continue
        try:
            from .bilibili import live_info, live_playurl
            cache_delete(f"li:{rid}")
            info = await live_info(int(rid))
            if info:
                cache_delete(f"lp:{rid}")
                await live_playurl(int(rid))
                total_rooms += 1
            else:
                errors.append(f"B站直播 {rid}: 获取失败")
        except Exception as e:
            errors.append(f"B站直播 {rid}: {e}")

    # ═══ A站合辑 ═══
    for a in album_list:
        aid = a.get("id", "")
        if not aid:
            continue
        try:
            from .acfun import fetch_album_videos
            result = await fetch_album_videos(aid, force=True)
            if result:
                total_albums += 1
            else:
                errors.append(f"A站合辑 {aid}: 获取失败")
        except Exception as e:
            errors.append(f"A站合辑 {aid}: {e}")

    # ═══ A站UP主 ═══
    for u in acfun_up_list:
        mid = u.get("mid", "")
        if not mid:
            continue
        try:
            from .acfun import fetch_user_videos
            result = await fetch_user_videos(mid, force=True)
            if result:
                total_acfun_ups += 1
            else:
                errors.append(f"A站UP主 {mid}: 获取失败")
        except Exception as e:
            errors.append(f"A站UP主 {mid}: {e}")

    # ═══ A站单视频 ═══
    for v in acfun_video_list:
        vid = v.get("id", "")
        if not vid:
            continue
        try:
            from .acfun import fetch_video_info, get_play_url
            cache_delete(f"avi:{vid}")
            info = await fetch_video_info(vid, force=True)
            if info:
                cache_delete(f"apu:{vid}:")
                cache_delete(f"appu:{vid}:")
                await get_play_url(vid, "")
                total_acfun_videos += 1
            else:
                errors.append(f"A站单视频 {vid}: 获取失败")
        except Exception as e:
            errors.append(f"A站单视频 {vid}: {e}")

    return errors, total_series, total_ups, total_videos, total_rooms, total_albums, total_acfun_ups, total_acfun_videos

def _send_notification(title, parts, total_series, total_ups, total_videos, total_rooms,
                       total_albums, total_acfun_ups, total_acfun_videos, errors):
    """推送通知到 WxPusher / PushPlus"""
    from .config import load_config as load_app_config
    cfg = load_app_config()
    ewxp = cfg.get("ENABLE_WXPUSHER", False)
    epp = cfg.get("ENABLE_PUSHPLUS", False)
    if not ewxp and not epp:
        return

    # 构建通知内容
    content_lines = []
    if parts:
        content_lines.append("🎬 A站相关：")
        if total_albums:
            content_lines.append(f"  • 合辑: {total_albums} 个")
        if total_acfun_ups:
            content_lines.append(f"  • UP主: {total_acfun_ups} 个")
        if total_acfun_videos:
            content_lines.append(f"  • 单视频: {total_acfun_videos} 个")
        content_lines.append("📺 B站相关：")
        if total_series:
            content_lines.append(f"  • 合集: {total_series} 个")
        if total_ups:
            content_lines.append(f"  • UP主: {total_ups} 个")
        if total_videos:
            content_lines.append(f"  • 单视频: {total_videos} 个")
        if total_rooms:
            content_lines.append(f"  • 直播: {total_rooms} 个")
    else:
        content_lines.append("📭 无内容更新")

    if errors:
        content_lines.append(f"")
        content_lines.append(f"⚠️ {len(errors)} 个错误")
        for e in errors[:5]:
            content_lines.append(f"  • {e}")
        if len(errors) > 5:
            content_lines.append(f"  ...还有 {len(errors)-5} 个错误")

    content = "\n".join(content_lines)

    import httpx
    # WxPusher
    if ewxp and cfg.get("WXPUSHER_APP_TOKEN"):
        try:
            payload = {
                "appToken": cfg["WXPUSHER_APP_TOKEN"],
                "content": content,
                "summary": title,
                "uids": cfg.get("WXPUSHER_UIDS", []),
            }
            httpx.post(
                cfg.get("WXPUSHER_API_URL", "https://wxpusher.zjiecode.com/api/send/message"),
                json=payload, timeout=10
            )
        except Exception:
            pass
    # PushPlus
    if epp and cfg.get("PUSHPLUS_TOKEN"):
        try:
            httpx.post(
                "https://www.pushplus.plus/send",
                json={"token": cfg["PUSHPLUS_TOKEN"], "title": title, "content": content, "template": "text"},
                timeout=10
            )
        except Exception:
            pass