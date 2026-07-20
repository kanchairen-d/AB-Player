"""Bilibili API (httpx 直调 + Wbi 签名)"""

import asyncio, hashlib, json, time, urllib.parse
from functools import reduce
from typing import Optional

from .config import cache_get, cache_set, cache_delete, http_get, http_get_json, load_config

_BASE = "https://api.bilibili.com"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

# Wbi shuffle 表
_OE = [
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,
    27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
    37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
    22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52,
]

# ═══════════════════════════════════════════════════════════════
# Wbi 签名
# ═══════════════════════════════════════════════════════════════

async def _get_mixin_key() -> str:
    """从 nav 接口获取 Wbi mixin_key（带缓存，24h）"""
    cached = cache_get("_wbi_mixin_key", ttl=86400)
    if cached:
        return cached

    data = await http_get_json(f"{_BASE}/x/web-interface/nav")
    if not data or not isinstance(data, dict):
        return ""
    # wbi_img 在未登录时也能获取到（code=-101），不影响签名
    inner = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
    wbi = inner.get("wbi_img", {})
    if not wbi:
        return ""

    def _split(key: str) -> str:
        return wbi.get(key, "").split("/")[-1].split(".")[0]

    ae = _split("img_url") + _split("sub_url")
    mixin = reduce(lambda s, i: s + (ae[i] if i < len(ae) else ""), _OE, "")[:32]
    cache_set("_wbi_mixin_key", mixin, 86400)
    return mixin

def _sign_wbi(params: dict, mixin: str) -> dict:
    p = {k: v for k, v in params.items() if k != "w_rid"}
    p["wts"] = int(time.time())
    sorted_qs = urllib.parse.urlencode(sorted(p.items()))
    p["w_rid"] = hashlib.md5((sorted_qs + mixin).encode()).hexdigest()
    return p

# ═══════════════════════════════════════════════════════════════
# 统一请求封装
# ═══════════════════════════════════════════════════════════════

async def _req(path: str, params: dict = None, *, wbi: bool = False) -> Optional[dict]:
    if params is None:
        params = {}
    if wbi:
        mixin = await _get_mixin_key()
        if mixin:
            params = _sign_wbi(params, mixin)

    if params:
        qs = urllib.parse.urlencode(params)
        url = f"{_BASE}{path}?{qs}"
    else:
        url = f"{_BASE}{path}"

    if "/live/" in path:
        ref = "https://live.bilibili.com"
    elif "/space/" in path:
        ref = "https://space.bilibili.com"
    else:
        ref = "https://www.bilibili.com"

    cfg = load_config()
    cookie = cfg.get("bili_cookie", cfg.get("cookie", ""))
    headers = {
        "User-Agent": _UA,
        "Referer": ref,
        "Origin": "https://www.bilibili.com",
        "Cookie": cookie,
    }
    data = await http_get_json(url, headers)
    if data is None:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("code") != 0:
        return None
    return data.get("data")


# ═══════════════════════════════════════════════════════════════
# 视频信息
# ═══════════════════════════════════════════════════════════════

async def video_info(bvid: str) -> Optional[dict]:
    key = f"vi:{bvid}"
    if cached := cache_get(key):
        return json.loads(cached)

    info = await _req("/x/web-interface/view", {"bvid": bvid})
    if not info:
        return None

    result = {
        "bvid": info.get("bvid", bvid),
        "aid": info.get("aid", 0),
        "title": info.get("title", ""),
        "desc": info.get("desc", ""),
        "pic": info.get("pic", ""),
        "duration": info.get("duration", 0),
        "cid": info.get("cid", 0),
        "owner": info.get("owner", {}),
        "stat": info.get("stat", {}),
        "pages": [{"page": p["page"], "part": p["part"], "cid": p["cid"]}
                  for p in (info.get("pages") or [])],
    }
    cache_set(key, json.dumps(result, ensure_ascii=False), 1800)
    return result

async def video_playurl(bvid: str, cid: int = 0, qn: int = 116, fnval: int = 0) -> Optional[dict]:
    key = f"pu:{bvid}:{cid}:{qn}:{fnval}"
    if cached := cache_get(key):
        return json.loads(cached)

    if not cid:
        info = await video_info(bvid)
        if not info:
            return None
        cid = int(info.get("cid", 0))

    params = {"bvid": bvid, "cid": cid, "qn": qn}
    if fnval:
        params["fnval"] = fnval
    play = await _req("/x/player/playurl", params)
    if not play:
        return None

    result = {"qn": qn}
    if play.get("dash"):
        result["dash"] = {
            "video": [{"id": t["id"], "url": t.get("baseUrl", "")}
                      for t in (play["dash"].get("video") or [])],
            "audio": [{"id": t["id"], "url": t.get("baseUrl", "")}
                      for t in (play["dash"].get("audio") or [])],
        }
    if play.get("durl"):
        result["flv"] = [{"url": d["url"], "size": d.get("size", 0)}
                         for d in play["durl"]]

    cache_set(key, json.dumps(result, ensure_ascii=False), 86400)
    return result


# ═══════════════════════════════════════════════════════════════
# 直播（使用 live 独立域名 API）
# ═══════════════════════════════════════════════════════════════

async def live_info(room_id: int) -> Optional[dict]:
    key = f"li:{room_id}"
    if cached := cache_get(key, ttl=60):
        return json.loads(cached)

    url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}"
    headers = {"User-Agent": _UA, "Referer": "https://live.bilibili.com", "Origin": "https://www.bilibili.com"}
    data = await http_get_json(url, headers)
    if data is None or not isinstance(data, dict) or data.get("code") != 0:
        return None

    r = data.get("data", {})
    result = {
        "room_id": r.get("room_id", room_id),
        "title": r.get("title", ""),
        "uname": r.get("uname", ""),
        "live_status": r.get("live_status", 0),
        "online": r.get("online", 0),
        "area": r.get("area_name", ""),
        "keyframe": r.get("keyframe", "") or r.get("user_cover", ""),
        "user_cover": r.get("user_cover", ""),
        "attention": r.get("attention", 0),
        "description": r.get("description", ""),
    }
    cache_set(key, json.dumps(result, ensure_ascii=False), 60)
    return result

async def live_playurl(room_id: int) -> Optional[str]:
    key = f"lp:{room_id}"
    if cached := cache_get(key, ttl=60):
        return json.loads(cached)

    url = f"https://api.live.bilibili.com/room/v1/Room/playUrl?cid={room_id}&qn=116&platform=web"
    headers = {"User-Agent": _UA, "Referer": "https://live.bilibili.com", "Origin": "https://www.bilibili.com"}
    data = await http_get_json(url, headers)
    if data is None or not isinstance(data, dict) or data.get("code") != 0:
        return None

    play = data.get("data", {})
    target = None
    for d in (play.get("durl") or []):
        if d.get("url"):
            target = d["url"]
            break

    if target:
        cache_set(key, json.dumps(target, ensure_ascii=False), 60)
    return target


# ═══════════════════════════════════════════════════════════════
# UP主
# ═══════════════════════════════════════════════════════════════

async def up_info(mid: int) -> Optional[dict]:
    key = f"up:{mid}"
    if cached := cache_get(key, ttl=600):
        return json.loads(cached)

    info = await _req("/x/space/acc/info", {"mid": mid})
    if not info:
        return None

    result = {
        "mid": info.get("mid", mid),
        "name": info.get("name", ""),
        "face": info.get("face", ""),
        "sign": info.get("sign", ""),
        "fans": info.get("follower", 0),
    }
    cache_set(key, json.dumps(result, ensure_ascii=False), 600)
    return result


# 判断标题是否可能有多P（避免对所有视频都调video_info）
_MULTI_P_PATTERNS = ["上集", "下集", "中集", "上/", "/下", "分P", "P1", "P2", "P3", "全集",
                     "(上)", "(下)", "（上）", "（下）", "[上]", "[下]", "［上］", "［下］",
                     "上/下", "上、下"]


def _may_have_multip(title: str) -> bool:
    tl = title.lower()
    for pat in _MULTI_P_PATTERNS:
        if pat in tl:
            return True
    return False


async def _prefetch_pages(videos: list):
    """预拉多P信息：只对标题有分P特征的视频调video_info拿pages"""
    import asyncio

    async def _fetch(v: dict):
        bvid = v.get("bvid", "")
        title = v.get("title", "")
        if not bvid or not _may_have_multip(title):
            return
        vi = await video_info(bvid)
        if vi and len(vi.get("pages", [])) > 1:
            v["pages"] = vi["pages"]

    await asyncio.gather(*[_fetch(v) for v in videos if v.get("bvid")])


async def up_videos(mid: int, pn: int = 1, ps: int = 30, force: bool = False) -> Optional[dict]:
    key = f"uv:{mid}:{pn}:{ps}"
    if force:
        cache_delete(key)
    if not force:
        if cached := cache_get(key, ttl=3600):
            return json.loads(cached)

    all_videos = []
    total = 0
    up_name = ""
    all_pages_ok = True

    for page in range(1, 501):
        raw = None
        for attempt in range(3):
            raw = await _req("/x/space/wbi/arc/search",
                             {"mid": mid, "pn": page, "ps": ps}, wbi=True)
            if raw:
                break
            await asyncio.sleep(1)
        if not raw:
            all_pages_ok = False
            break

        # 兼容 B站 API 多种返回格式
        raw_list = raw.get("list", {})
        if isinstance(raw_list, dict):
            vlist = raw_list.get("vlist", [])
        elif isinstance(raw_list, list):
            vlist = raw_list
        else:
            vlist = raw.get("vlist", [])
        if not vlist:
            break

        if page == 1:
            total = raw.get("page", {}).get("count", 0) or raw.get("count", 0) or 0
            up = await up_info(mid)
            up_name = up.get("name", "") if up else ""

        for v in vlist:
            dur = v.get("length") or v.get("duration", 0)
            if isinstance(dur, str):
                parts = dur.split(":")
                dur = int(parts[0]) * 60 + int(parts[1]) if len(parts) >= 2 else 0
            all_videos.append({
                "bvid": v.get("bvid", ""),
                "title": v.get("title", ""),
                "pic": v.get("pic", ""),
                "duration": dur,
                "play": v.get("play", 0) or v.get("stat", {}).get("view", 0),
                "pubdate": v.get("created", 0) or v.get("pubdate", 0),
                "cid": v.get("cid", 0),
            })

        if len(vlist) < ps:
            break

    if not all_videos:
        return None

    if len(all_videos) > total:
        total = len(all_videos)

    result = {
        "mid": mid,
        "name": up_name,
        "total": int(total),
        "page": pn,
        "videos": all_videos,
    }

    # 预拉多P信息（只拉标题有分P特征），缓存进列表数据
    await _prefetch_pages(all_videos)

    if all_pages_ok:
        cache_set(key, json.dumps(result, ensure_ascii=False), 3600)
    return result


# ═══════════════════════════════════════════════════════════════
# 合集
# ═══════════════════════════════════════════════════════════════

async def fetch_series_meta(series_id: int) -> Optional[dict]:
    """获取合集元信息（名称、描述等），来自 /x/series/series"""
    url = f"{_BASE}/x/series/series?series_id={series_id}"
    cfg = load_config()
    cookie = cfg.get("bili_cookie", cfg.get("cookie", ""))
    headers = {
        "User-Agent": _UA,
        "Referer": "https://space.bilibili.com",
        "Cookie": cookie,
    }
    data = await http_get_json(url, headers)
    if not data or not isinstance(data, dict) or data.get("code") != 0:
        return None
    meta = data.get("data", {}).get("meta")
    if not meta:
        return None
    return {
        "series_id": meta.get("series_id"),
        "mid": meta.get("mid"),
        "name": meta.get("name", ""),
        "description": meta.get("description", ""),
        "total": meta.get("total", 0),
    }


async def series_videos(series_id: int, mid: str = "", pn: int = 1, ps: int = 50, fmt: str = "channel", force: bool = False) -> Optional[dict]:
    full_key = f"sv_full:{series_id}:{mid}:{fmt}"
    if force:
        cache_delete(full_key)
    if not force:
        if cached := cache_get(full_key, ttl=7200):
            return json.loads(cached)

    videos = []
    total = 0
    name = ""

    if fmt == "list":
        for page in range(1, 501):
            url = f"https://api.bilibili.com/x/v2/medialist/resource/list?type=8&biz_id={series_id}&pn={page}&ps={ps}"
            cfg_local = load_config()
            cookie_local = cfg_local.get("bili_cookie", cfg_local.get("cookie", ""))
            raw = await http_get_json(url, {
                "User-Agent": _UA,
                "Referer": "https://www.bilibili.com",
                "Origin": "https://www.bilibili.com",
                "Cookie": cookie_local,
            })
            if not raw or not isinstance(raw, dict) or raw.get("code", -1) != 0:
                break
            items = raw.get("data", {}).get("media_list", [])
            if not items:
                break
            for item in items:
                videos.append({
                    "bvid": item.get("bv_id", ""),
                    "title": item.get("title", ""),
                    "pic": item.get("cover", ""),
                    "duration": item.get("duration", 0),
                    "play": item.get("cnt_info", {}).get("play", 0),
                    "pubdate": item.get("pubtime", 0),
                    "cid": item.get("cid", 0),
                })
            if len(items) < ps:
                break
        total = len(videos)
    else:
        for page in range(1, 501):
            params = {"series_id": series_id, "pn": page, "ps": ps}
            if mid:
                params["mid"] = mid
            raw = await _req("/x/series/archives", params, wbi=True)
            if not raw:
                break
            archives = raw.get("archives", []) or raw.get("list", [])
            if not archives:
                break
            if page == 1:
                total = raw.get("total", 0) or raw.get("count", 0) or 0
                if "series" in raw:
                    name = raw["series"].get("name", "")
            for v in archives:
                videos.append({
                    "bvid": v.get("bvid", ""),
                    "title": v.get("title", ""),
                    "pic": v.get("pic", ""),
                    "duration": v.get("duration", 0),
                    "play": v.get("stat", {}).get("view", 0),
                    "pubdate": v.get("pubdate", 0),
                    "cid": v.get("cid", 0),
                })
            if len(archives) < ps:
                break
        if len(videos) > total:
            total = len(videos)

    result = {"series_id": series_id, "mid": mid, "title": name, "total": int(total), "videos": videos}

    # 预拉多P信息（只拉标题有分P特征），缓存进系列数据
    await _prefetch_pages(videos)

    cache_set(full_key, json.dumps(result, ensure_ascii=False), 7200)
    key = f"sv:{series_id}:{mid}:{pn}:{ps}:{fmt}"
    cache_set(key, json.dumps(result, ensure_ascii=False), 7200)
    return result


# ═══════════════════════════════════════════════════════════════
# 搜索
# ═══════════════════════════════════════════════════════════════

async def search_videos(keyword: str, pn: int = 1) -> Optional[dict]:
    key = f"se:{keyword}:{pn}"
    if cached := cache_get(key, ttl=600):
        return json.loads(cached)

    raw = await _req("/x/web-interface/search/all/v2", {"keyword": keyword, "page": pn})
    if not raw:
        return None

    items = []
    for r in (raw.get("result") or []):
        if r.get("result_type") == "video":
            items = r.get("data") or []
            break

    results = [
        {"bvid": v.get("bvid", ""), "title": v.get("title", ""),
         "author": v.get("author", ""), "pic": v.get("pic", ""),
         "duration": v.get("duration", 0),
         "play": v.get("play", 0) or v.get("stat", {}).get("view", 0)}
        for v in items
    ]

    info = {"keyword": keyword, "total": raw.get("numResults", 0), "page": pn, "results": results}
    cache_set(key, json.dumps(info, ensure_ascii=False), 600)
    return info