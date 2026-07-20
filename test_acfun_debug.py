"""Test A站 play URL and M3U8 fetch inside container"""
import asyncio, sys, urllib.request, json

sys.path.insert(0, '/app')
from app.acfun import get_play_url, _headers

async def test():
    # Test 1: get_play_url for single-part video
    print("=== Test 1: get_play_url(48686034, 39003529) ===")
    url = await get_play_url('48686034', '39003529')
    print(f"Result: {str(url)[:80] if url else 'None'}")
    
    if url and url.startswith("http"):
        print(f"Result is a URL (not rewritten M3U8 content)")
        headers = _headers()
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        try:
            r = urllib.request.Request(url, headers=dict(headers))
            resp = urllib.request.urlopen(r, timeout=10)
            data = resp.read().decode("utf-8")
            print(f"M3U8 OK: {len(data)} bytes")
            print(f"First 80 chars: {data[:80]}")
        except Exception as e:
            print(f"M3U8 fetch error: {type(e).__name__}: {str(e)[:80]}")
    elif url:
        print(f"Result is M3U8 content (not URL), length: {len(url)}")
    else:
        print("Result is None")

    # Test 2: Try REST API directly with httpx
    print("\n=== Test 2: REST API via httpx ===")
    import httpx
    headers = _headers()
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    async with httpx.AsyncClient(timeout=10) as cli:
        resp = await cli.get(
            "https://www.acfun.cn/rest/pc-direct/play/playInfo/ksPlayJson?videoId=39003529",
            headers=headers
        )
        print(f"Status: {resp.status_code}")
        data = resp.json()
        print(f"Result: {data.get('result')}")
        pi = data.get("playInfo", {})
        ksp = pi.get("ksPlayJson", "")
        if ksp:
            pd = json.loads(ksp) if isinstance(ksp, str) else ksp
            ads = pd.get("adaptationSet", [])
            for ad in ads:
                reps = ad.get("representation", [])
                for rep in reps:
                    rurl = rep.get("url", "")
                    if rurl:
                        print(f"Best URL: {rurl[:80]}...")
                        break
                break

    # Test 3: Fetch M3U8 with httpx
    print("\n=== Test 3: Fetch M3U8 with httpx ===")
    best_url = None
    for ad in pd.get("adaptationSet", []):
        for rep in ad.get("representation", []):
            rurl = rep.get("url", "")
            if rurl:
                best_url = rurl
                break
        if best_url:
            break
    
    if best_url:
        resp2 = await cli.get(best_url)
        print(f"M3U8 Status: {resp2.status_code}, Length: {len(resp2.text)}")
        if resp2.status_code == 200:
            lines = resp2.text.split("\n")
            ts_urls = [l.strip() for l in lines if l.strip() and not l.startswith("#") and ".ts" in l.lower()]
            print(f"TS count: {len(ts_urls)}")
            if ts_urls:
                print(f"First TS: {ts_urls[0][:80]}")
                # Try to fetch TS with httpx
                cdn_base = best_url.rsplit("/", 1)[0] + "/"
                full_ts = cdn_base + ts_urls[0] if not ts_urls[0].startswith("http") else ts_urls[0]
                print(f"Full TS URL: {full_ts[:80]}")
                resp3 = await cli.get(full_ts)
                print(f"TS Status: {resp3.status_code}, Size: {len(resp3.content)}")

asyncio.run(test())