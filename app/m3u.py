"""M3U 订阅生成（B站 + A站）"""

import re
from fastapi import Request, Response
from .config import load_config
from .bilibili import series_videos, up_videos, video_info
from .acfun import fetch_album_videos, fetch_user_videos, expand_videos_for_m3u, get_play_url_proxy


def _get_part_label(page_num: int, total_parts: int) -> str:
    """根据分P总数返回人性化标题"""
    if total_parts == 2:
        return "上集" if page_num == 1 else "下集"
    elif total_parts == 3:
        labels = ["上集", "中集", "下集"]
    else:
        labels = [f"第{i}集" for i in range(1, total_parts + 1)]
    if 1 <= page_num <= len(labels):
        return labels[page_num - 1]
    return f"P{page_num}"


def bili_expand_videos_for_m3u(videos: list) -> list:
    """展开B站多P视频为独立条目"""
    expanded = []
    for v in videos:
        pages = v.get("pages", [])
        total_pages = len(pages)
        if total_pages > 1:
            vt = v.get("title", "未知")
            for i, pg in enumerate(pages):
                entry = dict(v)
                pn = pg.get("page", i + 1)
                label = _get_part_label(pn, total_pages)
                entry["page"] = pn
                entry["part"] = label
                entry["title"] = f"{vt} - {label}"
                expanded.append(entry)
        else:
            expanded.append(v)
    return expanded


async def build_bili_m3u(
    request: Request,
    cfg: dict,
    sub: str = "",
    ids: str = "",
    live_only: bool = False,
) -> Response:
    """构建B站综合M3U"""
    base_url = str(request.base_url).rstrip("/")

    # 过滤
    allowed = {}
    if sub and not ids:
        for s in cfg.get("subscriptions", []):
            if s.get("name") == sub:
                for item in s.get("items", []):
                    allowed[item] = True
                break
        ids = ",".join(allowed.keys())
    if ids:
        for item_id in ids.split(","):
            item_id = item_id.strip()
            if item_id:
                allowed[item_id] = True

    def filter_series(series_list):
        if not allowed:
            return series_list
        return [s for s in series_list if f"series_{s.get('series_id','') or s.get('id','')}" in allowed]

    def filter_ups(ups_list):
        if not allowed:
            return ups_list
        return [u for u in ups_list if f"up_{u.get('mid','')}" in allowed]

    def filter_videos(vids_list):
        if not allowed:
            return vids_list
        return [v for v in vids_list if f"video_{v.get('id','')}" in allowed]

    def filter_rooms(rooms_list):
        if not allowed:
            return rooms_list
        return [r for r in rooms_list if f"room_{r.get('room_id','') if isinstance(r, dict) else r}" in allowed]

    lines = ["#EXTM3U", "#PLAYLIST:AB Player 订阅\n"]

    # B站合集
    if not live_only:
        bili_series = cfg.get("bili_series", []) or cfg.get("series", [])
        for s in filter_series(bili_series):
            sid = s.get("series_id") or s.get("id", "")
            s_mid = s.get("mid", "")
            s_name = s.get("name", "合集")
            s_fmt = s.get("fmt", "channel")
            if not sid:
                continue
            videos_data = await series_videos(int(sid), mid=s_mid, pn=1, ps=100, fmt=s_fmt)
            if not videos_data:
                continue
            videos = videos_data.get("videos", [])
            expanded = bili_expand_videos_for_m3u(videos)
            for v in expanded:
                bvid = v.get("bvid", "")
                if not bvid:
                    continue
                vtitle = v.get("title", "未知")
                dur = int(v.get("duration", 0))
                lines.append(f"#EXTINF:{dur} group-title=\"{s_name}\",{vtitle}")
                lines.append(f"{base_url}/bili?id={bvid}")

        # B站UP主
        for u in filter_ups(cfg.get("bili_ups", []) or cfg.get("ups", [])):
            mid = u.get("mid", "")
            name = u.get("name", "UP主")
            if not mid:
                continue
            videos_data = await up_videos(int(mid), pn=1, ps=50)
            if not videos_data:
                continue
            videos = videos_data.get("videos", [])
            expanded = bili_expand_videos_for_m3u(videos)
            for v in expanded:
                bvid = v.get("bvid", "")
                if not bvid:
                    continue
                vtitle = v.get("title", "未知")
                dur = int(v.get("duration", 0))
                lines.append(f"#EXTINF:{dur} group-title=\"{name}\",{vtitle}")
                lines.append(f"{base_url}/bili?id={bvid}")

        # B站单视频
        for v in filter_videos(cfg.get("bili_videos", []) or cfg.get("videos", [])):
            vid = v.get("id", "")
            vname = v.get("name", "B站视频")
            if vid:
                lines.append(f"#EXTINF:-1 group-title=\"B站单视频\",{vname}")
                lines.append(f"{base_url}/bili?id={vid}")

    # B站直播
    for r in filter_rooms(cfg.get("bili_rooms", []) or cfg.get("rooms", [])):
        rid = r["room_id"] if isinstance(r, dict) else r
        rname = r.get("name", f"B站直播 #{rid}") if isinstance(r, dict) else f"B站直播 #{rid}"
        lines.append(f"#EXTINF:-1 group-title=\"B站直播\",{rname}")
        lines.append(f"{base_url}/bili?room={rid}")

    # A站合辑
    if not live_only:
        for a in cfg.get("acfun_albums", []):
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
                lines.append(f"#EXTINF:{dur} group-title=\"{name}\",{vtitle}")
                cid_qs = f"&cid={cid}" if cid else ""
                lines.append(f"{base_url}/acfun?id={vid}&play=1{cid_qs}")

        # A站UP主
        for u in cfg.get("acfun_ups", []):
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
                lines.append(f"#EXTINF:{dur} group-title=\"{name}\",{vtitle}")
                cid_qs = f"&cid={cid}" if cid else ""
                lines.append(f"{base_url}/acfun?id={vid}&play=1{cid_qs}")

        # A站单视频
        for v in cfg.get("acfun_videos", []):
            vid = v.get("id", "")
            name = v.get("name", "A站视频")
            if vid:
                lines.append(f"#EXTINF:-1 group-title=\"A站单视频\",{name}")
                lines.append(f"{base_url}/acfun?id={vid}&play=1")

    return Response("\n".join(lines), media_type="application/x-mpegURL; charset=utf-8")


def _fmt_m3u_title(title: str) -> str:
    title = re.sub(r"^【[^】]*】", "", title).strip()
    m = re.match(r"^(.*[^\s])\s*[（(]([^）)]+)[）)]\s*-\s*(.+)$", title)
    if m:
        return f"{m.group(1).strip()}-{m.group(3).strip()}（{m.group(2).strip()}）"
    m = re.match(r"^(.*)\s*-\s*(.+)$", title)
    if m:
        return f"{m.group(1).strip()}-{m.group(2).strip()}"
    return title


def register(app):
    """注册 M3U 综合路由"""
    from fastapi import APIRouter, Query

    router = APIRouter()

    @router.get("/m3u")
    async def m3u_root(request: Request, sub: str = Query(None), ids: str = Query(None), live: str = Query(None)):
        cfg = load_config()
        return await build_bili_m3u(request, cfg, sub=sub, ids=ids, live_only=live)

    app.include_router(router)


async def _prefetch_play_urls(cfg: dict, items: list):
    """后台预拉 play_url，让用户第一次播放时秒开"""
    import asyncio
    from .bilibili import series_videos, up_videos, video_info, video_playurl
    from .config import load_config, cache_get

    bvids = []
    for item_id in items:
        if item_id.startswith("series_"):
            sid = item_id.replace("series_", "")
            for s in cfg.get("bili_series", []):
                if str(s.get("series_id", "") or s.get("id", "")) == sid:
                    vd = await series_videos(int(sid), mid=s.get("mid", ""), pn=1, ps=100, fmt=s.get("fmt", "channel"))
                    if vd:
                        for v in vd.get("videos", [])[:30]:  # 只预拉前30个
                            if v.get("bvid"):
                                bvids.append(v["bvid"])
                    break
        elif item_id.startswith("up_"):
            mid = item_id.replace("up_", "")
            for u in cfg.get("bili_ups", []):
                if str(u.get("mid", "")) == mid:
                    vd = await up_videos(int(mid), pn=1, ps=30)
                    if vd:
                        for v in vd.get("videos", [])[:30]:
                            if v.get("bvid"):
                                bvids.append(v["bvid"])
                    break
        elif item_id.startswith("video_"):
            vid = item_id.replace("video_", "")
            for v in cfg.get("bili_videos", []):
                if str(v.get("id", "")) == vid:
                    if vid:
                        bvids.append(vid)
                    break

    async def _prefetch(bvid: str):
        vi = await video_info(bvid)
        if vi:
            pages = vi.get("pages", [])
            first_cid = pages[0]["cid"] if pages else vi.get("cid", 0)
            if first_cid:
                await video_playurl(bvid, int(first_cid), qn=116, fnval=4048)

    await asyncio.gather(*[_prefetch(b) for b in bvids[:30]])


async def build_sub_m3u(request: Request, cfg: dict, sub_name: str) -> Response:
    """根据订阅名称生成统一 M3U（合并B站+A站），同步预拉所有 playurl 供播放器秒开"""
    from .config import cache_get, cache_set
    from .bilibili import video_info, video_playurl

    # M3U 输出缓存 1 小时
    m3u_key = f"m3u_out:{sub_name}"
    if cached := cache_get(m3u_key, ttl=3600):
        return Response(cached, media_type="application/x-mpegURL; charset=utf-8")

    base_url = str(request.base_url).rstrip("/")
    lines = ["#EXTM3U", f"#PLAYLIST:{sub_name}\n"]

    # 找到订阅
    items = []
    if sub_name == "_all":
        # 合并所有订阅的项目
        seen = set()
        for s in cfg.get("subscriptions", []):
            sg = s.get("groups")
            if sg:
                for g in sg:
                    for it in g.get("items", []):
                        if it not in seen:
                            seen.add(it)
                            items.append(it)
            else:
                for it in s.get("items", []):
                    if it not in seen:
                        seen.add(it)
                        items.append(it)
        sub_name = "All"
    else:
        subscription = None
        for s in cfg.get("subscriptions", []):
            if s.get("name") == sub_name:
                subscription = s
                break
        if not subscription:
            return Response("订阅不存在", status_code=404)
        # 优先 groups，其次旧格式 items
        sg = subscription.get("groups")
        if sg:
            items = []
            for g in sg:
                items.extend(g.get("items", []))
        else:
            items = subscription.get("items", [])

    lines = ["#EXTM3U", f"#PLAYLIST:{sub_name}\n"]
    # 收集所有视频 BVID 用于预拉 playurl
    prefetch_bvids: list[tuple[str, int]] = []  # (bvid, cid)
    prefetch_acfun: list[tuple[str, str]] = []  # (vid, cid)

    import asyncio

    # 判断标题是否可能有多P（避免对所有视频都调video_info）
    _MULTI_P_PATTERNS = ["上集", "下集", "中集", "上/", "/下", "分P", "P1", "P2", "P3", "全集", "(上)","(下)","（上）","（下）","[上]","[下]","［上］","［下］","上/下","上、下"]

    def _may_have_multip(title: str) -> bool:
        tl = title.lower()
        for pat in _MULTI_P_PATTERNS:
            if pat in tl:
                return True
        return False

    async def _expand_bili_video(bvid: str, group_title: str, base_url: str,
                                  cached_title: str = "", cached_dur: int = 0,
                                  cached_pages: list | None = None,
                                  cached_cid: int = 0) -> list:
        """展开B站视频分P。优先使用预缓存的pages和cid，跳过video_info"""
        pages = cached_pages or []
        if not pages and _may_have_multip(cached_title):
            vi = await video_info(bvid)
            if vi:
                pages = vi.get("pages", [])
                vt = vi.get("title", cached_title or "未知")
            else:
                vt = cached_title or "未知"
        else:
            vt = cached_title or "未知"

        total = len(pages)
        if total > 1:
            results = []
            for i, pg in enumerate(pages):
                pn = pg.get("page", i + 1)
                label = _get_part_label(pn, total)
                dur = int(pg.get("duration", 0) or (cached_dur // total if cached_dur > 0 else -1))
                cid = pg.get("cid", 0)
                cid_q = f"&cid={cid}" if cid else ""
                results.append({
                    "extinf": f'#EXTINF:{dur} group-title="{group_title}",{vt} - {label}',
                    "url": f"{base_url}/bili?id={bvid}{cid_q}&p={pn}&proxy=true",
                })
            return results

        dur = cached_dur if cached_dur > 0 else -1
        cid_q = f"&cid={cached_cid}" if cached_cid else ""
        return [{
            "extinf": f'#EXTINF:{dur} group-title="{group_title}",{vt}',
            "url": f"{base_url}/bili?id={bvid}{cid_q}&proxy=true",
        }]

    async def process_item(item_id: str):
        nonlocal lines
        # B站合集
        if item_id.startswith("series_"):
            sid = item_id.replace("series_", "")
            for s in cfg.get("bili_series", []):
                if str(s.get("series_id", "") or s.get("id", "")) == sid:
                    vd = await series_videos(int(sid), mid=s.get("mid", ""), pn=1, ps=100, fmt=s.get("fmt", "channel"))
                    if not vd:
                        return
                    s_name = s.get("name", "合集")
                    tasks = []
                    for v in vd.get("videos", []):
                        bvid = v.get("bvid", "")
                        if not bvid:
                            continue
                        prefetch_bvids.append((bvid, int(v.get("cid", 0) or 0)))
                        tasks.append(_expand_bili_video(
                            bvid, s_name, base_url,
                            cached_title=v.get("title", ""),
                            cached_dur=int(v.get("duration", 0)),
                            cached_pages=v.get("pages"),
                            cached_cid=int(v.get("cid", 0) or 0),
                        ))
                    for exp_list in await asyncio.gather(*tasks):
                        for entry in exp_list:
                            lines.append(entry["extinf"])
                            lines.append(entry["url"])
                    return

        # B站UP主
        if item_id.startswith("up_"):
            mid = item_id.replace("up_", "")
            for u in cfg.get("bili_ups", []):
                if str(u.get("mid", "")) == mid:
                    vd = await up_videos(int(mid), pn=1, ps=50)
                    if not vd:
                        return
                    uname = u.get("name", "UP主")
                    tasks = []
                    for v in vd.get("videos", []):
                        bvid = v.get("bvid", "")
                        if not bvid:
                            continue
                        prefetch_bvids.append((bvid, int(v.get("cid", 0) or 0)))
                        tasks.append(_expand_bili_video(
                            bvid, uname, base_url,
                            cached_title=v.get("title", ""),
                            cached_dur=int(v.get("duration", 0)),
                            cached_pages=v.get("pages"),
                            cached_cid=int(v.get("cid", 0) or 0),
                        ))
                    for exp_list in await asyncio.gather(*tasks):
                        for entry in exp_list:
                            lines.append(entry["extinf"])
                            lines.append(entry["url"])
                    return

        # B站单视频
        if item_id.startswith("video_"):
            vid = item_id.replace("video_", "")
            for v in cfg.get("bili_videos", []):
                if str(v.get("id", "")) == vid:
                    prefetch_bvids.append((vid, 0))
                    exp = await _expand_bili_video(vid, "B站单视频", base_url, cached_title=v.get("name", "B站视频"))
                    for entry in exp:
                        lines.append(entry["extinf"])
                        lines.append(entry["url"])
                    return

        # B站直播
        if item_id.startswith("room_"):
            rid = item_id.replace("room_", "")
            for r in cfg.get("bili_rooms", []):
                if str(r.get("room_id", "")) == rid:
                    lines.append(f'#EXTINF:-1 group-title="B站直播",{r.get("name",f"B站直播 #{rid}")}')
                    lines.append(f"{base_url}/bili?room={rid}")
                    return

        # A站合辑
        if item_id.startswith("acfun_album_"):
            aid = item_id.replace("acfun_album_", "")
            for a in cfg.get("acfun_albums", []):
                if str(a.get("id", "")) == aid:
                    videos = await fetch_album_videos(aid)
                    if not videos:
                        return
                    aname = a.get("name", "A站合辑")
                    for v in expand_videos_for_m3u(videos):
                        vid = v.get("id", "")
                        cid = v.get("cid", "")
                        dur = int(v.get("duration", 0))
                        lines.append(f'#EXTINF:{dur} group-title="{aname}",{v.get("title","未知")}')
                        cid_qs = f"&cid={cid}" if cid else ""
                        lines.append(f"{base_url}/acfun?id={vid}&play=1{cid_qs}")
                        prefetch_acfun.append((vid, cid))
                    return

        # A站UP主
        if item_id.startswith("acfun_up_"):
            mid = item_id.replace("acfun_up_", "")
            for u in cfg.get("acfun_ups", []):
                if str(u.get("mid", "") or u.get("uid", "")) == mid:
                    videos = await fetch_user_videos(mid)
                    if not videos:
                        return
                    uname = u.get("name", "A站UP主")
                    for v in expand_videos_for_m3u(videos):
                        vid = v.get("id", "")
                        cid = v.get("cid", "")
                        dur = int(v.get("duration", 0))
                        lines.append(f'#EXTINF:{dur} group-title="{uname}",{v.get("title","未知")}')
                        cid_qs = f"&cid={cid}" if cid else ""
                        lines.append(f"{base_url}/acfun?id={vid}&play=1{cid_qs}")
                        prefetch_acfun.append((vid, cid))
                    return

        # A站单视频
        if item_id.startswith("acfun_video_"):
            vid = item_id.replace("acfun_video_", "")
            for v in cfg.get("acfun_videos", []):
                if str(v.get("id", "")) == vid:
                    lines.append(f'#EXTINF:-1 group-title="A站单视频",{v.get("name","A站视频")}')
                    lines.append(f"{base_url}/acfun?id={vid}&play=1")
                    prefetch_acfun.append((vid, ""))
                    return

    await asyncio.gather(*[process_item(it) for it in items])
    output = "\n".join(lines)

    # ═══ 同步预拉所有视频 playurl，缓存住让播放器秒开 ═══
    # 去重，同一个 BVID 只需拉一次
    seen_bvids: set[str] = set()
    async def _prefetch_one(bvid: str, cid: int):
        if not bvid or bvid in seen_bvids:
            return
        seen_bvids.add(bvid)
        if cid:
            await video_playurl(bvid, cid, qn=116, fnval=4048)
        else:
            vid_info = await video_info(bvid)
            if vid_info:
                pages = vid_info.get("pages", [])
                first_cid = pages[0]["cid"] if pages else vid_info.get("cid", 0)
                if first_cid:
                    await video_playurl(bvid, int(first_cid), qn=116, fnval=4048)

    if prefetch_bvids:
        await asyncio.gather(*[
            _prefetch_one(bvid, cid) for bvid, cid in prefetch_bvids
        ])

    # ═══ 后台预拉所有A站视频 playurl（不阻塞 M3U 响应） ═══
    if prefetch_acfun:
        seen_acfun: set[str] = set()
        _acfun_sem = asyncio.Semaphore(10)

        async def _prefetch_one_acfun(vid: str, cid: str):
            async with _acfun_sem:
                await get_play_url_proxy(vid, base_url, cid)

        async def _prefetch_acfun_background():
            tasks = []
            for vid, cid in prefetch_acfun:
                if not vid or vid in seen_acfun:
                    continue
                seen_acfun.add(vid)
                tasks.append(_prefetch_one_acfun(vid, cid))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.ensure_future(_prefetch_acfun_background())

    cache_set(m3u_key, output, 3600)
    return Response(output, media_type="application/x-mpegURL; charset=utf-8")