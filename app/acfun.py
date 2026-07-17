"""AcFun API - A站数据抓取与播放"""

import json, re, time
from typing import Optional
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from .config import cache_get, cache_set, cache_delete, http_get, http_get_json, BASE_DIR, load_config

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _headers():
    return {
        "User-Agent": _UA,
        "Referer": "https://www.acfun.cn/",
        "Origin": "https://www.acfun.cn",
    }


# ═══════════════════════════════════════════════════════════════
# 合辑
# ═══════════════════════════════════════════════════════════════

async def fetch_album_info(album_id: str) -> Optional[dict]:
    """获取A站合辑基本信息"""
    clean = re.sub(r"^aa", "", album_id)
    key = f"aai:{album_id}"
    if (cached := cache_get(key, 300)):
        return json.loads(cached)

    data = await http_get_json(
        f"https://www.acfun.cn/rest/pc-direct/arubamu/getById?arubamuId={clean}",
        _headers(),
    )
    if not data or data.get("result", -1) != 0:
        return None
    info = data.get("data", {})
    result = {
        "id": album_id,
        "title": info.get("title", ""),
        "authorId": info.get("authorId", ""),
        "authorName": info.get("authorName", ""),
        "intro": info.get("intro", ""),
        "cover": info.get("coverImage", ""),
        "totalSize": info.get("itemCount", 0),
    }
    cache_set(key, json.dumps(result, ensure_ascii=False), 300)
    return result


async def fetch_album_videos(album_id: str, force: bool = False) -> Optional[list]:
    """获取A站合辑所有视频"""
    clean = re.sub(r"^aa", "", album_id)
    key = f"aav:{album_id}"
    if force:
        cache_delete(key)
    if not force:
        if (cached := cache_get(key, 300)):
            return json.loads(cached)

    headers = _headers()
    all_videos = []
    for page in range(1, 11):
        data = await http_get_json(
            f"https://www.acfun.cn/rest/pc-direct/arubamu/content/list?"
            f"arubamuId={clean}&page={page}&size=100",
            headers,
        )
        if not data or data.get("result", -1) != 0:
            break
        items = data.get("contents", [])
        if not items:
            break
        for v in items:
            rid = v.get("resourceId") or v.get("id") or ""
            duration_ms = v.get("duration", 0)
            vlist = v.get("videoList", [])
            duration = 0
            if duration_ms > 0:
                duration = int(duration_ms / 1000)
            elif vlist:
                for sv in vlist:
                    duration += int((sv.get("durationMillis", 0) or sv.get("duration", 0)) / 1000)
            all_videos.append({
                "id": str(rid),
                "title": v.get("title", ""),
                "cover": v.get("coverImage") or v.get("coverUrl", ""),
                "duration": duration,
                "createTime": v.get("createTime", 0),
                "ownerName": v.get("user", {}).get("name", "") or v.get("authorName", ""),
                "videoList": vlist,
        
    })
        if page >= (data.get("pageCount", 1)):
            break

    if not all_videos:
        return None
    cache_set(key, json.dumps(all_videos, ensure_ascii=False), 300)
    return all_videos


# ═══════════════════════════════════════════════════════════════
# 用户视频
# ═══════════════════════════════════════════════════════════════

async def fetch_user_info(user_id: str) -> Optional[dict]:
    """获取A站UP主基本信息（通过搜索API）"""
    key = f"aui:{user_id}"
    if cached := cache_get(key, ttl=600):
        return json.loads(cached)
    # 使用搜索API获取用户信息
    headers = _headers()
    data = await http_get_json(
        f"https://www.acfun.cn/rest/pc-direct/search/user?keyword={user_id}&pageNo=1&pageSize=1",
        headers,
    )
    if data and data.get("result") == 0:
        userList = data.get("userList", [])
        for u in userList:
            if str(u.get("userId", "")) == str(user_id):
                name = u.get("userName", "") or u.get("emTitle", "")
                if name:
                    # Strip HTML tags from emTitle
                    import re as _re
                    name = _re.sub(r"<[^>]+>", "", name)
                    result = {"id": user_id, "name": name}
                    cache_set(key, json.dumps(result, ensure_ascii=False), 600)
                    return result
                break
    # 回退：通过用户第一个视频获取名称
    videos = await fetch_user_videos(user_id)
    if videos:
        result = {"id": user_id, "name": videos[0].get("ownerName", "") or f"UP主 #{user_id}"}
        cache_set(key, json.dumps(result, ensure_ascii=False), 600)
        return result
    return None


async def fetch_user_videos(user_id: str, force: bool = False) -> Optional[list]:
    """获取A站UP主的视频列表"""
    key = f"auv:{user_id}"
    if force:
        cache_delete(key)
    if not force:
        if (cached := cache_get(key, 300)):
            return json.loads(cached)

    headers = _headers()
    all_videos = []

    # 尝试1：旧REST API
    for pn in range(1, 11):
        data = await http_get_json(
            f"https://www.acfun.cn/rest/pc-direct/user/video?"
            f"userId={user_id}&pageNum={pn}&pageSize=100",
            headers,
        )
        if not data:
            break
        if data.get("data"):
            for v in data["data"]:
                all_videos.append({
                    "id": v.get("id", ""),
                    "title": v.get("title", ""),
                    "cover": v.get("coverUrl", ""),
                    "duration": v.get("duration", 0),
                    "createTime": v.get("createTime", 0),
                })
            if len(data["data"]) < 100:
                break
        else:
            break

    # 尝试2：A站 ajaxpipe 接口（空间页视频列表）
    if not all_videos:
        import httpx, re as _re
        headers["Accept"] = "application/json, text/plain, */*"
        headers["X-Requested-With"] = "XMLHttpRequest"
        # 先访问用户页面获取cookie
        async with httpx.AsyncClient(timeout=10) as cli:
            await cli.get(f"https://www.acfun.cn/u/{user_id}", headers=headers)
            for page in range(1, 101):
                url = (f"https://www.acfun.cn/u/{user_id}?quickViewId=ac-space-video-list"
                       f"&reqID={page}&ajaxpipe=1&type=video&order=newest&page={page}&pageSize=50")
                try:
                    resp = await cli.get(url, headers=headers, timeout=10)
                    text = resp.text
                    m = _re.search(r'^(\{.*\})', text, _re.S)
                    if not m:
                        break
                    d = json.loads(m.group(1))
                    html = d.get("html", "")
                    # 提取视频信息
                    items = _re.findall(
                        r'href="/v/ac(\d+)"[^>]*>.*?'
                        r'<img[^>]*src="([^"]+)"[^>]*/>.*?'
                        r'<p class="title[^"]*" title="([^"]+)"[^>]*>.*?'
                        r'<p class="date">([^<]+)',
                        html, _re.S
                    )
                    for item in items:
                        cid, cover_url, title, date_str = item
                    for item in items:
                        cid, cover_url, title, date_str = item
                        # 已有的不重复添加
                        if any(v.get("id") == cid for v in all_videos):
                            continue
                        # 解析日期 "2026/07/11" -> 时间戳（秒）
                        import calendar as _cal
                        try:
                            parts = date_str.split("/")
                            dt = __import__("datetime", fromlist=["datetime"]).datetime(int(parts[0]), int(parts[1]), int(parts[2]))
                            ts = int(_cal.timegm(dt.timetuple()))
                        except Exception:
                            ts = 0
                        all_videos.append({
                            "id": cid,
                            "title": title,
                            "cover": cover_url,
                            "duration": 0,
                            "createTime": ts,
                        })
                    if len(items) < 50:
                        break
                except Exception:
                    break

    # 尝试3：搜索API兜底（最新3个视频）
    if not all_videos:
        data = await http_get_json(
            f"https://www.acfun.cn/rest/pc-direct/search/user?keyword={user_id}&pageNo=1&pageSize=1",
            _headers(),
        )
        if data and data.get("result") == 0:
            userList = data.get("userList", [])
            for u in userList:
                if str(u.get("userId", "")) == str(user_id):
                    for v in u.get("dougaFeedList", []):
                        all_videos.append({
                            "id": str(v.get("contentId", "")),
                            "title": v.get("caption", ""),
                            "cover": (v.get("coverUrls", [""])[0] or ""),
                            "duration": _parse_duration(v.get("playDuration", "0:00")),
                            "createTime": 0,
                            "ownerName": u.get("userName", ""),
                        })
                    break

    if not all_videos:
        return None
    cache_set(key, json.dumps(all_videos, ensure_ascii=False), 300)
    return all_videos


def _parse_duration(dur_str: str) -> int:
    """将 "01:23:45" 或 "12:34" 转为秒数"""
    parts = dur_str.split(":")
    if len(parts) == 3:
        return int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
    elif len(parts) == 2:
        return int(parts[0])*60 + int(parts[1])
    return 0


# ═══════════════════════════════════════════════════════════════
# 单视频
# ═══════════════════════════════════════════════════════════════

async def fetch_video_info(content_id: str, force: bool = False) -> Optional[dict]:
    """获取A站单视频信息"""
    key = f"avi:{content_id}"
    if force:
        cache_delete(key)
    if not force:
        if (cached := cache_get(key, 300)):
            return json.loads(cached)

    headers = _headers()
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    html = await http_get(f"https://www.acfun.cn/v/ac{content_id}", headers)
    if not html:
        return None

    # 提取 videoInfo JS 变量
    m = re.search(r"videoInfo\s*=\s*({.+?});", html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None

    cvi = data.get("currentVideoInfo", {})
    user = data.get("user", {})
    result = {
        "id": str(data.get("currentVideoId", content_id)),
        "title": data.get("title") or cvi.get("title", ""),
        "cover": data.get("coverUrl") or (data.get("coverCdnUrls", [None])[0] or ""),
        "duration": int((data.get("durationMillis", 0) or 0) / 1000),
        "ownerName": user.get("name", ""),
        "videoList": data.get("videoList", []),
    }
    cache_set(key, json.dumps(result, ensure_ascii=False), 300)
    return result


async def get_play_url(content_id: str, target_cid: str = "") -> Optional[str]:
    """获取A站视频播放地址（返回M3U8 URL或M3U8内容）"""
    key = f"apu:{content_id}:{target_cid}"
    if (cached := cache_get(key, 300)):
        return json.loads(cached)

    headers = _headers()
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

    if target_cid:
        # 直接调用A站 REST API
        api_data = await http_get_json(
            f"https://www.acfun.cn/rest/pc-direct/play/playInfo/ksPlayJson?videoId={target_cid}",
            headers,
        )
        if api_data and api_data.get("result", -1) == 0:
            pi = api_data.get("playInfo", {})
            ksp = pi.get("ksPlayJson", "")
            if ksp:
                try:
                    play_data = json.loads(ksp) if isinstance(ksp, str) else ksp
                except json.JSONDecodeError:
                    play_data = None
                if play_data:
                    url = await _extract_best_m3u8(play_data, headers)
                    if url:
                        if url.startswith("http"):
                            cache_set(key, json.dumps(url, ensure_ascii=False), 300)
                            return url
                        # M3U8内容
                        cache_set(key, json.dumps(url, ensure_ascii=False), 300)
                        return url

    # 从页面提取
    html = await http_get(f"https://www.acfun.cn/v/ac{content_id}", headers)
    if not html:
        return None

    m = re.search(r"videoInfo\s*=\s*({.+?});", html)
    if not m:
        return None
    try:
        vi = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None

    lookup_id = target_cid or content_id
    vlist = vi.get("videoList", [])
    cvi = None
    for v in vlist:
        if str(v.get("id", "")) == str(lookup_id):
            cvi = v.get("currentVideoInfo")
            break
    if not cvi:
        cvi = vi.get("currentVideoInfo")
    if not cvi:
        return None

    ksp = cvi.get("ksPlayJson", "")
    if not ksp:
        return None
    try:
        play_data = json.loads(ksp) if isinstance(ksp, str) else ksp
    except json.JSONDecodeError:
        return None
    if not play_data:
        return None

    url = await _extract_best_m3u8(play_data, headers)
    if url:
        cache_set(key, json.dumps(url, ensure_ascii=False), 300)
    return url


async def get_play_url_proxy(content_id: str, base_url: str, target_cid: str = "") -> Optional[str]:
    """获取A站视频播放地址，返回带代理前缀的 M3U8"""
    key = f"appu:{content_id}:{target_cid}"
    if (cached := cache_get(key, 86400)):
        return json.loads(cached)

    headers = _headers()
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

    if target_cid:
        api_data = await http_get_json(
            f"https://www.acfun.cn/rest/pc-direct/play/playInfo/ksPlayJson?videoId={target_cid}",
            headers,
        )
        if api_data and api_data.get("result", -1) == 0:
            pi = api_data.get("playInfo", {})
            ksp = pi.get("ksPlayJson", "")
            if ksp:
                try:
                    play_data = json.loads(ksp) if isinstance(ksp, str) else ksp
                except json.JSONDecodeError:
                    play_data = None
                if play_data:
                    url = await _extract_best_m3u8(play_data, headers)
                    if url:
                        cache_set(key, json.dumps(url, ensure_ascii=False), 86400)
                        return url

    # 从页面提取
    html = await http_get(f"https://www.acfun.cn/v/ac{content_id}", headers)
    if not html:
        return None

    m = re.search(r"videoInfo\s*=\s*({.+?});", html)
    if not m:
        return None
    try:
        vi = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None

    vlist = vi.get("videoList", [])

    # 获取目标 CID（单视频用第一个 CID），走 REST API 拿到 tx-video CDN
    if target_cid:
        lookup_cid = target_cid
    else:
        lookup_cid = str(vlist[0]["id"]) if vlist and vlist[0].get("id") else content_id

    api_data = await http_get_json(
        f"https://www.acfun.cn/rest/pc-direct/play/playInfo/ksPlayJson?videoId={lookup_cid}",
        headers,
    )
    if api_data and api_data.get("result", -1) == 0:
        pi = api_data.get("playInfo", {})
        ksp = pi.get("ksPlayJson", "")
        if ksp:
            try:
                play_data = json.loads(ksp) if isinstance(ksp, str) else ksp
            except json.JSONDecodeError:
                play_data = None
            if play_data:
                url = await _extract_best_m3u8(play_data, headers)
                if url:
                    cache_set(key, json.dumps(url, ensure_ascii=False), 86400)
                    return url

    return None


async def _extract_best_m3u8(play_data: dict, headers: dict) -> Optional[str]:
    """从 ksPlayJson 数据中提取最高码率的M3U8，返回原始 CDN URL 的 M3U8"""
    ads = play_data.get("adaptationSet", [])
    best_url = ""
    best_pixels = 0
    for ad in ads:
        reps = ad.get("representation", [])
        for r in reps:
            rurl = r.get("url", "")
            width = int(r.get("width", 0) or 0)
            height = int(r.get("height", 0) or 0)
            pixels = width * height
            if rurl and pixels > best_pixels:
                best_pixels = pixels
                best_url = rurl

    if not best_url:
        return None

    # 检查是否是 m3u8 内容还是 URL
    if best_url.startswith("http"):
        # 尝试抓取 M3U8 并改写分片地址
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15) as cli:
                r = await cli.get(best_url, headers=headers)
                if r.status_code == 200 and r.text.strip().startswith("#EXTM3U"):
                    from urllib.parse import quote, urlparse
                    cdn_base = best_url.rsplit("/", 1)[0] + "/"
                    # 保留 best_url 上的认证参数（pkey, safety_id 等）
                    parsed = urlparse(best_url)
                    auth_query = parsed.query
                    lines = r.text.split("\n")
                    rewritten = []
                    for line in lines:
                        tl = line.strip()
                        if tl and not tl.startswith("#") and (".ts" in tl.lower() or ".m3u8" in tl.lower()):
                            if tl.startswith("http"):
                                # 已经是完整 URL（通常已有 auth 参数）
                                full_ts_url = tl
                            else:
                                # 只有文件名，需要拼 CDN base + auth
                                full_ts_url = cdn_base + tl
                                if auth_query:
                                    sep = "&" if "?" in full_ts_url else "?"
                                    full_ts_url += sep + auth_query
                            rewritten.append(full_ts_url)
                        else:
                            rewritten.append(line)
                    return "\n".join(rewritten)
        except Exception:
            pass
        return best_url
    return best_url


def _proxy_prefix_m3u8(m3u8_content: str, base_url: str) -> str:
    """将 M3U8 中的 CDN URL 替换为代理 URL，中间人替换"""
    from urllib.parse import quote
    lines = m3u8_content.split("\n")
    rewritten = []
    for line in lines:
        tl = line.strip()
        if tl and not tl.startswith("#") and (tl.startswith("http://") or tl.startswith("https://")):
            rewritten.append(f"{base_url}/acfun/tss?url={quote(tl)}")
        else:
            rewritten.append(line)
    return "\n".join(rewritten)


async def _proxy_acfun_m3u8(url: str, request: Request) -> Response:
    """代理 A站 M3U8/HLS 流，确保携带正确 Referer 和 Cookie"""
    import httpx, urllib.parse
    from .config import load_config
    headers = _headers()
    _cfg = load_config()
    ac_cookie = _cfg.get("acfun_cookie", "")
    if ac_cookie:
        headers["Cookie"] = urllib.parse.unquote(ac_cookie).strip().rstrip(";").strip()
    range_header = request.headers.get("range", "")
    if range_header:
        headers["Range"] = range_header

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as head_cli:
            head_resp = await head_cli.head(url, headers=headers)
        if head_resp.status_code >= 400:
            return PlainTextResponse(f"CDN 返回 {head_resp.status_code}", status_code=502)

        resp_headers = {"Access-Control-Allow-Origin": "*"}
        for hk in ("content-type", "content-range", "accept-ranges"):
            if hk in head_resp.headers:
                resp_headers[hk] = head_resp.headers[hk]
        status = head_resp.status_code

        async def _stream():
            async with httpx.AsyncClient(timeout=300, follow_redirects=True) as cli:
                async with cli.stream("GET", url, headers=headers) as resp:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        yield chunk

        return StreamingResponse(
            _stream(),
            status_code=status,
            headers=resp_headers,
        )
    except Exception as e:
        return PlainTextResponse(f"代理失败: {e}", status_code=502)


async def _proxy_acfun_ts(ts_url: str, request: Request) -> Response:
    """代理 A站 TS 分片，携带正确 Referer 和 Cookie"""
    import httpx, urllib.parse
    from .config import load_config
    headers = _headers()
    _cfg = load_config()
    ac_cookie = _cfg.get("acfun_cookie", "")
    if ac_cookie:
        headers["Cookie"] = urllib.parse.unquote(ac_cookie).strip().rstrip(";").strip()
    range_header = request.headers.get("range", "")
    if range_header:
        headers["Range"] = range_header

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as head_cli:
            head_resp = await head_cli.head(ts_url, headers=headers)
        if head_resp.status_code >= 400:
            return PlainTextResponse(f"TS CDN 返回 {head_resp.status_code}", status_code=502)

        resp_headers = {"Access-Control-Allow-Origin": "*"}
        for hk in ("content-type", "content-range", "accept-ranges", "content-length"):
            if hk in head_resp.headers:
                resp_headers[hk] = head_resp.headers[hk]
        status = head_resp.status_code

        async def _stream():
            async with httpx.AsyncClient(timeout=300, follow_redirects=True) as cli:
                async with cli.stream("GET", ts_url, headers=headers) as resp:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        yield chunk

        return StreamingResponse(
            _stream(),
            status_code=status,
            headers=resp_headers,
        )
    except Exception as e:
        return PlainTextResponse(f"TS 代理失败: {e}", status_code=502)


# ═══════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════

def expand_videos_for_m3u(videos: list) -> list:
    """展开A站多P视频为独立条目"""
def expand_videos_for_m3u(videos: list) -> list:
    """展开A站多P视频为独立条目"""
    expanded = []
    for v in videos:
        parent_id = v.get("id", "")
        vlist = v.get("videoList", [])
        if len(vlist) > 1:
            for sv in vlist:
                sv_id = sv.get("id", "")
                sv_title = sv.get("title", "")
                if sv_id:
                    entry = dict(v)
                    entry["cid"] = str(sv_id)
                    entry["title"] = (v.get("title", "")) + " - " + sv_title
                    entry.pop("videoList", None)
                    expanded.append(entry)
        else:
            v_copy = dict(v)
            v_copy.pop("videoList", None)
            expanded.append(v_copy)
    return expanded


# ═══════════════════════════════════════════════════════════════
# FastAPI 路由注册
# ═══════════════════════════════════════════════════════════════

from jinja2 import Environment, FileSystemLoader
import time
_j2_env = Environment(loader=FileSystemLoader(str(BASE_DIR / "app" / "templates")))
_j2_env.filters["timestamp_to_date"] = lambda ts: time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""
def _render(name, ctx=None, **kw):
    if ctx:
        kw.update(ctx)
    return _j2_env.get_template(name).render(**kw)


def register(app):
    router = APIRouter()

    @router.get("/acfun")
    async def acfun_route(
        request: Request,
        id: str = Query(None),
        album: str = Query(None),
        up: str = Query(None),
        play: bool = Query(False),
        cid: str = Query(None),
        p: int = Query(1),
        m3u: bool = Query(False),
        sub: str = Query(None),
        _parts: str = Query(None),
    ):
        cfg = load_config()

        # ─── M3U ────────────────────────────────
        if m3u:
            return await _handle_acfun_m3u(request, cfg, id, album, up, sub)

        # ─── UP主 ────────────────────────────────
        if up:
            return await _handle_acfun_up(request, up, m3u)

        # ─── 合辑 ────────────────────────────────
        if album:
            return await _handle_acfun_album(request, album, m3u)

        # ─── 播放/单视频 ─────────────────────────
        if id:
            if play:
                base_url = str(request.base_url).rstrip("/")
                url = await get_play_url_proxy(id, base_url, cid or "")
                if url:
                    if url.startswith("#EXTM3U"):
                        # 原始 M3U8（CDN URL），动态替换为代理 URL
                        url = _proxy_prefix_m3u8(url, base_url)
                        return Response(content=url, media_type="application/vnd.apple.mpegurl")
                    if url.startswith("http"):
                        # 是 URL 不是 M3U8 内容，可能被浏览器直接跳转
                        # 用代理方式播放，确保 Referer 正确
                        return await _proxy_acfun_m3u8(url, request)
                return PlainTextResponse("播放地址获取失败", status_code=502)
            info = await fetch_video_info(id)
            if info:
                return _render_acfun_player(request, id, info, _parts)
            return PlainTextResponse("视频信息获取失败")

        return PlainTextResponse(
            "AB Player - A站\n用法:\n  /acfun?id=xxx\n  /acfun?album=xxx\n  /acfun?up=xxx"
        )

    @router.get("/acfun/tss")
    @router.head("/acfun/tss", include_in_schema=False)
    async def acfun_tss_route(request: Request, url: str = Query(...)):
        """代理 A站 TS 分片，携带正确 Referer"""
        return await _proxy_acfun_ts(url, request)

    app.include_router(router)


async def _handle_acfun_m3u(request, cfg, id, album, up, sub):
    """处理A站M3U输出"""
    base_url = str(request.base_url).rstrip("/")
    allowed = {}

    if sub:
        for s in cfg.get("subscriptions", []):
            if s.get("name") == sub:
                for item in s.get("items", []):
                    allowed[item] = True
                break

    # 过滤
    def filter_albums(albs):
        if not allowed:
            return albs
        return [a for a in albs if f"acfun_album_{a['id']}" in allowed]

    def filter_ups(ups):
        if not allowed:
            return ups
        return [u for u in ups if f"acfun_up_{u['mid']}" in allowed]

    def filter_videos(vids):
        if not allowed:
            return vids
        return [v for v in vids if f"acfun_video_{v['id']}" in allowed]

    lines = ["#EXTM3U", "#PLAYLIST:AB Player A站 订阅\n"]

    # 合辑
    for a in filter_albums(cfg.get("acfun_albums", [])):
        aid = a.get("id", "")
        name = a.get("name", "A站合辑")
        if not aid:
            continue
        videos = await fetch_album_videos(aid)
        if not videos:
            continue
        expanded = expand_videos_for_m3u(videos)
        for v in expanded:
            vid = v.get("id", "")
            cid = v.get("cid", "")
            vtitle = v.get("title", "未知")
            dur = int(v.get("duration", 0))
            lines.append(f"#EXTINF:{dur} group-title=\"{name}\",{_fmt_m3u_title(vtitle)}")
            cid_qs = f"&cid={cid}" if cid else ""
            lines.append(f"{base_url}/acfun?id={vid}&play=1{cid_qs}")

    # UP主
    for u in filter_ups(cfg.get("acfun_ups", [])):
        mid = u.get("mid", "")
        name = u.get("name", "A站UP主")
        if not mid:
            continue
        videos = await fetch_user_videos(mid)
        if not videos:
            continue
        expanded = expand_videos_for_m3u(videos)
        for v in expanded:
            vid = v.get("id", "")
            cid = v.get("cid", "")
            vtitle = v.get("title", "未知")
            dur = int(v.get("duration", 0))
            lines.append(f"#EXTINF:{dur} group-title=\"{name}\",{_fmt_m3u_title(vtitle)}")
            cid_qs = f"&cid={cid}" if cid else ""
            lines.append(f"{base_url}/acfun?id={vid}&play=1{cid_qs}")

    # 单视频
    for v in filter_videos(cfg.get("acfun_videos", [])):
        vid = v.get("id", "")
        name = v.get("name", "A站视频")
        if vid:
            lines.append(f"#EXTINF:-1 group-title=\"A站单视频\",{_fmt_m3u_title(name)}")
            lines.append(f"{base_url}/acfun?id={vid}&play=1")

    return Response("\n".join(lines), media_type="application/x-mpegURL; charset=utf-8")


async def _handle_acfun_up(request, up_id, m3u):
    """处理A站UP主"""
    videos = await fetch_user_videos(up_id)
    if not videos:
        return PlainTextResponse("UP主视频获取失败")
    # 获取UP主名称
    info = await fetch_user_info(up_id)
    up_name = info.get("name", f"UP主 #{up_id}") if info else f"UP主 #{up_id}"
    return _render_series_list(request, "acfun", videos, f"{up_name} 的视频", "A站UP主", up_id)


async def _handle_acfun_album(request, album_id, m3u):
    """处理A站合辑"""
    videos = await fetch_album_videos(album_id)
    if not videos:
        return PlainTextResponse("合辑获取失败")
    # 获取合辑真实名称
    info = await fetch_album_info(album_id)
    title = info.get("title", f"A站合辑 #{album_id}") if info else f"A站合辑 #{album_id}"
    return _render_series_list(request, "acfun", videos, title, "A站合辑", album_id)


def _render_acfun_player(request, content_id, info, parts_encoded):
    """渲染A站播放器页"""
    title = info.get("title", "视频")
    pic = info.get("cover", "")
    owner = info.get("ownerName", "")
    dur = int(info.get("duration", 0))
    dur_str = f"{dur//60:02d}:{dur%60:02d}" if dur > 0 else ""
    episodes = []

    if parts_encoded:
        for seg in parts_encoded.split(","):
            seg = seg.strip()
            if ":" in seg:
                pid, ptitle = seg.split(":", 1)
                if pid:
                    episodes.append({"id": pid, "title": ptitle})
    elif info.get("videoList"):
        for sv in info["videoList"]:
            sv_id = str(sv.get("id", ""))
            if sv_id:
                episodes.append({"id": sv_id, "title": sv.get("title", f"第{len(episodes)+1}集")})

    base_url = str(request.base_url).rstrip("/")
    return HTMLResponse(_render("player.html", {
        "request": request,
        "platform": "acfun",
        "title": title,
        "pic": pic,
        "owner": owner,
        "dur_str": dur_str,
        "content_id": content_id,
        "episodes": episodes,

    }))


def _render_series_list(request, platform, videos, title, tag, source_id):
    """渲染合集/UP主视频列表"""
    return HTMLResponse(_render("series.html", {
        "request": request,
        "platform": platform,
        "title": title,
        "tag": tag,
        "videos": videos,
        "source_id": source_id,

    }))


def _fmt_m3u_title(title: str) -> str:
    """格式化 M3U 标题"""
    title = re.sub(r"^【[^】]*】", "", title).strip()
    m = re.match(r"^(.*[^\s])\s*[（(]([^）)]+)[）)]\s*-\s*(.+)$", title)
    if m:
        return f"{m.group(1).strip()}-{m.group(3).strip()}（{m.group(2).strip()}）"
    m = re.match(r"^(.*)\s*-\s*(.+)$", title)
    if m:
        return f"{m.group(1).strip()}-{m.group(2).strip()}"
    return title
