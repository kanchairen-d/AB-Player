"""TVBox / UZ 订阅 API - Apple CMS JSON 格式"""

import json, re, math, time
from typing import Optional
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from .config import load_config, BASE_DIR, http_get
from .bilibili import video_info, series_videos, up_videos, search_videos
from .acfun import fetch_album_videos, fetch_user_videos

# B站 CDN URL 缓存（detail 预取，play 直接用）
_BILI_CDN_CACHE = {}  # key: bvid_p -> {"cdn_url": str, "cid": int, "expires_at": float}


def build_base_url(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-Host")
    if forwarded:
        return f"http://{forwarded}"
    host = request.headers.get("host", "")
    if host:
        return f"http://{host}"
    return str(request.base_url).rstrip("/")


def register(app):
    router = APIRouter()

    @router.get("/api")
    async def api_route(
        request: Request,
        t: str = Query(None),
        uz: str = Query(None),
        ac: str = Query(None),
        id: str = Query(None),
        p: int = Query(1),
        info: str = Query(None),
        ids: str = Query(None),
        wd: str = Query(None),
        pg: int = Query(1),
        sub: str = Query(None),
        action: str = Query(None),
        series: str = Query(None),
        mid: str = Query(None),
        up: str = Query(None),
        album: str = Query(None),
    ):
        base_url = build_base_url(request)
        cfg = load_config()

        # ═══ B站 代理播放（bili_proxy=CDN_URL） ═══
        bili_proxy_url = request.query_params.get("bili_proxy")
        if bili_proxy_url:
            import httpx
            from fastapi.responses import StreamingResponse
            bili_headers = {
                "Referer": "https://www.bilibili.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            range_h = request.headers.get("range")
            if range_h:
                bili_headers["Range"] = range_h

            cli = httpx.AsyncClient(timeout=30.0)
            req = httpx.Request("GET", bili_proxy_url, headers=bili_headers)
            resp = await cli.send(req, stream=True)

            resp_headers = {}
            for k, v in resp.headers.items():
                if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges"):
                    resp_headers[k] = v

            async def _bili_stream():
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                finally:
                    await resp.aclose()
                    await cli.aclose()

            return StreamingResponse(
                _bili_stream(),
                status_code=resp.status_code,
                headers=resp_headers,
                media_type=resp_headers.get("content-type", "video/mp4"),
            )

        # ═══ UZ 蜘蛛 ═══
        if uz or ac == "uz":
            return await _handle_uz_spider(request, cfg)

        # ═══ TVBox 播放代理 ═══
        if id and not info:
            bvid = id
            # 先查 CDN 缓存（detail 预取），跳过 B站 API 调用
            cache_key = f"{bvid}_{p}"
            cached = _BILI_CDN_CACHE.get(cache_key)
            if cached and cached["expires_at"] > time.time():
                url = cached["cdn_url"]
            else:
                vi = await video_info(bvid)
                if not vi:
                    return JSONResponse({"error": "获取视频信息失败"}, status_code=404)
                pages = vi.get("pages", [])
                cid = 0
                for pg_ in pages:
                    if pg_.get("page", 0) == p:
                        cid = pg_.get("cid", 0)
                        break
                if not cid and pages:
                    cid = pages[0].get("cid", 0)
                if not cid:
                    return JSONResponse({"error": "无法获取CID"}, status_code=404)

                from .player import bili_get_play_url
                url = await bili_get_play_url(bvid, cid)
                if not url:
                    return JSONResponse({"error": "获取播放地址失败"}, status_code=404)

            import httpx
            from fastapi.responses import StreamingResponse
            bili_headers = {
                "Referer": "https://www.bilibili.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            range_h = request.headers.get("range")
            if range_h:
                bili_headers["Range"] = range_h

            cli = httpx.AsyncClient(timeout=30.0)
            req = httpx.Request("GET", url, headers=bili_headers)
            resp = await cli.send(req, stream=True)

            resp_headers = {}
            for k, v in resp.headers.items():
                if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges"):
                    resp_headers[k] = v

            async def _bili_stream():
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                finally:
                    await resp.aclose()
                    await cli.aclose()

            return StreamingResponse(
                _bili_stream(),
                status_code=resp.status_code,
                headers=resp_headers,
                media_type=resp_headers.get("content-type", "video/mp4"),
            )

        # ═══ 刷新合集/UP主（UZ蜘蛛用） ═══
        if action in ("refresh_series", "refresh_up", "refresh_acfun_album", "refresh_acfun_up", "refresh_sub"):
            if action == "refresh_series":
                sid = series or request.query_params.get("series", "")
                if not sid:
                    return JSONResponse({"error": "缺少 series"})
                s_mid = mid or ""
                fmt = "channel"
                if not s_mid:
                    for s in cfg.get("series", []):
                        if str(s.get("id", "")) == sid or str(s.get("series_id", "")) == sid:
                            s_mid = str(s.get("mid", ""))
                            fmt = s.get("fmt", "channel")
                            break
                    if not s_mid:
                        for s in cfg.get("bili_series", []):
                            if str(s.get("series_id", "")) == sid:
                                s_mid = str(s.get("mid", ""))
                                fmt = s.get("fmt", "channel")
                                break

                videos_data = await series_videos(int(sid), mid=s_mid, pn=1, ps=100, fmt=fmt)
                if videos_data:
                    return JSONResponse({"videos": videos_data.get("videos", [])})
                return JSONResponse({"error": "获取失败"})

            if action == "refresh_up":
                uid = up or request.query_params.get("mid", "")
                if not uid:
                    return JSONResponse({"error": "缺少 mid"})
                videos_data = await up_videos(int(uid), pn=1, ps=50)
                if videos_data:
                    return JSONResponse({"videos": videos_data.get("videos", [])})
                return JSONResponse({"error": "获取失败"})

            if action == "refresh_acfun_album":
                album_id = album or request.query_params.get("album", "")
                if not album_id:
                    return JSONResponse({"error": "缺少 album"})
                from .acfun import fetch_album_videos as _fetch_acfun_album_videos
                videos = await _fetch_acfun_album_videos(album_id)
                if videos:
                    return JSONResponse({"videos": videos})
                return JSONResponse({"error": "获取失败"})

            if action == "refresh_acfun_up":
                ac_up = up or request.query_params.get("up", "")
                if not ac_up:
                    return JSONResponse({"error": "缺少 up"})
                from .acfun import fetch_user_videos as _fetch_acfun_user_videos
                videos = await _fetch_acfun_user_videos(ac_up)
                if videos:
                    return JSONResponse({"videos": videos})
                return JSONResponse({"error": "获取失败"})

            # ═══ 刷新合并订阅（UZ蜘蛛用） ═══
            if action == "refresh_sub":
                sub_name = sub or request.query_params.get("sub", "")
                if not sub_name:
                    return JSONResponse({"error": "缺少 sub"})
                # 支持 sub=订阅名_索引 格式
                sub_group_idx = -1
                parts = sub_name.rsplit("_", 1)
                if len(parts) == 2 and parts[1].isdigit():
                    sub_name = parts[0]
                    sub_group_idx = int(parts[1])
                sub_data = None
                for s in cfg.get("subscriptions", []):
                    if s.get("name") == sub_name:
                        sub_data = s
                        break
                if not sub_data:
                    return JSONResponse({"videos": []})
                # 优先 groups，其次旧格式 items
                sg = sub_data.get("groups")
                if sg and sub_group_idx >= 0:
                    # 指定了分组索引，只取该组
                    items = sg[sub_group_idx].get("items", []) if sub_group_idx < len(sg) else []
                elif sub_group_idx >= 0 and not sg:
                    # 无 groups 但有索引：按索引取单个 item
                    all_items = sub_data.get("items", [])
                    items = [all_items[sub_group_idx]] if sub_group_idx < len(all_items) else []
                elif sg:
                    # 未指定分组索引，合并所有组
                    items = []
                    for g in sg:
                        items.extend(g.get("items", []))
                else:
                    items = sub_data.get("items", [])
                videos = []
                for it in items:
                    if it.startswith("video_"):
                        vid = it[6:]
                        vname = vid
                        for v in cfg.get("bili_videos", []):
                            if v.get("id") == vid:
                                vname = v.get("name", vid)
                                break
                        pic = ""
                        try:
                            vi = await video_info(vid)
                            if vi:
                                pic = vi.get("pic", "")
                                if vname == vid or vname.startswith("视频 #"):
                                    vname = vi.get("title", vname)
                        except:
                            pass
                        videos.append({"bvid": vid, "title": vname, "pic": pic, "pubdate": 0})
                    elif it.startswith("acfun_video_"):
                        vid = it[12:]
                        vname = vid
                        for v in cfg.get("acfun_videos", []):
                            if v.get("id") == vid:
                                vname = v.get("name", vid)
                                break
                        cover = ""
                        try:
                            from .acfun import fetch_video_info as _acfun_fetch_vi
                            vi = await _acfun_fetch_vi(vid)
                            if vi:
                                cover = vi.get("cover", "")
                                if vname == vid:
                                    vname = vi.get("title", vname)
                        except:
                            pass
                        videos.append({"id": vid, "title": vname, "cover": cover, "createTime": 0})
                    elif it.startswith("room_"):
                        rid = it[5:]
                        rname = rid
                        for r in cfg.get("bili_rooms", []):
                            if str(r.get("room_id", "")) == rid:
                                rname = r.get("name", rid)
                                break
                        videos.append({"id": f"room_{rid}", "title": rname, "pic": "", "live": True})
                    elif it.startswith("series_"):
                        sid = it[7:]
                        try:
                            # 从配置查 mid 和 fmt
                            s_mid = ""
                            s_fmt = "channel"
                            for s in cfg.get("bili_series", []):
                                if str(s.get("series_id", "")) == sid:
                                    s_mid = str(s.get("mid", ""))
                                    s_fmt = s.get("fmt", "channel")
                                    break
                            if not s_mid:
                                for s in cfg.get("series", []):
                                    if str(s.get("id", "")) == sid or str(s.get("series_id", "")) == sid:
                                        s_mid = str(s.get("mid", ""))
                                        s_fmt = s.get("fmt", "channel")
                                        break
                            sv = await series_videos(int(sid), mid=s_mid, pn=1, ps=100, fmt=s_fmt)
                            if sv and sv.get("videos"):
                                for v in sv["videos"]:
                                    bvid = v.get("bvid", v.get("aid", ""))
                                    if bvid:
                                        videos.append({"bvid": bvid, "title": v.get("title", ""), "pic": v.get("pic", ""), "pubdate": v.get("pubdate", 0)})
                        except Exception:
                            pass
                    elif it.startswith("up_"):
                        mid = it[3:]
                        try:
                            uv = await up_videos(int(mid))
                            if uv and uv.get("videos"):
                                for v in uv["videos"]:
                                    bvid = v.get("bvid", v.get("aid", ""))
                                    if bvid:
                                        videos.append({"bvid": bvid, "title": v.get("title", ""), "pic": v.get("pic", ""), "pubdate": v.get("pubdate", 0)})
                        except Exception:
                            pass
                    elif it.startswith("acfun_album_"):
                        aid = it[12:]
                        try:
                            av = await fetch_album_videos(aid)
                            if av:
                                for v in av:
                                    vid = v.get("id", "")
                                    if vid:
                                        videos.append({"id": vid, "title": v.get("title", ""), "cover": v.get("cover", ""), "createTime": v.get("createTime", 0)})
                        except Exception:
                            pass
                    elif it.startswith("acfun_up_"):
                        uid = it[9:]
                        try:
                            uvv = await fetch_user_videos(uid)
                            if uvv:
                                for v in uvv:
                                    vid = v.get("id", "")
                                    if vid:
                                        videos.append({"id": vid, "title": v.get("title", ""), "cover": v.get("cover", ""), "createTime": v.get("createTime", 0)})
                        except Exception:
                            pass
                return JSONResponse({"videos": videos})

        # ═══ 视频信息（UZ蜘蛛用） ═══
        if info:
            info_str = info if isinstance(info, str) else id
            # A站视频 ID 是纯数字
            if info_str and info_str.isdigit():
                from .acfun import fetch_video_info as _fetch_acfun_info
                vi = await _fetch_acfun_info(info_str)
                if vi:
                    return JSONResponse({"data": vi})
            else:
                vi = await video_info(info_str)
                if vi:
                    pages = vi.get("pages", [])
                    return JSONResponse({"data": vi, "total": len(pages)})
            return JSONResponse({"error": "获取失败"})

        if t == "sub":
            # type:4 — 使用 Omnibox.jar（已验证可加载），API 指向我们
            omni_jar = f"{base_url}/static/abplayer.jar"
            return JSONResponse({
                "spider": f"{omni_jar};md5;16e1a670c0f8ce12bd993c1b488aacde",
                "sites": [{
                    "key": "abplayer",
                    "name": "AB Player (B站/A站)",
                    "type": 4,
                    "api": f"{base_url}/api",
                    "searchable": 1,
                    "quickSearch": 0,
                    "filterable": 0,
                    "indexs": 1,
                }],
                "lives": [],
                "parses": [],
            })

        if t == "sub3":
            # type:3 — JSON 蜘蛛
            return JSONResponse({
                "spider": f"{jar_url};md5;16e1a670c0f8ce12bd993c1b488aacde",
                "sites": [{
                    "key": "abplayer",
                    "name": "AB Player (B站/A站)",
                    "type": 3,
                    "api": f"{base_url}/api",
                    "searchable": 1,
                    "quickSearch": 0,
                    "filterable": 0,
                }],
                "lives": [],
                "parses": [],
            })

        if t == "sub4":
            # type:4 — 全局蜘蛛（不带 spider 字段，对比测试用）
            return JSONResponse({
                "sites": [
                    {
                        "key": "abplayer",
                        "name": "AB Player (B站/A站)",
                        "type": 1,
                        "api": api_url,
                        "jar": jar_url,
                        "ext": api_url,
                        "searchable": 1,
                        "quickSearch": 0,
                        "filterable": 0,
                    }
                ],
                "lives": [],
                "parses": [],
            })

        if t == "sub3":
            # type:3 — JSON 蜘蛛（无需 JAR）
            return JSONResponse({
                "sites": [
                    {
                        "key": "abplayer",
                        "name": "AB Player (B站/A站)",
                        "type": 3,
                        "api": f"{base_url}/api",
                        "searchable": 1,
                        "quickSearch": 0,
                        "filterable": 0,
                    }
                ],
                "lives": [],
                "parses": [],
            })

        if t == "sub4":
            # type:4 — 全局蜘蛛格式（类似 Omnibox 方式）
            return JSONResponse({
                "sites": [
                    {
                        "key": "abplayer",
                        "name": "AB Player (B站/A站)",
                        "type": 4,
                        "api": f"{base_url}/api",
                        "searchable": 1,
                        "quickSearch": 0,
                        "filterable": 0,
                    }
                ],
                "lives": [],
                "parses": [],
            })

        if t == "config":
            if uz is not None:
                spider_url = f"{base_url}/api?t=spider"
                if ids:
                    spider_url += f"&ids={ids}"
                if sub:
                    # 用路径式 URL 防 APP 丢掉 sub 参数
                    spider_url = f"{base_url}/api/spider/{sub}"
                return JSONResponse({
                    "vod": [{
                        "api": spider_url,
                        "name": "AB Player (B站/A站)",
                        "order": "A",
                        "type": 101,
                        "webSite": base_url,
                    }],
                    "live": [],
                    "danMu": [],
                })
            else:
                jar_url = f"{base_url}/static/abplayer.jar;md5;16e1a670c0f8ce12bd993c1b488aacde"
                api_url = f"{base_url}/api"
                if sub:
                    api_url += f"/sub/{sub}"
                return JSONResponse({
                    "spider": jar_url,
                    "sites": [{
                        "key": "abplayer",
                        "name": "AB Player (B站/A站)",
                        "type": 4,
                        "api": api_url,
                        "searchable": 1,
                        "quickSearch": 0,
                        "filterable": 0,
                        "indexs": 1,
                    }],
                    "lives": [],
                    "parses": [],
                })

        # ═══ 订阅格式输出 ═══
        if t == "sub_m3u":
            if not sub:
                return JSONResponse({"error": "缺少 sub"})
            from .m3u import build_sub_m3u
            return await build_sub_m3u(request, cfg, sub)

        if t == "sub_ok":
            # OK影视格式（TVBox + spider jar）
            if not sub:
                return JSONResponse({"error": "缺少 sub"})
            api_url = f"{base_url}/api?sub_ok_source={sub}"
            live_url = f"{base_url}/api?sub_live_m3u={sub}"
            return JSONResponse({
                "sites": [{
                    "key": f"abp_{sub}",
                    "name": f"ABP {sub}",
                    "type": 4,
                    "api": api_url,
                    "searchable": 1,
                    "filterable": 0,
                }],
                "lives": [{
                    "name": f"ABP {sub} 直播",
                    "type": 4,
                    "url": live_url,
                }],
                "parses": [],
            })

        if t == "sub_catvod":
            # 猫影视 JS 蜘蛛脚本
            if not sub:
                return JSONResponse({"error": "缺少 sub"})
            from .m3u import build_sub_m3u
            m3u_url = f"{base_url}/api?t=sub_m3u&sub={sub}"
            js_code = f"""var rule = {{
    title: '{sub}',
    host: '{base_url}',
    url: '/api?t=sub_m3u&sub={sub}',
    searchable: 0,
    quickSearch: 0,
    filterable: 0,
    class_name: '点播',
    class_url: 'vod',
}};

async function getHomeContent() {{
    const m3u = await req('{m3u_url}', {{}});
    const items = m3u.split('\\n');
    const videos = [];
    let currentGroup = '';
    for (let i = 0; i < items.length; i++) {{
        const line = items[i].trim();
        if (line.startsWith('#EXTINF:')) {{
            const match = line.match(/group-title=\"([^\"]+)\"/);
            if (match) currentGroup = match[1];
            const nameMatch = line.match(/,(.+)$/);
            const vname = nameMatch ? nameMatch[1] : '';
            const nextLine = items[i+1] ? items[i+1].trim() : '';
            if (nextLine && !nextLine.startsWith('#')) {{
                videos.push({{
                    vod_id: nextLine,
                    vod_name: vname + ' - ' + currentGroup,
                    vod_pic: '',
                    vod_remarks: 'B站/A站',
                }});
                i++;
            }}
        }}
    }}
    return {{ list: videos }};
}}

async function getCategoryContent(tid, pg, ext, filter) {{
    return await getHomeContent();
}}

async function getDetailContent(id) {{
    const play_url = id;
    return {{
        list: [{{
            vod_id: id,
            vod_name: id,
            vod_pic: '',
            vod_play_from: 'ABP',
            vod_play_url: '播放$' + play_url,
        }}],
    }};
}}

async function getSearchContent(wd, quick) {{
    return {{ list: [] }};
}}

async function getPlayerContent(flag, id) {{
    return {{ urls: [{{ url: id }}] }};
}}
"""
            return Response(js_code, media_type="application/javascript; charset=utf-8")

        if t == "sub_live_m3u":
            # 订阅的直播 M3U
            if not sub:
                return JSONResponse({"error": "缺少 sub"})
            items = []
            if sub == "_all":
                seen = set()
                for s in cfg.get("subscriptions", []):
                    for it in s.get("items", []):
                        if it not in seen:
                            seen.add(it)
                            items.append(it)
            else:
                for s in cfg.get("subscriptions", []):
                    if s.get("name") == sub:
                        items = s.get("items", [])
                        break
            lines = ["#EXTM3U", f"#PLAYLIST:{sub} 直播\n"]
            for it in items:
                if it.startswith("room_"):
                    rid = it.replace("room_", "")
                    for r in cfg.get("bili_rooms", []):
                        if str(r.get("room_id", "")) == rid:
                            rname = r.get("name", f"B站直播 #{rid}")
                            lines.append(f"#EXTINF:-1 group-title=\"B站直播\",{rname}")
                            lines.append(f"{base_url}/bili?room={rid}")
            return Response("\n".join(lines), media_type="application/x-mpegURL; charset=utf-8")

        if t == "sub_ok_source":
            # OK影视 spider source：返回该订阅的视频列表
            from .m3u import build_sub_m3u
            return await build_sub_m3u(request, cfg, sub)

        # ═══ UZ 蜘蛛脚本 ═══
        if t == "spider":
            return await _handle_uz_spider(request, cfg)

        # ═══ TVBox 蜘蛛 JS（JS 蜘蛛代理） ═══
        if t == "tvbox_spider":
            return await _handle_tvbox_spider(request, cfg, base_url)

        # ═══ Apple CMS JSON API ═══
        # TVBox 把 ?ac=xxx 追加到已有 ?sub=xxx 的 API URL 后面
        # 结果：api?sub=22?ac=list → sub="22?ac=list", ac=None
        # 从 raw query string 手动重解析
        raw_qs = request.scope.get("query_string", b"").decode()
        if raw_qs.startswith("sub=") and "?" in raw_qs:
            # sub=22?ac=list&t=xxx&pg=1
            sub = raw_qs.split("?")[0].split("=", 1)[1]  # "22"
            # 从 ? 后面解析 ac (TVBox 追加的参数)
            rest = raw_qs.split("?", 1)[1]
            for pair in rest.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    if k == "ac": ac = v
        filtered_cfg = _filter_config(cfg, ids, sub)

        if ac == "list" or ac == "home":
            return await _handle_home(filtered_cfg)
        elif ac == "class":
            return _handle_classes(filtered_cfg)
        elif ac == "videolist":
            type_id = request.query_params.get("t", "")
            return await _handle_videolist(filtered_cfg, type_id, pg)
        # ★ playId 处理器（TVBox 发 playId=URL）
        if (not ac or ac == "play") and request.query_params.get("playId"):
            from urllib.parse import unquote
            play_url = unquote(request.query_params["playId"])
            return JSONResponse({
                "url": play_url,
                "header": "Referer:https://www.bilibili.com/",
                "user_agent": "Mozilla/5.0",
                "parse": 0,
            })

        # ★ play 处理器（TVBox 发 play=URL，支持 CDN 直链、A站代理、B站代理）
        if (not ac or ac == "play") and request.query_params.get("play"):
            from urllib.parse import unquote
            play_url = unquote(request.query_params["play"])

            # 直链模式：B站 CDN 或 A站 M3U8/代理 URL
            if "bili_proxy=" in play_url:
                # 已经是我们的代理 URL，透传
                return JSONResponse({
                    "url": play_url,
                    "header": "",
                    "user_agent": "",
                    "parse": 0,
                })
            if "bilivideo" in play_url or "hdslb" in play_url:
                from urllib.parse import quote
                proxy_url = f"{base_url}/api?bili_proxy={quote(play_url)}"
                return JSONResponse({
                    "url": proxy_url,
                    "header": "",
                    "user_agent": "",
                    "parse": 0,
                })
            if "acfun" in play_url:
                return JSONResponse({
                    "url": play_url,
                    "header": "Referer:https://www.acfun.cn/",
                    "user_agent": "Mozilla/5.0",
                    "parse": 0,
                })

            # 代理模式：api?id=BVxxx 格式，重新获取 CDN 直链
            from urllib.parse import urlparse, parse_qs as _urlparse_qs
            parsed = urlparse(play_url)
            params = _urlparse_qs(parsed.query)
            bvid = params.get("id", [None])[0]
            if not bvid:
                return JSONResponse({"error": "无法解析播放地址"}, status_code=400)
            pn = int(params.get("p", [1])[0])
            vi = await video_info(bvid)
            if not vi:
                return JSONResponse({"error": "获取视频信息失败"}, status_code=404)
            pages = vi.get("pages", [])
            cid = 0
            for pg_ in pages:
                if pg_.get("page", 0) == pn:
                    cid = pg_.get("cid", 0)
                    break
            if not cid and pages:
                cid = pages[0].get("cid", 0)
            if not cid:
                return JSONResponse({"error": "无法获取CID"}, status_code=404)
            from .player import bili_get_play_url as _bili_get_play_url
            cdn_url = await _bili_get_play_url(bvid, cid)
            if cdn_url:
                from urllib.parse import quote
                pu = f"{base_url}/api?bili_proxy={quote(cdn_url)}"
                return JSONResponse({
                    "url": pu,
                    "header": "",
                    "user_agent": "",
                    "parse": 0,
                })
            proxy_url = f"{base_url}/api?id={bvid}"
            if pn > 1:
                proxy_url += f"&p={pn}"
            return JSONResponse({
                "url": proxy_url,
                "header": "",
                "user_agent": "",
                "parse": 0,
            })


        elif ac == "detail":
            ids_param = request.query_params.get("ids", "")
            # TVBox 有些版本点类目时请求 ac=detail&t=xxx 而不是 ac=videolist
            if not ids_param:
                t_param = request.query_params.get("t", "")
                if t_param:
                    try:
                        pg_int = int(request.query_params.get("pg", 1))
                    except:
                        pg_int = 1
                    return await _handle_videolist(filtered_cfg, t_param, pg_int)
            return await _handle_detail(ids_param, base_url)
        elif ac == "search":
            return await _handle_search(filtered_cfg, wd, pg)
        elif not ac and request.query_params.get("filter") == "true":
            # TVBox 有些版本先请求 filter=true 获取分类筛选配置
            # 必须返回 class 列表，否则 TVBox 认为没数据
            filt_cfg = _filter_config(cfg, ids, sub)
            classes = _build_category_list(filt_cfg)
            return JSONResponse({"class": classes, "filters": {}})
        else:
            return JSONResponse({"error": "未知操作"}, status_code=400)

    @router.get("/api/sub/{sub_name}")
    async def api_sub_route(sub_name: str, request: Request):
        """TVBox 某些版本不支持 api URL 带 query，通过路径传递 sub"""
        from urllib.parse import urlencode
        qs = dict(request.query_params)
        qs["sub"] = sub_name
        internal_url = f"http://127.0.0.1:8080/api?{urlencode(qs)}"
        import httpx
        from fastapi.responses import Response
        orig_host = request.headers.get("host", "127.0.0.1:5081")
        async with httpx.AsyncClient() as cli:
            try:
                resp = await cli.get(
                    internal_url,
                    headers={"Host": orig_host},
                    timeout=60.0,
                    follow_redirects=False,
                )
                return Response(
                    content=resp.content,
                    media_type=resp.headers.get("content-type", "application/json"),
                    status_code=resp.status_code,
                )
            except Exception as e:
                return JSONResponse({"error": f"内部代理失败: {str(e)}"}, status_code=502)

    @router.get("/api/spider/{sub_name}")
    async def api_spider_route(sub_name: str, request: Request):
        """UZ spider 路径式 sub 传递（防 APP 丢掉 query 参数）"""
        import httpx
        from fastapi.responses import Response
        orig_host = request.headers.get("host", "127.0.0.1:5081")
        internal_url = f"http://127.0.0.1:8080/api?t=spider&sub={sub_name}"
        async with httpx.AsyncClient() as cli:
            try:
                resp = await cli.get(
                    internal_url,
                    headers={"Host": orig_host},
                    timeout=60.0,
                    follow_redirects=False,
                )
                return Response(
                    content=resp.content,
                    media_type=resp.headers.get("content-type", "application/javascript"),
                    status_code=resp.status_code,
                )
            except Exception as e:
                return JSONResponse({"error": f"内部代理失败: {str(e)}"}, status_code=502)

    app.include_router(router)


# ═══════════════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════════════

def _filter_config(cfg: dict, ids: str = "", sub: str = "") -> dict:
    """根据 ids/sub 参数过滤配置"""
    allowed = {}
    if sub and not ids:
        for s in cfg.get("subscriptions", []):
            if s.get("name") == sub:
                # 优先 groups，其次旧格式 items
                sg = s.get("groups")
                if sg:
                    for g in sg:
                        for item in g.get("items", []):
                            allowed[item] = True
                else:
                    for item in s.get("items", []):
                        allowed[item] = True
                break
        ids = ",".join(allowed.keys())
    if ids:
        for item_id in ids.split(","):
            item_id = item_id.strip()
            if item_id:
                allowed[item_id] = True

    if not allowed:
        return cfg

    result = dict(cfg)
    # B站合集
    result["series"] = [s for s in cfg.get("series", []) if f"series_{s.get('id','') or s.get('series_id','')}" in allowed]
    result["bili_series"] = [s for s in cfg.get("bili_series", []) if f"series_{s.get('series_id','')}" in allowed]
    # B站UP主
    result["ups"] = [u for u in cfg.get("ups", []) if f"up_{u.get('mid','')}" in allowed]
    result["bili_ups"] = [u for u in cfg.get("bili_ups", []) if f"up_{u.get('mid','')}" in allowed]
    # A站合辑
    result["acfun_albums"] = [a for a in cfg.get("acfun_albums", []) if f"acfun_album_{a.get('id','')}" in allowed]
    # A站UP主
    result["acfun_ups"] = [u for u in cfg.get("acfun_ups", []) if f"acfun_up_{u.get('mid','')}" in allowed]
    # 订阅过滤：只保留匹配的订阅
    if sub:
        result["subscriptions"] = [s for s in cfg.get("subscriptions", []) if s.get("name") == sub]
    elif ids:
        result["subscriptions"] = [s for s in cfg.get("subscriptions", []) if any(it in allowed for it in s.get("items", []))]
    return result


def _build_category_list(cfg: dict) -> list:
    classes = []
    # 如果有订阅使用了 groups 格式（有实际分类名），则不显示单个 item 分类
    _has_grouped_subs = any(
        any(g.get("class_name") for g in (s.get("groups") or []))
        for s in cfg.get("subscriptions", [])
    )
    if not _has_grouped_subs:
        for s in cfg.get("series", []) or cfg.get("bili_series", []):
            sid = s.get("id") or s.get("series_id", "")
            name = s.get("name", f"合集 #{sid}")
            classes.append({"type_id": f"series_{sid}", "type_name": f"📁 {name}"})
        for u in cfg.get("ups", []) or cfg.get("bili_ups", []):
            mid = u.get("mid", "")
            name = u.get("name", f"UP #{mid}")
            classes.append({"type_id": f"up_{mid}", "type_name": f"👤 {name}"})
        for a in cfg.get("acfun_albums", []):
            aid = a.get("id", "")
            name = a.get("name", f"A站合辑 #{aid}")
            classes.append({"type_id": f"acfun_album_{aid}", "type_name": f"🎟 {name}"})
        for u in cfg.get("acfun_ups", []):
            mid = u.get("mid", "")
            name = u.get("name", f"A站UP #{mid}")
            classes.append({"type_id": f"acfun_up_{mid}", "type_name": f"🎥 {name}"})
    for s in cfg.get("subscriptions", []):
        groups = s.get("groups")
        if groups:
            for gi, g in enumerate(groups):
                cn = g.get("class_name", "")
                if not cn:
                    # 未命名分组：从 items 查找实际名字
                    for it in g.get("items", []):
                        if it.startswith("acfun_up_"):
                            uid = it[9:]
                            for u in cfg.get("acfun_ups", []):
                                if str(u.get("mid", "")) == uid:
                                    cn = u.get("name", f"A站UP #{uid}")
                                    break
                        elif it.startswith("series_"):
                            sid = it[7:]
                            for s2 in cfg.get("bili_series", []):
                                if str(s2.get("series_id", "")) == sid:
                                    cn = s2.get("name", sid)
                                    break
                        elif it.startswith("up_"):
                            mid = it[3:]
                            for u2 in cfg.get("bili_ups", []):
                                if str(u2.get("mid", "")) == mid:
                                    cn = u2.get("name", f"UP #{mid}")
                                    break
                        elif it.startswith("acfun_album_"):
                            aid = it[12:]
                            for a2 in cfg.get("acfun_albums", []):
                                if str(a2.get("id", "")) == aid:
                                    cn = a2.get("name", f"A站合辑 #{aid}")
                                    break
                        elif it.startswith("acfun_video_"):
                            vid = it[12:]
                            for av in cfg.get("acfun_videos", []):
                                if av.get("id") == vid:
                                    cn = av.get("name", f"A站视频 #{vid}")
                                    break
                        elif it.startswith("room_"):
                            rid = it[5:]
                            for r in cfg.get("bili_rooms", []):
                                if str(r.get("room_id", "")) == rid:
                                    cn = r.get("name", f"直播间 #{rid}")
                                    break
                        elif it.startswith("video_"):
                            vid = it[6:]
                            for bv in cfg.get("bili_videos", []):
                                if bv.get("id") == vid:
                                    cn = bv.get("name", vid)
                                    break
                        if cn:
                            break
                if not cn:
                    cn = f"组 {gi}"
                classes.append({"type_id": f"sub_{s['name']}_{gi}", "type_name": cn})
        else:
            cn = s.get("class_name", "")
            if cn:
                classes.append({"type_id": f"sub_{s['name']}", "type_name": cn})
            else:
                # 无 groups 也无 class_name：每个 item 单独成分类
                for idx, it in enumerate(s.get("items", [])):
                    item_cn = ""
                    if it.startswith("acfun_up_"):
                        uid = it[9:]
                        for u in cfg.get("acfun_ups", []):
                            if str(u.get("mid", "")) == uid:
                                item_cn = u.get("name", f"A站UP #{uid}")
                                break
                    elif it.startswith("series_"):
                        sid = it[7:]
                        for s2 in cfg.get("bili_series", []):
                            if str(s2.get("series_id", "")) == sid:
                                item_cn = s2.get("name", sid)
                                break
                    elif it.startswith("up_"):
                        mid = it[3:]
                        for u2 in cfg.get("bili_ups", []):
                            if str(u2.get("mid", "")) == mid:
                                item_cn = u2.get("name", f"UP #{mid}")
                                break
                    elif it.startswith("acfun_album_"):
                        aid = it[12:]
                        for a2 in cfg.get("acfun_albums", []):
                            if str(a2.get("id", "")) == aid:
                                item_cn = a2.get("name", f"A站合辑 #{aid}")
                                break
                    elif it.startswith("acfun_video_"):
                        vid = it[12:]
                        for av in cfg.get("acfun_videos", []):
                            if av.get("id") == vid:
                                item_cn = av.get("name", f"A站视频 #{vid}")
                                break
                    elif it.startswith("room_"):
                        rid = it[5:]
                        for r in cfg.get("bili_rooms", []):
                            if str(r.get("room_id", "")) == rid:
                                item_cn = r.get("name", f"直播间 #{rid}")
                                break
                    elif it.startswith("video_"):
                        vid = it[6:]
                        for bv in cfg.get("bili_videos", []):
                            if bv.get("id") == vid:
                                item_cn = bv.get("name", vid)
                                break
                    if not item_cn:
                        item_cn = f"未分类 {idx}"
                    classes.append({"type_id": f"sub_{s['name']}_{idx}", "type_name": item_cn})
    return classes


async def _handle_home(cfg: dict):
    classes = _build_category_list(cfg)
    # 填充推荐视频：取前3个分类的前5个视频
    list_data = []
    for c in classes[:3]:
        try:
            resp = await _handle_videolist(cfg, c["type_id"], 1)
            body = json.loads(resp.body.decode())
            for v in body.get("list", [])[:5]:
                list_data.append({
                    "vod_id": v.get("vod_id", ""),
                    "vod_name": v.get("vod_name", ""),
                    "type_id": c["type_id"],
                    "type_name": c["type_name"],
                    "vod_remarks": v.get("vod_remarks", ""),
                    "vod_pic": v.get("vod_pic", ""),
                })
        except Exception as e:
            pass
    return JSONResponse({"class": classes, "list": list_data})


def _handle_classes(cfg: dict):
    classes = _build_category_list(cfg)
    return JSONResponse({"class": classes})


async def _handle_videolist(cfg: dict, type_id: str, page: int):
    ps = 30
    offset = (page - 1) * ps

    if type_id.startswith("series_"):
        sid = type_id[7:]
        s = None
        for x in cfg.get("series", []) or cfg.get("bili_series", []):
            if str(x.get("id") or x.get("series_id", "")) == sid:
                s = x
                break
        if not s:
            return JSONResponse({"list": [], "total": 0})
        s_mid = str(s.get("mid", ""))
        fmt = s.get("fmt", "channel")
        videos_data = await series_videos(int(sid), mid=s_mid, pn=1, ps=100, fmt=fmt)
        if not videos_data:
            return JSONResponse({"list": [], "total": 0})
        videos = videos_data.get("videos", [])
        total = len(videos)
        page_videos = videos[offset:offset + ps]
        vlist = [{
            "vod_id": f"bili_{v['bvid']}_1",
            "vod_name": v.get("title", ""),
            "vod_pic": v.get("pic", ""),
            "vod_remarks": _fmt_dur(v.get("duration", 0)),
        } for v in page_videos if v.get("bvid")]
        return JSONResponse({
            "list": vlist, "total": total, "page": page,
            "pagecount": max(1, math.ceil(total / ps)), "limit": ps,
        })

    if type_id.startswith("up_"):
        mid = type_id[3:]
        videos_data = await up_videos(int(mid), pn=1, ps=50)
        if not videos_data:
            return JSONResponse({"list": [], "total": 0})
        videos = videos_data.get("videos", [])
        total = len(videos)
        page_videos = videos[offset:offset + ps]
        vlist = [{
            "vod_id": f"bili_{v['bvid']}_1",
            "vod_name": v.get("title", ""),
            "vod_pic": v.get("pic", ""),
            "vod_remarks": str(v.get("duration", "")),
        } for v in page_videos if v.get("bvid")]
        return JSONResponse({
            "list": vlist, "total": total, "page": page,
            "pagecount": max(1, math.ceil(total / ps)), "limit": ps,
        })

    if type_id.startswith("acfun_album_"):
        aid = type_id[12:]
        videos = await fetch_album_videos(aid)
        if not videos:
            return JSONResponse({"list": [], "total": 0})
        total = len(videos)
        page_videos = videos[offset:offset + ps]
        vlist = [{
            "vod_id": f"acfun_{v['id']}",
            "vod_name": v.get("title", ""),
            "vod_pic": v.get("cover", ""),
            "vod_remarks": _fmt_dur(v.get("duration", 0)),
        } for v in page_videos if v.get("id")]
        return JSONResponse({
            "list": vlist, "total": total, "page": page,
            "pagecount": max(1, math.ceil(total / ps)), "limit": ps,
        })

    if type_id.startswith("acfun_up_"):
        mid = type_id[9:]
        videos = await fetch_user_videos(int(mid))
        if not videos:
            return JSONResponse({"list": [], "total": 0})
        total = len(videos)
        page_videos = videos[offset:offset + ps]
        vlist = [{
            "vod_id": f"acfun_{v['id']}",
            "vod_name": v.get("title", ""),
            "vod_pic": v.get("cover", ""),
            "vod_remarks": _fmt_dur(v.get("duration", 0)),
        } for v in page_videos if v.get("id")]
        return JSONResponse({
            "list": vlist, "total": total, "page": page,
            "pagecount": max(1, math.ceil(total / ps)), "limit": ps,
        })

    if type_id.startswith("sub_"):
        rest = type_id[4:]
        # 支持分组索引: sub_订阅名_0, sub_订阅名_1
        parts = rest.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            sub_name = parts[0]
            group_idx = int(parts[1])
        else:
            sub_name = rest
            group_idx = -1

        sub = None
        for s in cfg.get("subscriptions", []):
            if s.get("name") == sub_name:
                sub = s
                break
        if not sub:
            return JSONResponse({"list": [], "total": 0})

        # 取 items：优先 groups，其次旧格式 items
        sg = sub.get("groups")
        if sg and group_idx >= 0:
            items = sg[group_idx].get("items", []) if group_idx < len(sg) else []
        elif group_idx >= 0 and not sg:
            # 无 groups 但有索引：按索引取单个 item
            all_items = sub.get("items", [])
            items = [all_items[group_idx]] if group_idx < len(all_items) else []
        elif sg:
            items = []
            for g in sg:
                items.extend(g.get("items", []))
        else:
            items = sub.get("items", [])

        # 展平：拉取所有实际视频，不返回占位符
        all_videos = []
        for it in items:
            if it.startswith("video_"):
                vid = it[6:]
                try:
                    vi = await video_info(vid)
                    if vi:
                        all_videos.append({"vod_id": f"bili_{vid}_1", "vod_name": vi.get("title", vid), "vod_pic": vi.get("pic", ""), "vod_remarks": ""})
                except:
                    all_videos.append({"vod_id": f"bili_{vid}_1", "vod_name": vid, "vod_pic": "", "vod_remarks": ""})
            elif it.startswith("acfun_video_"):
                vid = it[12:]
                try:
                    from .acfun import fetch_video_info as _af_vi
                    vi = await _af_vi(vid)
                    if vi:
                        all_videos.append({"vod_id": f"acfun_{vid}", "vod_name": vi.get("title", vid), "vod_pic": vi.get("cover", ""), "vod_remarks": ""})
                    else:
                        all_videos.append({"vod_id": f"acfun_{vid}", "vod_name": vid, "vod_pic": "", "vod_remarks": ""})
                except:
                    all_videos.append({"vod_id": f"acfun_{vid}", "vod_name": vid, "vod_pic": "", "vod_remarks": ""})
            elif it.startswith("room_"):
                rid = it[5:]
                rname = rid
                for r in cfg.get("bili_rooms", []):
                    if str(r.get("room_id", "")) == rid:
                        rname = r.get("name", rid)
                        break
                all_videos.append({"vod_id": it, "vod_name": rname, "vod_pic": "", "vod_remarks": "直播"})
            elif it.startswith("series_"):
                sid = it[7:]
                try:
                    # 从配置查 mid 和 fmt
                    s_mid = ""
                    s_fmt = "channel"
                    for s in cfg.get("bili_series", []):
                        if str(s.get("series_id", "")) == sid:
                            s_mid = str(s.get("mid", ""))
                            s_fmt = s.get("fmt", "channel")
                            break
                    if not s_mid:
                        for s in cfg.get("series", []):
                            if str(s.get("id", "")) == sid or str(s.get("series_id", "")) == sid:
                                s_mid = str(s.get("mid", ""))
                                s_fmt = s.get("fmt", "channel")
                                break
                    sv = await series_videos(int(sid), mid=s_mid, pn=1, ps=100, fmt=s_fmt)
                    if sv and sv.get("videos"):
                        for v in sv["videos"]:
                            bvid = v.get("bvid", v.get("aid", ""))
                            if bvid:
                                all_videos.append({"vod_id": f"bili_{bvid}_1", "vod_name": v.get("title", ""), "vod_pic": v.get("pic", ""), "vod_remarks": ""})
                except:
                    pass
            elif it.startswith("up_"):
                mid = it[3:]
                try:
                    uv = await up_videos(int(mid))
                    if uv and uv.get("videos"):
                        for v in uv["videos"]:
                            bvid = v.get("bvid", v.get("aid", ""))
                            if bvid:
                                all_videos.append({"vod_id": f"bili_{bvid}_1", "vod_name": v.get("title", ""), "vod_pic": v.get("pic", ""), "vod_remarks": ""})
                except:
                    pass
            elif it.startswith("acfun_album_"):
                aid = it[12:]
                try:
                    av = await fetch_album_videos(aid)
                    if av:
                        for v in av:
                            vid = v.get("id", "")
                            if vid:
                                all_videos.append({"vod_id": f"acfun_{vid}", "vod_name": v.get("title", ""), "vod_pic": v.get("cover", ""), "vod_remarks": ""})
                except:
                    pass
            elif it.startswith("acfun_up_"):
                uid = it[9:]
                try:
                    uvv = await fetch_user_videos(uid)
                    if uvv:
                        for v in uvv:
                            vid = v.get("id", "")
                            if vid:
                                all_videos.append({"vod_id": f"acfun_{vid}", "vod_name": v.get("title", ""), "vod_pic": v.get("cover", ""), "vod_remarks": ""})
                except:
                    pass
        total = len(all_videos)
        page_videos = all_videos[offset:offset + ps]
        return JSONResponse({
            "list": page_videos, "total": total, "page": page,
            "pagecount": max(1, math.ceil(total / ps)), "limit": ps,
        })

    return JSONResponse({"list": [], "total": 0})


async def _handle_detail(ids_param: str, base_url: str):
    if not ids_param:
        return JSONResponse({"list": []})
    parts = ids_param.split("_")
    if len(parts) < 2:
        return JSONResponse({"list": []})
    
    # A站视频
    if parts[0] == "acfun":
        aid = parts[1]
        from .acfun import fetch_video_info as _acfun_fetch_vi
        vi = await _acfun_fetch_vi(aid, force=True)
        if not vi:
            return JSONResponse({"list": []})
        vlist = vi.get("videoList", [])
        if vlist and len(vlist) > 1:
            episodes = []
            for sv in vlist:
                sv_id = str(sv.get("id", ""))
                sv_title = sv.get("title", f"第{len(episodes)+1}集")
                pu = f"{base_url}/acfun?id={aid}&play=1&direct=true&cid={sv_id}"
                episodes.append(f"{sv_title}${pu}")
            play_url = "#".join(episodes)
            remarks = f"{len(vlist)}集"
        else:
            pu = f"{base_url}/acfun?id={aid}&play=1&direct=true"
            if vlist and len(vlist) == 1:
                sv_title = vlist[0].get("title", "播放")
                play_url = f"{sv_title}${pu}"
            else:
                play_url = f"播放${pu}"
            remarks = "1集"
        video = {
            "vod_id": ids_param,
            "vod_name": vi.get("title", ""),
            "vod_pic": vi.get("cover", ""),
            "vod_content": vi.get("description", ""),
            "vod_play_from": "A站",
            "vod_play_url": play_url,
            "vod_remarks": remarks,
        }
        return JSONResponse({"list": [video]})

    bvid = parts[1]
    if not re.match(r"^BV[0-9A-Za-z]{10}$", bvid):
        # 可能是 A站 id
        return JSONResponse({"list": []})

    vi = await video_info(bvid)
    if not vi:
        return JSONResponse({"list": []})

    pages = vi.get("pages", [])
    total = len(pages) or 1
    episodes = []

    from .player import bili_get_play_url as _bili_get_play_url

    if total > 1:
        for pg_ in pages:
            pn = int(pg_.get("page", 0))
            cid = int(pg_.get("cid", 0))
            part = pg_.get("part", f"第{pn}集")
            if cid:
                cdn_url = await _bili_get_play_url(bvid, cid)
                if cdn_url:
                    _BILI_CDN_CACHE[f"{bvid}_{pn}"] = {"cdn_url": cdn_url, "cid": cid, "expires_at": time.time() + 600}
                    episodes.append(f"{part}${base_url}/api?id={bvid}&p={pn}")
                    continue
            episodes.append(f"{part}${base_url}/api?id={bvid}&p={pn}")
    else:
        cid = int(pages[0].get("cid", 0)) if pages else 0
        if cid:
            cdn_url = await _bili_get_play_url(bvid, cid)
            if cdn_url:
                _BILI_CDN_CACHE[f"{bvid}_1"] = {"cdn_url": cdn_url, "cid": cid, "expires_at": time.time() + 600}
                episodes.append(f"播放${base_url}/api?id={bvid}&p=1")
            else:
                episodes.append(f"播放${base_url}/api?id={bvid}")
        else:
            episodes.append(f"播放${base_url}/api?id={bvid}")

    video = {
        "vod_id": ids_param,
        "vod_name": vi.get("title", ""),
        "vod_pic": vi.get("pic", ""),
        "vod_actor": vi.get("owner", {}).get("name", ""),
        "vod_content": vi.get("desc", ""),
        "vod_play_from": "B站",
        "vod_play_url": "#".join(episodes),
        "vod_remarks": f"{total}集" if total > 1 else "",
    }
    return JSONResponse({"list": [video]})


async def _handle_search(cfg: dict, keyword: str, page: int):
    if not keyword:
        return JSONResponse({"list": [], "total": 0})

    kw = keyword.lower()
    results = []

    # 搜索合集视频
    for s in cfg.get("series", []) or cfg.get("bili_series", []):
        sid = s.get("id") or s.get("series_id", "")
        s_mid = str(s.get("mid", ""))
        fmt = s.get("fmt", "channel")
        if not kw or kw in (s.get("name", "").lower()):
            videos_data = await series_videos(int(sid), mid=s_mid, pn=1, ps=100, fmt=fmt)
            if videos_data:
                for v in videos_data.get("videos", []):
                    if kw in v.get("title", "").lower():
                        results.append({
                            "vod_id": f"bili_{v['bvid']}_1",
                            "vod_name": v.get("title", ""),
                            "vod_pic": v.get("pic", ""),
                            "vod_remarks": s.get("name", ""),
                        })

    # 搜索UP主视频
    for u in cfg.get("ups", []) or cfg.get("bili_ups", []):
        mid = u.get("mid", "")
        videos_data = await up_videos(int(mid), pn=1, ps=50)
        if videos_data:
            for v in videos_data.get("videos", []):
                if kw in v.get("title", "").lower():
                    results.append({
                        "vod_id": f"bili_{v['bvid']}_1",
                        "vod_name": v.get("title", ""),
                        "vod_pic": v.get("pic", ""),
                        "vod_remarks": u.get("name", ""),
                    })

    # 搜索A站合辑视频
    for a in cfg.get("acfun_albums", []):
        aid = a.get("id", "")
        videos = await fetch_album_videos(aid)
        if videos:
            for v in videos:
                if kw in v.get("title", "").lower():
                    results.append({
                        "vod_id": f"acfun_{v['id']}",
                        "vod_name": v.get("title", ""),
                        "vod_pic": v.get("cover", ""),
                        "vod_remarks": a.get("name", ""),
                    })

    # 搜索A站UP主视频
    for u in cfg.get("acfun_ups", []):
        mid = u.get("mid", "")
        videos = await fetch_user_videos(int(mid))
        if videos:
            for v in videos:
                if kw in v.get("title", "").lower():
                    results.append({
                        "vod_id": f"acfun_{v['id']}",
                        "vod_name": v.get("title", ""),
                        "vod_pic": v.get("cover", ""),
                        "vod_remarks": u.get("name", ""),
                    })

    ps = 30
    offset = (page - 1) * ps
    total = len(results)
    paged = results[offset:offset + ps]
    return JSONResponse({"list": paged, "total": total})


def _fmt_dur(sec):
    if not sec:
        return ""
    m = sec // 60
    s = sec % 60
    return f"{m}:{s:02d}"


# ═══ TVBox JS 蜘蛛（代理到 TVBox JSON API） ═══
async def _handle_tvbox_spider(request: Request, cfg: dict, base_url: str):
    sub_param = request.query_params.get("sub", "")
    api_base = f"{base_url}/api"
    if sub_param:
        api_base += f"?sub={sub_param}"
    
    js = f"""var apiUrl = \"{api_base}&\";
const UA = \"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\";

function init(ext) {{}}

async function homeContent() {{
  var r = await req(apiUrl + \"ac=list\", {{headers: {{\"User-Agent\": UA}}}});
  return r;
}}

async function homeVideoContent() {{
  return {{list: []}};
}}

async function categoryContent(tid, pg, filter, extend) {{
  try {{
    var r = await req(apiUrl + \"ac=videolist&t=\" + tid + \"&pg=\" + pg, {{headers: {{\"User-Agent\": UA}}}});
    return r;
  }} catch (e) {{
    return {{list: [], total: 0}};
  }}
}}

async function detailContent(ids) {{
  try {{
    var r = await req(apiUrl + \"ac=detail&ids=\" + ids, {{headers: {{\"User-Agent\": UA}}}});
    return r;
  }} catch (e) {{
    return {{list: []}};
  }}
}}

async function searchContent(key, quick) {{
  if (!key) return {{list: []}};
  try {{
    var r = await req(apiUrl + \"ac=search&wd=\" + encodeURIComponent(key), {{headers: {{\"User-Agent\": UA}}}});
    return r;
  }} catch (e) {{
    return {{list: []}};
  }}
}}

async function playerContent(vod_id, flag, url, headers_config) {{
  return {{header: \"\", js: \"\", parse: 0, url: url}};
}}
"""
    return Response(content=js.strip(), media_type="application/javascript")


# UZ 蜘蛛脚本

# UZ 蜘蛛脚本
async def _handle_uz_spider(request: Request, cfg: dict):
    base_url = build_base_url(request)
    ids_param = request.query_params.get("ids", "")
    sub_param = request.query_params.get("sub", "")

    series_list = []
    ups_list = []

    for s in cfg.get("series", []) or cfg.get("bili_series", []):
        sid = s.get("id") or s.get("series_id", "")
        series_list.append({
            "series_id": sid,
            "mid": str(s.get("mid", "")),
            "name": s.get("name", f"合集 #{sid}"),
        })

    for u in cfg.get("ups", []) or cfg.get("bili_ups", []):
        mid = u.get("mid", "")
        ups_list.append({"mid": mid, "name": u.get("name", f"UP #{mid}")})

    acfun_albums = []
    for a in cfg.get("acfun_albums", []):
        aid = a.get("id", "")
        acfun_albums.append({"id": aid, "name": a.get("name", f"A站合辑 #{aid}")})

    acfun_ups = []
    for u in cfg.get("acfun_ups", []):
        mid = u.get("mid", "")
        acfun_ups.append({"mid": mid, "name": u.get("name", f"A站UP #{mid}")})

    # 过滤
    allowed = {}
    if sub_param and not ids_param:
        for s in cfg.get("subscriptions", []):
            if s.get("name") == sub_param:
                # 优先 groups，其次旧格式 items
                sg = s.get("groups")
                if sg:
                    for g in sg:
                        for item in g.get("items", []):
                            allowed[item] = True
                else:
                    for item in s.get("items", []):
                        allowed[item] = True
                break
        ids_param = ",".join(allowed.keys())
    # 检查订阅的 groups：区分有名字的组和无名字的组
    _named_items = set()    # 有分类名的组里的 item，走 SUBS
    _unnamed_items = set()  # 无分类名的组里的 item，走个体列表
    for s in cfg.get("subscriptions", []):
        if s.get("name") == sub_param and s.get("groups"):
            for g in s["groups"]:
                if g.get("class_name"):
                    for it in g.get("items", []):
                        _named_items.add(it)
                else:
                    for it in g.get("items", []):
                        _unnamed_items.add(it)
            break
    # 如果订阅有 named 分组，才清空个体列表（只显示分组分类）
    # unnamed 分组保持不变（显示为个体 item 分类）
    _only_named = bool(_named_items)
    if ids_param:
        for item_id in ids_param.split(","):
            item_id = item_id.strip()
            if item_id:
                allowed[item_id] = True
        if not _only_named:
            series_list = [s for s in series_list if f"series_{s['series_id']}" in allowed]
            ups_list = [u for u in ups_list if f"up_{u['mid']}" in allowed]
            acfun_albums = [a for a in acfun_albums if f"acfun_album_{a['id']}" in allowed]
            acfun_ups = [u for u in acfun_ups if f"acfun_up_{u['mid']}" in allowed]
        else:
            # 有命名分组时，只保留未命名组里的个体 item
            series_list = [s for s in series_list if f"series_{s['series_id']}" in _unnamed_items]
            ups_list = [u for u in ups_list if f"up_{u['mid']}" in _unnamed_items]
            acfun_albums = [a for a in acfun_albums if f"acfun_album_{a['id']}" in _unnamed_items]
            acfun_ups = [u for u in acfun_ups if f"acfun_up_{u['mid']}" in _unnamed_items]

    import json as _json
    # 构建SUBS列表：支持新格式 groups，未命名分组也用分类名
    _subs_groups = set()  # 记录哪些 item 被放进了SUBS，避免重复
    subs_list = []
    for s in cfg.get("subscriptions", []):
        if sub_param and s.get("name") != sub_param:
            continue
        sg = s.get("groups")
        if sg:
            for gi, g in enumerate(sg):
                cn = g.get("class_name", "")
                if not cn:
                    # 未命名分组：从 items 生成分类名
                    for it in g.get("items", []):
                        if it.startswith("acfun_up_"):
                            uid = it[9:]
                            for u in cfg.get("acfun_ups", []):
                                if str(u.get("mid", "")) == uid:
                                    cn = u.get("name", f"A站UP #{uid}")
                                    break
                        elif it.startswith("series_"):
                            sid = it[7:]
                            for s2 in cfg.get("bili_series", []):
                                if str(s2.get("series_id", "")) == sid:
                                    cn = s2.get("name", sid)
                                    break
                        elif it.startswith("up_"):
                            mid = it[3:]
                            for u2 in cfg.get("bili_ups", []):
                                if str(u2.get("mid", "")) == mid:
                                    cn = u2.get("name", f"UP #{mid}")
                                    break
                        elif it.startswith("acfun_album_"):
                            aid = it[12:]
                            for a2 in cfg.get("acfun_albums", []):
                                if str(a2.get("id", "")) == aid:
                                    cn = a2.get("name", f"A站合辑 #{aid}")
                                    break
                        elif it.startswith("acfun_video_"):
                            vid = it[12:]
                            for av in cfg.get("acfun_videos", []):
                                if av.get("id") == vid:
                                    cn = av.get("name", f"A站视频 #{vid}")
                                    break
                        elif it.startswith("room_"):
                            rid = it[5:]
                            for r in cfg.get("bili_rooms", []):
                                if str(r.get("room_id", "")) == rid:
                                    cn = r.get("name", f"直播间 #{rid}")
                                    break
                        elif it.startswith("video_"):
                            vid = it[6:]
                            for bv in cfg.get("bili_videos", []):
                                if bv.get("id") == vid:
                                    cn = bv.get("name", vid)
                                    break
                        if cn:
                            break
                if cn:
                    subs_list.append({"name": f"{s['name']}_{gi}", "class_name": cn})
                    for it in g.get("items", []):
                        _subs_groups.add(it)
        elif s.get("class_name"):
            subs_list.append({"name": s["name"], "class_name": s.get("class_name", "")})
            for it in s.get("items", []):
                _subs_groups.add(it)
        else:
            # 无 groups 也无 class_name：每个 item 单独一个分类
            for idx, it in enumerate(s.get("items", [])):
                cn = ""
                if it.startswith("acfun_up_"):
                    uid = it[9:]
                    for u in cfg.get("acfun_ups", []):
                        if str(u.get("mid", "")) == uid:
                            cn = u.get("name", f"A站UP #{uid}")
                            break
                elif it.startswith("series_"):
                    sid = it[7:]
                    for s2 in cfg.get("bili_series", []):
                        if str(s2.get("series_id", "")) == sid:
                            cn = s2.get("name", sid)
                            break
                elif it.startswith("up_"):
                    mid = it[3:]
                    for u2 in cfg.get("bili_ups", []):
                        if str(u2.get("mid", "")) == mid:
                            cn = u2.get("name", f"UP #{mid}")
                            break
                elif it.startswith("room_"):
                    rid = it[5:]
                    for r in cfg.get("bili_rooms", []):
                        if str(r.get("room_id", "")) == rid:
                            cn = r.get("name", f"直播间 #{rid}")
                            break
                elif it.startswith("acfun_video_"):
                    vid = it[12:]
                    for av in cfg.get("acfun_videos", []):
                        if av.get("id") == vid:
                            cn = av.get("name", f"A站视频 #{vid}")
                            break
                elif it.startswith("video_"):
                    vid = it[6:]
                    for bv in cfg.get("bili_videos", []):
                        if bv.get("id") == vid:
                            cn = bv.get("name", vid)
                            break
                if not cn:
                    cn = "未分类"
                subs_list.append({"name": f"{s['name']}_{idx}", "class_name": cn})
                _subs_groups.add(it)
    subs_json = _json.dumps(subs_list, ensure_ascii=False)

    # 如果订阅有分组（SUBS），从个体列表中移除已加入SUBS的item，避免重复
    if _subs_groups:
        series_list = [s for s in series_list if f"series_{s['series_id']}" not in _subs_groups]
        ups_list = [u for u in ups_list if f"up_{u['mid']}" not in _subs_groups]
        acfun_albums = [a for a in acfun_albums if f"acfun_album_{a['id']}" not in _subs_groups]
        acfun_ups = [u for u in acfun_ups if f"acfun_up_{u['mid']}" not in _subs_groups]

    series_json = _json.dumps(series_list, ensure_ascii=False)
    ups_json = _json.dumps(ups_list, ensure_ascii=False)
    acfun_albums_json = _json.dumps(acfun_albums, ensure_ascii=False)
    acfun_ups_json = _json.dumps(acfun_ups, ensure_ascii=False)

    js = f"""//@name:AB Player (B站/A站)
//@version:1
//@webSite:{base_url}
//@type:101
//@order:A

const BASE = "{base_url}/";
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36";

const SERIES = {series_json};
const UPS = {ups_json};
const ACFUN_ALBUMS = {acfun_albums_json};
const ACFUN_UPS = {acfun_ups_json};
const SUBS = {subs_json};

function buildURL(path, params) {{
  let url = BASE + path;
  const q = [];
  if (params) {{
    for (const k in params) {{
      if (params[k] !== null && params[k] !== undefined && params[k] !== "") {{
        q.push(k + "=" + encodeURIComponent(params[k]));
      }}
    }}
  }}
  if (q.length > 0) url += "?" + q.join("&");
  return url;
}}

async function getClassList(args) {{
  const list = [];
  for (const s of SERIES) {{
    list.push({{ type_id: "series_" + s.series_id, type_name: "📁 " + (s.name || "合集"), hasSubclass: false }});
  }}
  for (const u of UPS) {{
    list.push({{ type_id: "up_" + u.mid, type_name: "👤 " + (u.name || "UP主"), hasSubclass: false }});
  }}
  for (const a of ACFUN_ALBUMS) {{
    list.push({{ type_id: "acfun_album_" + a.id, type_name: "🎟 " + (a.name || "A站合辑"), hasSubclass: false }});
  }}
  for (const u of ACFUN_UPS) {{
    list.push({{ type_id: "acfun_up_" + u.mid, type_name: "🎥 " + (u.name || "A站UP"), hasSubclass: false }});
  }}
  const SUBS = {subs_json};
  for (const s of SUBS) {{
    if (s.class_name) {{
      list.push({{ type_id: "sub_" + s.name, type_name: s.class_name, hasSubclass: false }});
    }}
  }}
  return JSON.stringify({{ error: null, data: list }});
}}

async function getVideoList(args) {{
  const typeId = args.url || "";
  const page = args.page || 1;
  if (!typeId) return JSON.stringify({{ error: "缺ID", data: [], total: 0 }});

  let url = "";
  if (typeId.indexOf("series_") === 0) {{
    const sid = typeId.substring(7);
    url = buildURL("api", {{ action: "refresh_series", series: sid }});
  }} else if (typeId.indexOf("up_") === 0) {{
    const mid = typeId.substring(3);
    url = buildURL("api", {{ action: "refresh_up", mid: mid }});
  }} else if (typeId.indexOf("acfun_album_") === 0) {{
    const aid = typeId.substring(12);
    url = buildURL("api", {{ action: "refresh_acfun_album", album: aid }});
  }} else if (typeId.indexOf("acfun_up_") === 0) {{
    const mid = typeId.substring(8);
    url = buildURL("api", {{ action: "refresh_acfun_up", up: mid }});
  }} else if (typeId.indexOf("sub_") === 0) {{
    const subName = typeId.substring(4);
    url = buildURL("api", {{ action: "refresh_sub", sub: subName }});
  }}
  if (!url) return JSON.stringify({{ error: "未知分类", data: [], total: 0 }});

  try {{
    const res = await req(url, {{ headers: {{ "User-Agent": UA }} }});
    if (!res) return JSON.stringify({{ error: "无数据", data: [], total: 0 }});
    const videos = (res.data && res.data.videos) || res.videos;
    if (!videos || !videos.length) return JSON.stringify({{ error: "无数据", data: [], total: 0 }});
    const total = videos.length;
    const perPage = 30;
    const offset = (page - 1) * perPage;
    const list = [];
    for (let k = offset; k < offset + perPage && k < videos.length; k++) {{
      const v = videos[k];
      let vodId = "";
      let pic = "";
      if (v.id && !v.bvid) {{
        vodId = "acfun_" + v.id;
        pic = v.cover || "";
      }} else {{
        const bvid = v.bvid || v.aid || "";
        if (!bvid) continue;
        vodId = "bili_" + bvid + "_1";
        pic = v.pic || "";
      }}
      const ts = v.pubdate || v.ctime || (v.createTime ? Math.floor(v.createTime / 1000) : 0);
      let rem = "";
      if (ts) {{
        const d = new Date(ts * 1000);
        rem = d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate();
      }}
      list.push({{
        vod_id: vodId,
        vod_name: v.title || "",
        vod_pic: pic,
        type_id: typeId,
        vod_remarks: rem,
      }});
    }}
    return JSON.stringify({{ error: null, data: list, total: total }});
  }} catch (e) {{
    return JSON.stringify({{ error: String(e), data: [], total: 0 }});
  }}
}}

async function getVideoDetail(args) {{
  const vodId = args.url || "";
  if (!vodId) return JSON.stringify({{ error: "未知来源", data: null }});

  // A站
  if (vodId.indexOf("acfun_") === 0) {{
    const acId = vodId.substring(6);
    try {{
      const res = await req(buildURL("api", {{ info: acId }}), {{ headers: {{ "User-Agent": UA }} }});
      const title = (res && res.data && res.data.title) ? res.data.title : "A站视频";
      const videoList = (res && res.data && res.data.videoList) || [];
      let playUrl = "";
      let remarks = "";
      if (videoList.length > 1) {{
        const eps = [];
        for (const sv of videoList) {{
          const st = sv.title || ("第" + (eps.length + 1) + "集");
          eps.push(st + "$" + BASE + "acfun?id=" + acId + "&play=1&cid=" + sv.id);
        }}
        playUrl = eps.join("#");
        remarks = videoList.length + "集";
      }} else {{
        playUrl = "播放$" + BASE + "acfun?id=" + acId + "&play=1";
        remarks = "1集";
      }}
      return JSON.stringify({{
        error: null,
        data: {{
          vod_id: vodId, vod_name: title, vod_pic: "",
          type_name: "A站", vod_remarks: remarks,
          vod_content: remarks,
          vod_play_from: "A站",
          vod_play_url: playUrl,
        }}
      }});
    }} catch (e) {{
      return JSON.stringify({{ error: String(e), data: null }});
    }}
  }}

  // B站
  let bvid = vodId;
  if (vodId.indexOf("bili_") === 0) bvid = vodId.substring(5);
  const m = bvid.match(/^(BV[0-9A-Za-z]+)_\\d+$/);
  if (m) bvid = m[1];

  try {{
    const res = await req(buildURL("api", {{ info: bvid }}), {{ headers: {{ "User-Agent": UA }} }});
    if (!res || !res.data) return JSON.stringify({{ error: "获取失败", data: null }});
    const info = res.data;
    const pages = info.pages || [];
    const total = info.total || 0;
    const title = info.title || "";
    const episodes = [];
    for (let i = 0; i < total; i++) {{
      const pn = i + 1;
      const part = (pages[i] && pages[i].part) ? pages[i].part : "第" + pn + "集";
      episodes.push(part + "$" + BASE + "api?id=" + bvid + "&p=" + pn);
    }}
    return JSON.stringify({{
      error: null,
      data: {{
        vod_id: vodId, vod_name: title, vod_pic: "",
        type_name: "B站", vod_remarks: total + "集",
        vod_content: total + "集",
        vod_play_from: "B站",
        vod_play_url: episodes.join("#"),
      }}
    }});
  }} catch (e) {{
    return JSON.stringify({{ error: String(e), data: null }});
  }}
}}

async function getVideoPlayUrl(args) {{
  const playId = args.url || "";
  if (!playId) return JSON.stringify({{ error: "空" }});
  const isAcfun = playId.indexOf("/acfun?") !== -1;
  const site = isAcfun ? "acfun.cn" : "bilibili.com";
  return JSON.stringify({{
    error: null,
    url: playId,
    header: "Referer:https://www." + site + "/\\r\\nUser-Agent:" + UA,
    user_agent: UA,
    parse: 0,
  }});
}}

async function searchVideo(args) {{
  const keyword = args.searchWord || "";
  const page = args.page || 1;
  if (!keyword) return JSON.stringify({{ error: null, data: [], total: 0 }});
  const kw = keyword.toLowerCase();
  const seen = {{}};
  const results = [];

  // A站搜索
  for (const a of ACFUN_ALBUMS) {{
    try {{
      const res = await req(buildURL("api", {{ action: "refresh_acfun_album", album: a.id }}), {{ headers: {{ "User-Agent": UA }} }});
      if (!res) continue;
      const videos = (res.data && res.data.videos) || res.videos;
      if (!videos) continue;
      for (const v of videos) {{
        const aid = v.id || "";
        const title = v.title || "";
        if (!aid || seen[aid]) continue;
        if (title.toLowerCase().indexOf(kw) !== -1) {{
          seen[aid] = true;
          results.push({{ vod_id: "acfun_" + aid, vod_name: title, vod_pic: v.cover || "", vod_remarks: "" }});
        }}
      }}
    }} catch (e) {{}}
  }}
  for (const u of ACFUN_UPS) {{
    try {{
      const res = await req(buildURL("api", {{ action: "refresh_acfun_up", up: u.mid }}), {{ headers: {{ "User-Agent": UA }} }});
      if (!res) continue;
      const videos = (res.data && res.data.videos) || res.videos;
      if (!videos) continue;
      for (const v of videos) {{
        const aid = v.id || "";
        const title = v.title || "";
        if (!aid || seen[aid]) continue;
        if (title.toLowerCase().indexOf(kw) !== -1) {{
          seen[aid] = true;
          results.push({{ vod_id: "acfun_" + aid, vod_name: title, vod_pic: v.cover || "", vod_remarks: "" }});
        }}
      }}
    }} catch (e) {{}}
  }}

  // B站搜索
  for (const s of SERIES) {{
    try {{
      const res = await req(buildURL("api", {{ action: "refresh_series", series: s.series_id }}), {{ headers: {{ "User-Agent": UA }} }});
      if (!res) continue;
      const videos = (res.data && res.data.videos) || res.videos;
      if (!videos) continue;
      for (const v of videos) {{
        const bvid = v.bvid || v.aid || "";
        const title = v.title || "";
        if (!bvid || seen[bvid]) continue;
        if (title.toLowerCase().indexOf(kw) !== -1) {{
          seen[bvid] = true;
          const ts = v.pubdate || v.ctime || 0;
          let rem = "";
          if (ts) {{
            const d = new Date(ts * 1000);
            rem = d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate();
          }}
          results.push({{ vod_id: "bili_" + bvid + "_1", vod_name: title, vod_pic: v.pic || "", vod_remarks: rem }});
        }}
      }}
    }} catch (e) {{}}
  }}

  const total = results.length;
  const perPage = 30;
  const offset = (page - 1) * perPage;
  return JSON.stringify({{ error: null, data: results.slice(offset, offset + perPage), total: total }});
}}
"""
    return Response(content=js, media_type="application/javascript; charset=utf-8")

# 需要导入 RedirectResponse
from fastapi.responses import RedirectResponse