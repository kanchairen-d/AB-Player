"""Config management + cache"""

import json, hashlib, time, os, random
from pathlib import Path
from typing import Optional
import httpx

BASE_DIR = Path("/app")
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = DATA_DIR / "config.json"
CACHE_DIR = DATA_DIR / "cache"

CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ─── 共享 httpx 客户端（高并发连接池） ───
_HTTPX_LIMITS = httpx.Limits(max_keepalive_connections=100, max_connections=100, keepalive_expiry=30)
_HTTPX_CLIENT = httpx.AsyncClient(
    limits=_HTTPX_LIMITS,
    timeout=httpx.Timeout(15.0, connect=5.0),
    follow_redirects=True,
)

# Cache TTL: 1 hour
_CACHE_MAX_AGE = 3600

# ─── 内存缓存（热缓存层） ───
_mem_cache = {}
_MEM_TTL = {}


def _mem_get(key: str):
    """内存缓存读取"""
    if key in _MEM_TTL:
        if time.time() < _MEM_TTL[key]:
            return _mem_cache[key]
        # 过期清理
        del _mem_cache[key], _MEM_TTL[key]
    return None


def _mem_set(key: str, value: str, ttl: int = 600):
    """内存缓存写入"""
    _mem_cache[key] = value
    _MEM_TTL[key] = time.time() + ttl
    # 限制大小，超过 500 条清理最旧的
    if len(_mem_cache) > 500:
        try:
            oldest = min(_MEM_TTL.keys(), key=lambda k: _MEM_TTL[k])
            del _mem_cache[oldest], _MEM_TTL[oldest]
        except Exception:
            pass


def _cleanup_cache():
    """Remove expired cache files"""
    now = time.time()
    count = 0
    for f in CACHE_DIR.iterdir():
        if f.is_file():
            mtime = f.stat().st_mtime
            if now - mtime > _CACHE_MAX_AGE:
                f.unlink()
                count += 1
    return count


# Run cleanup on import
_cleanup_cache()


def cache_get(key: str, ttl: int = 600) -> Optional[str]:
    """缓存读取——先查内存，再查文件"""
    # 先查内存缓存
    val = _mem_get(key)
    if val is not None:
        return val
    # 再查文件缓存
    path = CACHE_DIR / hashlib.md5(key.encode()).hexdigest()
    if path.exists():
        if time.time() - path.stat().st_mtime < ttl:
            val = path.read_text(encoding="utf-8")
            _mem_set(key, val, ttl)  # 同步到内存
            return val
        path.unlink()
    return None


def cache_set(key: str, value: str, ttl: int = 600):
    """缓存写入——同时写内存和文件"""
    _mem_set(key, value, ttl)  # 先写内存
    path = CACHE_DIR / hashlib.md5(key.encode()).hexdigest()
    path.write_text(value, encoding="utf-8")  # 再写文件持久化


def cache_delete(key: str):
    """删除指定缓存（内存+文件）"""
    if key in _mem_cache:
        del _mem_cache[key]
        if key in _MEM_TTL:
            del _MEM_TTL[key]
    path = CACHE_DIR / hashlib.md5(key.encode()).hexdigest()
    if path.exists():
        path.unlink()


def cache_delete_prefix(prefix: str):
    """删除所有以 prefix 开头的内存缓存"""
    for k in list(_mem_cache.keys()):
        if k.startswith(prefix):
            del _mem_cache[k]
            if k in _MEM_TTL:
                del _MEM_TTL[k]


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _buvid3() -> str:
    return f"{random.randint(10**10, 10**11-1):X}{random.randint(10**10, 10**11-1):X}"


async def http_get(url: str, headers: dict | None = None) -> str:
    h = {"User-Agent": _UA, "Cookie": f"buvid3={_buvid3()}; b_nut={int(time.time())}"}
    if headers:
        h.update(headers)
    r = await _HTTPX_CLIENT.get(url, headers=h)
    return r.text


async def http_get_json(url: str, headers: dict | None = None) -> dict | list | None:
    h = {"User-Agent": _UA, "Cookie": f"buvid3={_buvid3()}; b_nut={int(time.time())}"}
    if headers:
        h.update(headers)
    r = await _HTTPX_CLIENT.get(url, headers=h)
    if r.status_code != 200 or not r.content.strip():
        return None
    try:
        return r.json()
    except Exception:
        return None