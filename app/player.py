"""B站播放路由 - 合集、UP主、单视频、直播"""

import json, re, urllib.parse
from typing import Optional
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, JSONResponse, StreamingResponse
from .config import cache_get, cache_set, BASE_DIR, load_config, http_get, http_get_json
from .bilibili import (
    video_info, video_playurl, live_info, live_playurl,
    up_info, up_videos, series_videos, search_videos,
)
from .m3u import build_bili_m3u

from jinja2 import Environment, FileSystemLoader
import time
_j2_env = Environment(loader=FileSystemLoader(str(BASE_DIR / "app" / "templates")))
_j2_env.filters["timestamp_to_date"] = lambda ts: time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""
def _render(name, ctx=None, **kw):
    if ctx:
        kw.update(ctx)
    return _j2_env.get_template(name).render(**kw)


async def bili_get_play_url(bvid: str, cid: int) -> Optional[str]:
    """获取B站视频播放地址。优先 FLV（音视频一体），降级至 DASH video-only"""
    # 从低到高尝试各画质，优先找 FLV（含音频）
    for qn in [80, 64, 32, 16]:
        info = await video_playurl(bvid, cid, qn=qn)
        if info and info.get("flv"):
            return info["flv"][0]["url"]
    # 通过 fnval=4048 取 DASH（含 1080P+，需要播放器合并音视频）
    info = await video_playurl(bvid, cid, qn=116, fnval=4048)
    if info:
        if info.get("flv"):
            return info["flv"][0]["url"]
        if info.get("dash") and info["dash"].get("video"):
            return info["dash"]["video"][0]["url"]
    return None


def register(app):
    router = APIRouter()

    @router.head("/bili", include_in_schema=False)
    async def bili_head(
        request: Request,
        id: str = Query(None),
    ):
        """HEAD 探活：播放器检查 URL 有效性时快速返回 200"""
        return Response(
            status_code=200,
            headers={"Access-Control-Allow-Origin": "*", "Content-Type": "video/mp4"},
        )

    @router.get("/bili")
    async def bili_route(
        request: Request,
        id: str = Query(None),
        p: int = Query(1),
        info: str = Query(None),
        series: str = Query(None),
        mid: str = Query(None),
        up: str = Query(None),
        m3u: str = Query(None),
        room: str = Query(None),
        live_m3u_room: str = Query(None),
        live_proxy: str = Query(None),
        live_url: str = Query(None),
        room_id: str = Query(None),
        sub: str = Query(None),
        ids: str = Query(None),
        proxy: bool = Query(False),
        cid: int = Query(0),
    ):
        cfg = load_config()
        base_url = str(request.base_url).rstrip("/")

        # ─── 视频信息 JSON ──────────────────────
        if info:
            vi = await video_info(info)
            if vi:
                return JSONResponse(vi)
            return JSONResponse({"error": "获取失败"}, status_code=404)

        # ─── 直播流 JSON ────────────────────────
        if live_url:
            import httpx
            from urllib.parse import urlencode
            try:
                h = {
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://live.bilibili.com/",
                }
                async with httpx.AsyncClient(timeout=15) as cli:
                    r = await cli.get(
                        f"https://api.live.bilibili.com/room/v1/Room/playUrl",
                        params={"cid": live_url, "platform": "web", "qn": 10000},
                        headers=h,
                    )
                    data = r.json()
                    stream_url = data.get("data", {}).get("durl", [{}])[0].get("url", "")
                    if stream_url:
                        stream_url = stream_url.replace("&p=1", "")
                        return JSONResponse({"url": stream_url})
            except Exception:
                pass
            return JSONResponse({"error": "直播流获取失败"}, status_code=502)

        # ─── 直播流代理 ─────────────────────────
        if live_proxy:
            live_ref = f"https://live.bilibili.com/{room_id or ''}"
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Referer": live_ref,
                "Origin": "https://live.bilibili.com",
            }
            import httpx
            async def stream_proxy():
                async with httpx.AsyncClient(timeout=30) as cli:
                    async with cli.stream("GET", live_proxy, headers=headers) as resp:
                        async for chunk in resp.aiter_bytes():
                            yield chunk
            return StreamingResponse(
                stream_proxy(),
                media_type="video/x-flv",
                headers={
                    "X-Accel-Buffering": "no",
                    "Cache-Control": "no-cache",
                    "Access-Control-Allow-Origin": "*",
                },
            )

        # ─── 直播流代取（M3U8）──────────────────
        if live_m3u_room:
            rid = live_m3u_room
            import httpx
            try:
                h = {
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://live.bilibili.com/",
                    "Origin": "https://live.bilibili.com",
                }
                async with httpx.AsyncClient(timeout=15) as cli:
                    r = await cli.get(
                        f"https://api.live.bilibili.com/room/v1/Room/playUrl",
                        params={"cid": rid, "platform": "web", "qn": 10000},
                        headers=h,
                    )
                    data = r.json()
                    stream_url = data.get("data", {}).get("durl", [{}])[0].get("url", "")
                    if stream_url:
                        stream_url = stream_url.replace("&p=1", "")
                        async def proxy_stream():
                            async with httpx.AsyncClient(timeout=30) as cli2:
                                async with cli2.stream("GET", stream_url, headers=h) as resp2:
                                    async for chunk in resp2.aiter_bytes():
                                        yield chunk
                        return StreamingResponse(
                            proxy_stream(),
                            media_type="application/vnd.apple.mpegurl",
                            headers={"Access-Control-Allow-Origin": "*"},
                        )
            except Exception:
                pass
            return PlainTextResponse("直播流获取失败", status_code=502)

        # ─── 直播 ───────────────────────────────
        if room:
            return await _handle_live(request, room)

        # ─── 综合 M3U ───────────────────────────
        if m3u is not None and not series and not up:
            return await build_bili_m3u(request, cfg, sub=sub, ids=ids)

        # ─── UP主 M3U ───────────────────────────
        if up and m3u is not None:
            return await _handle_up_m3u(request, up)

        # ─── UP主 ───────────────────────────────
        if up:
            return await _handle_up(request, up)

        # ─── 合集 M3U ───────────────────────────
        if series and m3u is not None:
            return await _handle_series_m3u(request, series, mid)

        # ─── 单视频代理 ─────────────────────────
        if id and proxy:
            # 如果M3U已经提供了cid，直接跳过video_info
            found_cid = cid or 0
            if not found_cid:
                vi = await video_info(id)
                if not vi:
                    return PlainTextResponse("获取视频信息失败", status_code=404)
                pages = vi.get("pages", [])
                for pg in pages:
                    if pg.get("page", 0) == p:
                        found_cid = pg.get("cid", 0)
                        break
                if not found_cid and pages:
                    found_cid = pages[0].get("cid", 0)
                if not found_cid:
                    return PlainTextResponse("无法获取CID", status_code=404)
            url = await bili_get_play_url(id, found_cid)
            if not url:
                return PlainTextResponse("获取播放地址失败", status_code=404)
            return await _proxy_stream_bili(url, request)

        # ─── 单视频播放 ─────────────────────────
        if id:
            vi = await video_info(id)

        # ─── 单视频播放 ─────────────────────────
        if id:
            vi = await video_info(id)
            if not vi:
                return PlainTextResponse("视频信息获取失败")
            pages = vi.get("pages", [])
            # 渲染播放器页面
            episodes = [{"id": str(pg["page"]), "title": pg.get("part", f"第{pg['page']}集")} for pg in pages]
            # 如果传了 series/mid，表示来自合集上下文，可以传回合集信息
            series_ctx = {}
            if series and mid:
                series_ctx = {"series_id": series, "mid": mid}
            return HTMLResponse(_render("player.html", {
                "request": request,
                "platform": "bili",
                "content_id": id,
                "title": vi.get("title", "视频"),
                "pic": vi.get("pic", ""),
                "episodes": episodes,
                "series": series_ctx,
            }))

        # ─── 合集 ───────────────────────────────
        if series:
            return await _handle_series(request, series, mid)

        return PlainTextResponse(
            "AB Player - B站\n用法:\n  /bili?id=BVxxx\n  /bili?series=xxx&mid=xxx\n  /bili?up=xxx\n  /bili?room=xxx"
        )

    app.include_router(router)


# ─── 直播 ────────────────────────────────────────────────────────

async def _handle_live(request: Request, room_id: str):
    info = await live_info(int(room_id))
    if not info:
        return PlainTextResponse("直播间不存在")
    base_url = str(request.base_url).rstrip("/")
    # 获取直播流 URL
    stream_url = await live_playurl(int(room_id))
    proxy_url = ""
    if stream_url:
        proxy_url = f"{base_url}/bili?live_proxy={urllib.parse.quote(stream_url)}&room_id={room_id}"
    return HTMLResponse(_render("player.html", {
        "request": request,
        "platform": "bili_live",
        "title": info.get("title", "直播"),
        "room_id": room_id,
        "live_status": info.get("live_status", 0),
        "live_stream_url": proxy_url,
        "content_id": room_id,
        "episodes": [],
        "live_uname": info.get("uname", ""),
        "live_online": info.get("online", 0),
        "live_attention": info.get("attention", 0),
        "live_cover": info.get("user_cover", "") or info.get("keyframe", ""),
        "live_area": info.get("area", ""),
        "live_description": info.get("description", ""),
        "is_live": info.get("live_status", 0) == 1,
    }))


# ─── UP主 ────────────────────────────────────────────────────────

async def _handle_up(request: Request, up_id: str):
    videos_data = await up_videos(int(up_id), pn=1, ps=50)
    if not videos_data:
        return PlainTextResponse("UP主视频获取失败")
    up_name = videos_data.get("name", f"UP主 #{up_id}")
    videos = videos_data.get("videos", [])
    return HTMLResponse(_render("series.html", {
        "request": request,
        "platform": "bili",
        "title": f"{up_name} 的视频",
        "tag": "B站UP主",
        "videos": videos,
        "source_id": up_id,

    }))


async def _handle_up_m3u(request: Request, up_id: str):
    base_url = str(request.base_url).rstrip("/")
    videos_data = await up_videos(int(up_id), pn=1, ps=50)
    if not videos_data:
        return PlainTextResponse("UP主视频获取失败")
    up_name = videos_data.get("name", f"UP主 #{up_id}")
    videos = videos_data.get("videos", [])
    from .m3u import bili_expand_videos_for_m3u
    expanded = bili_expand_videos_for_m3u(videos)

    lines = ["#EXTM3U", f"#PLAYLIST:{up_name} 的视频\n"]
    for v in expanded:
        bvid = v.get("bvid", "")
        if not bvid:
            continue
        vtitle = v.get("title", "视频")
        dur = int(v.get("duration", 0))
        lines.append(f"#EXTINF:{dur} group-title=\"B站UP主\",{vtitle}")
        lines.append(f"{base_url}/bili?id={bvid}")
    return Response("\n".join(lines), media_type="application/x-mpegURL; charset=utf-8")


# ─── 合集 ────────────────────────────────────────────────────────

async def _handle_series(request: Request, series_id: str, mid: str):
    from .config import load_config as _lc
    _cfg_local = _lc()
    s_fmt = "channel"
    s_name = ""
    for s in _cfg_local.get("bili_series", []):
        if str(s.get("series_id", "")) == str(series_id):
            s_fmt = s.get("fmt", "channel")
            s_name = s.get("name", "")
            break
    videos_data = await series_videos(int(series_id), mid=mid, pn=1, ps=50, fmt=s_fmt)
    if not videos_data:
        return PlainTextResponse("合集获取失败")
    # 优先用 config 里的名字，其次 API 返回的名字，最后 fallback
    api_name = videos_data.get("title") or ""
    title = s_name or api_name or f"合集 #{series_id}"
    videos = videos_data.get("videos", [])
    return HTMLResponse(_render("series.html", {
        "request": request,
        "platform": "bili",
        "title": title,
        "tag": "B站合集",
        "videos": videos,
        "source_id": series_id,
        "series_mid": mid,
    }))


async def _handle_series_m3u(request: Request, series_id: str, mid: str):
    from .config import load_config as _lc
    _cfg_local = _lc()
    s_fmt = "channel"
    for s in _cfg_local.get("bili_series", []):
        if str(s.get("series_id", "")) == str(series_id):
            s_fmt = s.get("fmt", "channel")
            break
    base_url = str(request.base_url).rstrip("/")
    videos_data = await series_videos(int(series_id), mid=mid, pn=1, ps=50, fmt=s_fmt)
    if not videos_data:
        return PlainTextResponse("合集获取失败")
    title = videos_data.get("title", f"合集 #{series_id}")
    videos = videos_data.get("videos", [])
    from .m3u import bili_expand_videos_for_m3u
    expanded = bili_expand_videos_for_m3u(videos)

    lines = ["#EXTM3U", f"#PLAYLIST:{title}\n"]
    for v in expanded:
        bvid = v.get("bvid", "")
        if not bvid:
            continue
        vtitle = v.get("title", "视频")
        dur = int(v.get("duration", 0))
        lines.append(f"#EXTINF:{dur} group-title=\"{title}\",{vtitle}")
        lines.append(f"{base_url}/bili?id={bvid}")
    return Response("\n".join(lines), media_type="application/x-mpegURL; charset=utf-8")

# ─── B站流式代理 ──────────────────────────────────────────────────

import httpx as _httpx

async def _proxy_stream_bili(url: str, request: Request):
    """流式代理 B站 CDN 视频。使用独立长超时客户端。"""
    _STREAM_CLIENT = _httpx.AsyncClient(
        timeout=_httpx.Timeout(None, connect=10.0),
        follow_redirects=True,
    )
    get_headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/",
    }
    range_header = request.headers.get("range", "")
    if range_header:
        get_headers["Range"] = range_header

    try:
        # 用 send(stream=True) 避免 async with 提前关闭响应
        req = _STREAM_CLIENT.build_request("GET", url, headers=get_headers)
        resp = await _STREAM_CLIENT.send(req, stream=True)

        if resp.status_code >= 400:
            await resp.aclose()
            return PlainTextResponse(f"上游返回 {resp.status_code}", status_code=502)

        fwd_headers = {
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=86400",
        }
        for h in ["content-type", "content-length", "content-range", "accept-ranges"]:
            if h in resp.headers:
                fwd_headers[h] = resp.headers[h]
        if "content-type" not in fwd_headers:
            fwd_headers["content-type"] = "video/mp4"

        async def _stream():
            try:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk
            finally:
                await resp.aclose()
                await _STREAM_CLIENT.aclose()

        return StreamingResponse(_stream(), status_code=resp.status_code, headers=fwd_headers)

    except Exception as e:
        return PlainTextResponse(f"代理错误: {e}", status_code=502)
