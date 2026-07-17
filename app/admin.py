"""管理面板 - AJAX action 处理 + 管理页面渲染"""

import json, time, hashlib, re
from typing import Optional
from fastapi import APIRouter, Query, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from .config import load_config, save_config, cache_get, cache_set, BASE_DIR, CONFIG_FILE, CACHE_DIR
from .bilibili import video_info, series_videos, up_videos, up_info, live_info, http_get_json, fetch_series_meta
from .acfun import fetch_album_videos, fetch_video_info as acfun_video_info, fetch_album_info, fetch_user_info
import httpx

def _cfg_get(cfg: dict, key: str, default=None):
    """Get config value with compatibility for both config formats"""
    return cfg.get(key, cfg.get(key, default))


def register(app):
    router = APIRouter()

    @router.get("/admin")
    async def admin_page(request: Request):
        action = request.query_params.get("action")
        if action == "load_config":
            cfg = load_config()
            return JSONResponse(cfg)
        if action == "up_info":
            mid = request.query_params.get("mid", "")
            if mid:
                info = await up_info(int(mid))
                if info:
                    return JSONResponse({"ok": True, "name": info.get("name", ""), "mid": info.get("mid", mid)})
            return JSONResponse({"error": "获取失败"})
        if action == "series_info":
            sid = request.query_params.get("series_id", "")
            s_mid = request.query_params.get("mid", "")
            s_fmt = request.query_params.get("fmt", "channel")
            if sid:
                # 先查配置中是否有已有名称
                cfg_local = load_config()
                for s in cfg_local.get("bili_series", []):
                    if s.get("series_id") == sid and s.get("name"):
                        if not s["name"].startswith("合集 #"):
                            return JSONResponse({"ok": True, "name": s["name"]})
                        break
                # 从 B站取合集元信息（轻量，1次API调用，无缓存污染）
                from .bilibili import fetch_series_meta as _fsm
                meta = await _fsm(int(sid))
                if meta and meta.get("name"):
                    # 校验合集是否属于当前用户（防取到同名 ID 的别人的合集）
                    if s_mid and str(meta.get("mid")) != str(s_mid):
                        return JSONResponse({"ok": False, "name": f"合集 #{sid}"})
                    return JSONResponse({"ok": True, "name": meta["name"]})
            return JSONResponse({"ok": False, "name": f"合集 #{sid}"})
        if action == "room_info":
            rid = request.query_params.get("room_id", "")
            if rid:
                info = await live_info(int(rid))
                if info:
                    return JSONResponse({
                        "ok": True,
                        "title": info.get("title", ""),
                        "uname": info.get("uname", ""),
                        "live_status": info.get("live_status", 0),
                        "room_id": info.get("room_id", rid),
                    })
            return JSONResponse({"error": "获取失败"})
        if action == "acfun_album_info":
            aid = request.query_params.get("album_id", "")
            if aid:
                from .acfun import fetch_album_info
                info = await fetch_album_info(aid)
                if info:
                    return JSONResponse({"ok": True, "title": info.get("title", "")})
            return JSONResponse({"error": "获取失败"})
        if action == "acfun_up_info":
            uid = request.query_params.get("up_mid", "")
            if uid:
                from .acfun import fetch_user_info as _acfun_fetch_user_info
                info = await _acfun_fetch_user_info(uid)
                if info:
                    return JSONResponse({"ok": True, "name": info.get("name", "")})
            return JSONResponse({"error": "获取失败"})
        if action == "acfun_video_info":
            vid = request.query_params.get("video_id", "")
            if vid:
                info = await acfun_video_info(vid)
                if info:
                    return JSONResponse({"ok": True, "title": info.get("title", "")})
            return JSONResponse({"error": "获取失败"})
        from jinja2 import Environment, FileSystemLoader
        templates = Environment(loader=FileSystemLoader(str(BASE_DIR / "app" / "templates")))
        tmpl = templates.get_template("admin.html")
        html = tmpl.render()
        return HTMLResponse(html)

    @router.post("/admin")
    async def admin_action(
        request: Request,
        action: str = Query(None),
        # 通用
        name: str = Form(None),
        items: str = Form(None),
        # B站合集
        series_id: str = Form(None),
        mid: str = Form(None),
        fmt: str = Form(None),
        series_name: str = Form(None),
        # B站UP主
        up_mid: str = Form(None),
        up_name: str = Form(None),
        # B站单视频
        video_id: str = Form(None),
        video_name: str = Form(None),
        # B站直播
        room_id: str = Form(None),
        room_name: str = Form(None),
        # A站合辑
        album_id: str = Form(None),
        album_name: str = Form(None),
        # A站UP主
        acfun_up_mid: str = Form(None),
        acfun_up_name: str = Form(None),
        # A站单视频
        acfun_video_id: str = Form(None),
        acfun_video_name: str = Form(None),
        # 订阅
        sub_name: str = Form(None),
        sub_type: str = Form(None),
        sub_items: str = Form(None),
        sub_groups: str = Form(None),
        class_name: str = Form(None),
        # 分类
        cat_name: str = Form(None),
        cat_items: str = Form(None),
        # Cookie
        cookie: str = Form(None),
        # 调度器
        sched_enabled: str = Form(None),
        sched_mode: str = Form(None),
        sched_time: str = Form(None),
        sched_interval: str = Form(None),
    ):
        cfg = load_config()

        # ════════════════════════════════════════════════
        # B站合集
        # ════════════════════════════════════════════════

        if action == "add_series":
            sid = series_id or ""
            s_mid = mid or ""
            s_name = series_name or f"合集 #{sid}"
            s_fmt = fmt or "channel"
            if not sid:
                return JSONResponse({"error": "缺少系列ID"})

            series = cfg.setdefault("bili_series", [])
            # 检查是否已存在
            for s in series:
                if s.get("series_id") == sid:
                    return JSONResponse({"error": "已存在"})
            # 没提供名字时从 B站 API 查
            if not series_name:
                try:
                    meta = await fetch_series_meta(int(sid))
                    if meta and meta.get("name"):
                        s_name = meta["name"]
                        if not s_mid:
                            s_mid = str(meta.get("mid", ""))
                except Exception:
                    pass
            series.append({"series_id": sid, "mid": s_mid, "name": s_name, "fmt": s_fmt})
            save_config(cfg)
            return JSONResponse({"ok": True, "name": s_name, "count": len(series)})

        if action == "delete_series":
            sid = series_id or ""
            series = cfg.get("bili_series", [])
            cfg["bili_series"] = [s for s in series if s.get("series_id") != sid]
            save_config(cfg)
            return JSONResponse({"ok": True})

        if action == "refresh_series":
            sid = series_id or ""
            s_mid = mid or ""
            s_fmt = fmt or "channel"
            if not s_mid:
                for s in cfg.get("bili_series", []):
                    if s.get("series_id") == sid:
                        s_mid = s.get("mid", "")
                        s_fmt = s.get("fmt", "channel")
                        break
            videos_data = await series_videos(int(sid), mid=s_mid, pn=1, ps=100, fmt=s_fmt, force=True)
            if videos_data:
                return JSONResponse({"ok": True, "total": videos_data.get("total", 0)})
            return JSONResponse({"error": "获取失败"})

        # ════════════════════════════════════════════════
        # B站UP主
        # ════════════════════════════════════════════════

        if action == "add_up":
            uid = up_mid or ""
            u_name = up_name or f"UP主 #{uid}"
            if not uid:
                return JSONResponse({"error": "缺少UP主ID"})
            ups = cfg.setdefault("bili_ups", [])
            for u in ups:
                if u.get("mid") == uid:
                    # 已存在时更新名称
                    if up_name:
                        u["name"] = up_name
                    else:
                        try:
                            info = await up_info(int(uid))
                            if info and info.get("name"):
                                u["name"] = info["name"]
                        except Exception:
                            pass
                    save_config(cfg)
                    return JSONResponse({"ok": True, "name": u["name"], "count": len(ups)})
            # 没提供名字时从 B站 API 查
            if not up_name:
                try:
                    info = await up_info(int(uid))
                    if info and info.get("name"):
                        u_name = info["name"]
                except Exception:
                    pass
            ups.append({"mid": uid, "name": u_name})
            save_config(cfg)
            return JSONResponse({"ok": True, "name": u_name, "count": len(ups)})

        if action == "delete_up":
            uid = up_mid or ""
            cfg["bili_ups"] = [u for u in cfg.get("bili_ups", []) if u.get("mid") != uid]
            save_config(cfg)
            return JSONResponse({"ok": True})

        if action == "refresh_up":
            uid = up_mid or ""
            # 刷新时顺带更新UP主名称
            if uid:
                ups = cfg.setdefault("bili_ups", [])
                for u in ups:
                    if u.get("mid") == uid:
                        info = await up_info(int(uid))
                        if info and info.get("name"):
                            u["name"] = info["name"]
                        break
                save_config(cfg)
            videos_data = await up_videos(int(uid), pn=1, ps=50, force=True)
            if videos_data:
                return JSONResponse({"ok": True, "total": len(videos_data.get("videos", []))})

        if action == "up_info":
            uid = up_mid or ""
            if not uid:
                return JSONResponse({"error": "缺少mid"})
            info = await up_info(int(uid))
            if info:
                return JSONResponse({"ok": True, "name": info.get("name", ""), "mid": info.get("mid", uid)})
            return JSONResponse({"error": "获取失败"})
            return JSONResponse({"error": "获取失败"})

        # ════════════════════════════════════════════════
        # B站单视频
        # ════════════════════════════════════════════════

        if action == "add_video":
            vid = video_id or ""
            v_name = video_name or f"视频 #{vid}"
            if not vid:
                return JSONResponse({"error": "缺少视频ID"})
            videos = cfg.setdefault("bili_videos", [])
            for v in videos:
                if v.get("id") == vid:
                    return JSONResponse({"error": "已存在"})
            # 没提供名字时从 B站 API 查
            if not video_name:
                try:
                    info = await video_info(vid)
                    if info and info.get("title"):
                        v_name = info["title"]
                except Exception:
                    pass
            videos.append({"id": vid, "name": v_name})
            save_config(cfg)
            return JSONResponse({"ok": True, "name": v_name, "count": len(videos)})

        if action == "delete_video":
            vid = video_id or ""
            cfg["bili_videos"] = [v for v in cfg.get("bili_videos", []) if v.get("id") != vid]
            save_config(cfg)
            return JSONResponse({"ok": True})

        if action == "refresh_bili_video":
            vid = video_id or ""
            if not vid:
                return JSONResponse({"error": "缺少视频ID"})
            info = await video_info(vid)
            if info:
                vname = info.get("title", "") or f"视频 #{vid}"
                for v in cfg.get("bili_videos", []):
                    if v.get("id") == vid:
                        v["name"] = vname
                        break
                save_config(cfg)
                return JSONResponse({"ok": True, "name": vname})
            return JSONResponse({"error": "获取失败"})

        # ════════════════════════════════════════════════
        # B站直播
        # ════════════════════════════════════════════════

        if action == "add_room":
            rid = room_id or ""
            r_name = room_name or f"直播间 #{rid}"
            if not rid:
                return JSONResponse({"error": "缺少房间ID"})
            rooms = cfg.setdefault("bili_rooms", [])
            for r in rooms:
                if r.get("room_id") == rid:
                    return JSONResponse({"error": "已存在"})
            # 没提供名字时从 B站 API 查
            if not room_name:
                try:
                    info = await live_info(int(rid))
                    if info and info.get("uname"):
                        r_name = info["uname"]
                except Exception:
                    pass
            rooms.append({"room_id": rid, "name": r_name})
            save_config(cfg)
            return JSONResponse({"ok": True, "name": r_name, "count": len(rooms)})

        if action == "delete_room":
            rid = room_id or ""
            cfg["bili_rooms"] = [r for r in cfg.get("bili_rooms", []) if r.get("room_id") != rid]
            save_config(cfg)
            return JSONResponse({"ok": True})

        if action == "refresh_room":
            rid = room_id or ""
            info = await live_info(int(rid))
            if info:
                return JSONResponse({
                    "ok": True,
                    "title": info.get("title", ""),
                    "live_status": info.get("live_status", 0),
                    "online": info.get("online", 0),
                })
            return JSONResponse({"error": "获取失败"})

        if action == "room_info":
            rid = room_id or ""
            if not rid:
                return JSONResponse({"error": "缺少房间号"})
            info = await live_info(int(rid))
            if info:
                return JSONResponse({
                    "ok": True,
                    "title": info.get("title", ""),
                    "uname": info.get("uname", ""),
                    "live_status": info.get("live_status", 0),
                    "room_id": info.get("room_id", rid),
                })
            return JSONResponse({"error": "获取失败"})

        # ════════════════════════════════════════════════
        # A站合辑
        # ════════════════════════════════════════════════

        if action == "add_album":
            aid = album_id or ""
            a_name = album_name or f"合辑 #{aid}"
            if not aid:
                return JSONResponse({"error": "缺少合辑ID"})
            albums = cfg.setdefault("acfun_albums", [])
            for a in albums:
                if a.get("id") == aid:
                    return JSONResponse({"error": "已存在"})
            # 没提供名字时从 A站 API 查
            if not album_name:
                try:
                    info = await fetch_album_info(aid)
                    if info and info.get("title"):
                        a_name = info["title"]
                except Exception:
                    pass
            albums.append({"id": aid, "name": a_name})
            save_config(cfg)
            return JSONResponse({"ok": True, "name": a_name, "count": len(albums)})

        if action == "delete_album":
            aid = album_id or ""
            cfg["acfun_albums"] = [a for a in cfg.get("acfun_albums", []) if a.get("id") != aid]
            save_config(cfg)
            return JSONResponse({"ok": True})

        if action == "refresh_album":
            aid = album_id or ""
            from .acfun import fetch_album_info as _fetch_album_info
            videos = await fetch_album_videos(aid, force=True)
            if videos:
                # 更新名称
                info = await _fetch_album_info(aid)
                if info and info.get("title"):
                    for a in cfg.get("acfun_albums", []):
                        if a.get("id") == aid:
                            a["name"] = info["title"]
                            save_config(cfg)
                            break
                return JSONResponse({"ok": True, "total": len(videos), "name": info.get("title", "") if info else ""})
            return JSONResponse({"error": "获取失败"})

        # ════════════════════════════════════════════════
        # A站UP主
        # ════════════════════════════════════════════════

        if action == "add_acfun_up":
            uid = acfun_up_mid or ""
            u_name = acfun_up_name or f"UP主 #{uid}"
            if not uid:
                return JSONResponse({"error": "缺少UP主ID"})
            ups = cfg.setdefault("acfun_ups", [])
            for u in ups:
                if u.get("mid") == uid:
                    # 已存在时更新名称
                    if acfun_up_name:
                        u["name"] = acfun_up_name
                    else:
                        try:
                            info = await fetch_user_info(str(uid))
                            if info and info.get("name"):
                                u["name"] = info["name"]
                        except Exception:
                            pass
                    save_config(cfg)
                    return JSONResponse({"ok": True, "name": u["name"], "count": len(ups)})
            # 没提供名字时从 A站 API 查
            if not acfun_up_name:
                try:
                    info = await fetch_user_info(str(uid))
                    if info and info.get("name"):
                        u_name = info["name"]
                except Exception:
                    pass
            ups.append({"mid": uid, "name": u_name})
            save_config(cfg)
            return JSONResponse({"ok": True, "name": u_name, "count": len(ups)})

        if action == "delete_acfun_up":
            uid = acfun_up_mid or ""
            cfg["acfun_ups"] = [u for u in cfg.get("acfun_ups", []) if u.get("mid") != uid]
            save_config(cfg)
            return JSONResponse({"ok": True})

        if action == "refresh_acfun_up":
            uid = acfun_up_mid or ""
            from .acfun import fetch_user_videos as _fetch_user_videos, fetch_user_info as _fetch_user_info
            videos = await _fetch_user_videos(uid, force=True)
            if videos:
                # 更新名称
                info = await _fetch_user_info(uid)
                if info and info.get("name"):
                    for u in cfg.get("acfun_ups", []):
                        if u.get("mid") == uid:
                            u["name"] = info["name"]
                            save_config(cfg)
                            break
                return JSONResponse({"ok": True, "total": len(videos), "name": info.get("name", "") if info else ""})
            return JSONResponse({"error": "获取失败"})

        # ════════════════════════════════════════════════
        # A站单视频
        # ════════════════════════════════════════════════

        if action == "add_acfun_video":
            vid = acfun_video_id or ""
            v_name = acfun_video_name or f"视频 #{vid}"
            if not vid:
                return JSONResponse({"error": "缺少视频ID"})
            videos = cfg.setdefault("acfun_videos", [])
            for v in videos:
                if v.get("id") == vid:
                    return JSONResponse({"error": "已存在"})
            # 没提供名字时从 A站 API 查
            if not acfun_video_name:
                try:
                    info = await acfun_video_info(vid)
                    if info and info.get("title"):
                        v_name = info["title"]
                except Exception:
                    pass
            videos.append({"id": vid, "name": v_name})
            save_config(cfg)
            return JSONResponse({"ok": True, "name": v_name, "count": len(videos)})

        if action == "delete_acfun_video":
            vid = acfun_video_id or ""
            cfg["acfun_videos"] = [v for v in cfg.get("acfun_videos", []) if v.get("id") != vid]
            save_config(cfg)
            return JSONResponse({"ok": True})

        if action == "refresh_acfun_video":
            vid = acfun_video_id or ""
            if not vid:
                return JSONResponse({"error": "缺少视频ID"})
            info = await acfun_video_info(vid, force=True)
            if info:
                vname = info.get("title", "") or f"视频 #{vid}"
                for v in cfg.get("acfun_videos", []):
                    if v.get("id") == vid:
                        v["name"] = vname
                        break
                save_config(cfg)
                return JSONResponse({"ok": True, "name": vname})
            return JSONResponse({"error": "获取失败"})

        # ════════════════════════════════════════════════
        # 订阅管理
        # ════════════════════════════════════════════════

        # ════════════════════════════════════════════════
        # 订阅管理
        # ════════════════════════════════════════════════

        if action == "save_sub":
            s_name = sub_name or ""
            s_type = sub_type or "live"
            s_class_name = class_name or ""
            s_groups_raw = sub_groups or ""
            s_items_raw = sub_items or ""
            if not s_name:
                return JSONResponse({"error": "缺少订阅名称"})

            subs = cfg.setdefault("subscriptions", [])
            new_entry = {"name": s_name, "type": s_type}

            if s_groups_raw:
                # 新格式：分组订阅
                try:
                    groups = json.loads(s_groups_raw)
                    if not isinstance(groups, list):
                        groups = []
                    for g in groups:
                        if isinstance(g.get("items"), str):
                            g["items"] = [x.strip() for x in g["items"].split(",") if x.strip()]
                        elif not isinstance(g.get("items"), list):
                            g["items"] = []
                    new_entry["groups"] = groups
                except json.JSONDecodeError:
                    pass
            elif s_items_raw:
                # 旧格式：平面 item 列表
                s_items = [x.strip() for x in s_items_raw.split(",") if x.strip()]
                new_entry["items"] = s_items

            if s_class_name:
                new_entry["class_name"] = s_class_name

            found = False
            for s in subs:
                if s.get("name") == s_name:
                    s.update(new_entry)
                    found = True
                    break
            if not found:
                subs.append(new_entry)
            # 如果同时传了分类，一并保存（防前端异步丢失）
            if cat_items:
                try:
                    _cats = json.loads(cat_items)
                    if isinstance(_cats, list):
                        for _c in _cats:
                            if isinstance(_c.get("items"), str):
                                _c["items"] = [x.strip() for x in _c["items"].split(",") if x.strip()]
                            elif not isinstance(_c.get("items"), list):
                                _c["items"] = []
                        cfg["categories"] = _cats
                except json.JSONDecodeError:
                    pass
            save_config(cfg)
            return JSONResponse({"ok": True, "count": len(subs)})

        if action == "delete_sub":
            s_name = sub_name or ""
            cfg["subscriptions"] = [s for s in cfg.get("subscriptions", []) if s.get("name") != s_name]
            save_config(cfg)
            return JSONResponse({"ok": True})

        # ════════════════════════════════════════════════
        # 分类管理
        # ════════════════════════════════════════════════

        if action == "save_categories":
            """保存全部分类（覆盖写入）"""
            raw = cat_items or "[]"
            try:
                cats = json.loads(raw)
                if not isinstance(cats, list):
                    cats = []
                for c in cats:
                    if isinstance(c.get("items"), str):
                        c["items"] = [x.strip() for x in c["items"].split(",") if x.strip()]
                    elif not isinstance(c.get("items"), list):
                        c["items"] = []
                cfg["categories"] = cats
                save_config(cfg)
                return JSONResponse({"ok": True, "count": len(cats)})
            except json.JSONDecodeError:
                return JSONResponse({"error": "JSON 格式错误"})

        if action == "delete_category":
            """删除分类"""
            cname = cat_name or ""
            if not cname:
                return JSONResponse({"error": "缺少分类名"})
            old_cats = cfg.get("categories", [])
            cfg["categories"] = [c for c in old_cats if c.get("name") != cname]
            save_config(cfg)
            return JSONResponse({"ok": True})

        # ════════════════════════════════════════════════
        # Cookie 管理
        # ════════════════════════════════════════════════

        if action == "save_cookie":
            cfg["cookie"] = cookie or ""
            cfg["bili_cookie"] = cookie or ""
            save_config(cfg)
            return JSONResponse({"ok": True})

        if action == "save_acfun_cookie":
            cfg["acfun_cookie"] = cookie or ""
            save_config(cfg)
            return JSONResponse({"ok": True})

        if action == "clear_acfun_cookie":
            cfg["acfun_cookie"] = ""
            save_config(cfg)
            return JSONResponse({"ok": True})

        # ════════════════════════════════════════════════
        # 缓存管理
        # ════════════════════════════════════════════════

        if action == "clear_cache":
            count = 0
            if CACHE_DIR.exists():
                for f in CACHE_DIR.iterdir():
                    if f.is_file():
                        f.unlink()
                        count += 1
            return JSONResponse({"ok": True, "count": count})

        # ════════════════════════════════════════════════
        # B站扫码登录
        # ════════════════════════════════════════════════

        if action == "qrcode_gen":
            return await _qrcode_generate(request)

        if action == "qrcode_poll":
            qrcode_key = request.query_params.get("qrcode_key", "") or (await request.form()).get("qrcode_key", "")
            return await _qrcode_poll(cfg, qrcode_key)

        # ════════════════════════════════════════════════
        # A站 扫码登录
        # ════════════════════════════════════════════════

        if action == "acfun_qrcode_gen":
            return await _acfun_qrcode_generate()

        if action == "acfun_qrcode_poll":
            qr_token = request.query_params.get("qrLoginToken", "") or (await request.form()).get("qrLoginToken", "")
            qr_sig = request.query_params.get("qrLoginSignature", "") or (await request.form()).get("qrLoginSignature", "")
            return await _acfun_qrcode_poll(cfg, qr_token, qr_sig)

        # ════════════════════════════════════════════════
        # 加载配置
        # ════════════════════════════════════════════════

        if action == "load_config":
            return JSONResponse(cfg)

        # ════════════════════════════════════════════════
        # 调度器配置
        # ════════════════════════════════════════════════

        if action == "load_schedule":
            from .scheduler import load_schedule
            return JSONResponse(load_schedule())

        if action == "save_schedule":
            from .scheduler import load_schedule, save_schedule
            sched = load_schedule()
            if sched_enabled is not None:
                sched["enabled"] = sched_enabled.lower() == "true"
            if sched_mode:
                sched["mode"] = sched_mode
            if sched_time:
                sched["time"] = sched_time
            if sched_interval:
                sched["interval_hours"] = int(sched_interval)
            save_schedule(sched)
            return JSONResponse({"ok": True, "schedule": sched})

        return JSONResponse({"error": f"未知操作: {action}"})

    app.include_router(router)


# ═══════════════════════════════════════════════════════════════════
#  扫码登录
# ═══════════════════════════════════════════════════════════════════

async def _qrcode_generate(request: Request):
    """生成B站扫码登录二维码"""
    import httpx
    base_url = str(request.base_url).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            # 获取二维码 URL 和 key
            r = await cli.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            data = r.json()
            if data.get("code") != 0:
                return JSONResponse({"error": "获取二维码失败"})
            qr_data = data.get("data", {})
            return JSONResponse({
                "url": qr_data.get("url", ""),
                "qrcode_key": qr_data.get("qrcode_key", ""),
            })
    except Exception as e:
        return JSONResponse({"error": str(e)})


async def _qrcode_poll(cfg: dict, qrcode_key: str):
    """轮询扫码状态"""
    import httpx
    if not qrcode_key:
        return JSONResponse({"error": "缺少 qrcode_key"})
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.get(
                f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            data = r.json()
            if data.get("code") != 0:
                return JSONResponse({"code": -1, "message": data.get("message", "未知错误")})

            poll_data = data.get("data", {})
            code = poll_data.get("code", -1)
            # B站 QR 轮询:
            #   code = -5   → 未扫码
            #   code = 86090 → 已扫码，等待手机确认
            #   code = 0    → 扫码成功！Cookie 在响应中
            #   code = 86038 → 已过期
            if code == 0:
                # 扫码成功，从 httpx cookie jar 提取
                try:
                    sc_jar = {}
                    for k, v in r.cookies.items():
                        sc_jar[k] = v
                    if sc_jar:
                        cookie_str = "; ".join(f"{k}={v}" for k, v in sc_jar.items())
                        cfg["cookie"] = cookie_str
                        cfg["bili_cookie"] = cookie_str
                        save_config(cfg)
                        return JSONResponse({"code": 1, "cookie": cookie_str})
                except Exception:
                    pass
                return JSONResponse({"code": 0, "message": "已扫码，请手动复制Cookie"})

            return JSONResponse({"code": code})
    except Exception as e:
        return JSONResponse({"code": -1, "message": str(e)})


async def _acfun_qrcode_generate():
    """A站生成扫码登录二维码"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.get(
                "https://scan.acfun.cn/rest/pc-direct/qr/start?type=WEB_LOGIN",
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.acfun.cn"},
            )
            data = r.json()
            if data.get("result") != 0:
                return JSONResponse({"error": "获取二维码失败"})
            return JSONResponse({
                "image_data": data.get("imageData", ""),
                "qrLoginToken": data.get("qrLoginToken", ""),
                "qrLoginSignature": data.get("qrLoginSignature", ""),
                "expireTime": data.get("expireTime", 120000),
            })
    except Exception as e:
        return JSONResponse({"error": str(e)})


async def _acfun_qrcode_poll(cfg: dict, qr_token: str, qr_sig: str):
    """轮询A站扫码状态
    用短超时做轮询：每个请求只等几秒，超时了就重试
    """
    import httpx
    if not qr_token or not qr_sig:
        return JSONResponse({"error": "缺少参数"})
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.acfun.cn"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as cli:
            # 第一步：等扫码
            scan_url = f"https://scan.acfun.cn/rest/pc-direct/qr/scanResult?qrLoginToken={qr_token}&qrLoginSignature={qr_sig}"
            r = await cli.get(scan_url, headers=headers)
            data = r.json()
            result = data.get("result", -1)
            
            if result in (100400002, -2):
                return JSONResponse({"code": -2, "message": "二维码已过期"})
            
            if result != 0:
                return JSONResponse({"code": result})
            
            # 已扫码，第二步：等手机确认
            # 注意：scanResult 返回了一个新的 qrLoginSignature，acceptResult 必须用新签名
            new_sig = data.get("qrLoginSignature", qr_sig)
            accept_url = f"https://scan.acfun.cn/rest/pc-direct/qr/acceptResult?qrLoginToken={qr_token}&qrLoginSignature={new_sig}"
            r2 = await cli.get(accept_url, headers=headers)
            data2 = r2.json()
            result2 = data2.get("result", -1)
            
            if result2 == 0:
                # 从 httpx cookie jar 提取
                try:
                    sc_jar = {}
                    for k, v in r2.cookies.items():
                        sc_jar[k] = v
                    if sc_jar:
                        cookie_str = "; ".join(f"{k}={v}" for k, v in sc_jar.items())
                        cfg["acfun_cookie"] = cookie_str
                        save_config(cfg)
                        return JSONResponse({"code": 1, "cookie": cookie_str})
                except Exception:
                    pass
                # 备选：从响应头 Set-Cookie 提取
                try:
                    sc = {}
                    for k, v in r2.headers.raw:
                        if k.lower() == b"set-cookie":
                            for part in v.decode().split(";"):
                                if "=" in part:
                                    kv = part.strip().split("=", 1)
                                    sc[kv[0]] = kv[1]
                    if sc:
                        cookie_str = "; ".join(f"{k}={v}" for k, v in sc.items())
                        cfg["acfun_cookie"] = cookie_str
                        save_config(cfg)
                        return JSONResponse({"code": 1, "cookie": cookie_str})
                except Exception:
                    pass
                return JSONResponse({"code": 0, "message": "请手动复制Cookie"})
            elif result2 in (100400002, -2):
                return JSONResponse({"code": -2, "message": "二维码已过期"})
            else:
                return JSONResponse({"code": result2})
    except httpx.TimeoutException:
        return JSONResponse({"code": -5, "message": "等待扫码"})
    except Exception as e:
        return JSONResponse({"code": -1, "message": str(e)})