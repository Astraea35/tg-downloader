"""
全网网盘资源聚合搜索插件 (Dual-Engine: 珈珈搜索 feapi.xyz + 不二搜索 buerchen.top)
- 4K精选秒解：内置不二搜索 6,800+ 每日 4K/1080P 精选影视专库，瞬时秒解免风控直链。
- 双引擎并发聚合：并发检索珈珈搜索与不二搜索全网资源，智能去重合并排序。
- 覆盖各大主流网盘：夸克、百度、阿里、迅雷、UC、123、115、天翼、移动等。
- 极致纯净直达：100% 真实网盘直链与提取码，绝无中间跳转搜索页，支持单条资源一键点播解析。
"""

import os
import re
import time
import html
import json
import asyncio
import hashlib
import urllib.request
import urllib.parse
from typing import Dict, List, Optional, Tuple, Set

import pyrogram
from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.enums import ParseMode
from loguru import logger

PLUGIN_NAME = "网盘资源聚合搜索"
PLUGIN_DESC = "多引擎并发聚合检索 (PanSearch + 爱盼 4K/网盘聚合 + 不二 4K精选/爬虫 + 珈珈自愈)，覆盖夸克/阿里/百度/迅雷/UC/123/115等多网盘"
PLUGIN_VERSION = "3.1"

# 不二搜索主备域名配置
BUERCHEN_DOMAINS = [
    "http://buerchen.top",
    "http://buerchen.cn",
]

# 网盘渠道配置字典
CATEGORIES = {
    "all": {"name": "全部网盘", "icon": "🌐", "buer_type": None},
    "quark": {"name": "夸克网盘", "icon": "⚡", "buer_type": 0},
    "ali": {"name": "阿里网盘", "icon": "📦", "buer_type": None},
    "baidu": {"name": "百度网盘", "icon": "🐻", "buer_type": 2},
    "xunlei": {"name": "迅雷网盘", "icon": "⚡", "buer_type": 4},
    "uc": {"name": "UC网盘", "icon": "🐿️", "buer_type": None},
    "123": {"name": "123网盘", "icon": "📁", "buer_type": None},
    "115": {"name": "115网盘", "icon": "💾", "buer_type": None},
    "ty189": {"name": "天翼云盘", "icon": "☁️", "buer_type": None},
    "yd139": {"name": "移动云盘", "icon": "📱", "buer_type": None},
}

# 别名映射
CATEGORY_ALIASES = {
    "全部": "all", "all": "all",
    "夸克": "quark", "quark": "quark", "夸克网盘": "quark", "kk": "quark",
    "阿里": "ali", "ali": "ali", "阿里云盘": "ali", "阿里网盘": "ali", "alipan": "ali",
    "百度": "baidu", "baidu": "baidu", "百度网盘": "baidu", "度盘": "baidu", "bd": "baidu",
    "迅雷": "xunlei", "xunlei": "xunlei", "迅雷网盘": "xunlei", "xl": "xunlei",
    "uc": "uc", "UC": "uc", "uc网盘": "uc", "UC网盘": "uc",
    "123": "123", "123pan": "123", "123网盘": "123",
    "115": "115", "115网盘": "115",
    "天翼": "ty189", "ty189": "ty189", "天翼云": "ty189", "天翼网盘": "ty189", "天翼云盘": "ty189",
    "移动": "yd139", "yd139": "yd139", "移动云": "yd139", "移动网盘": "yd139", "移动云盘": "yd139",
}

# 每页展示资源数量
SUBPAGE_SIZE = 6

# 内部会话与全局缓存管理器
_sessions: Dict[str, dict] = {}
_raw_cache: Dict[str, Tuple[List[dict], float]] = {}       # key -> (items, timestamp)
_hot_cache: Tuple[List[str], float] = ([], 0.0)             # (keywords, timestamp)
_buer_curated_cache: List[dict] = []                        # 不二 6800+ 每日精选库
_buer_curated_time: float = 0.0                             # 精选库缓存更新时间
_resolved_link_cache: Dict[str, Tuple[str, str]] = {}       # url/key -> (direct_link, pwd)
_expired_links_cache: Set[str] = set()                      # 记录已失效死链，全局快速剔除
_buer_challenge_until: float = 0.0                          # 不二防刷封锁到期时间戳
_feapi_broken_until: float = time.time() + 3600.0           # 珈珈搜索故障熔断到期时间戳（默认初始静默跳过）
_bot_instance = None


def get_proxy_dict(bot_instance) -> dict:
    """提取机器人配置中的本地代理设置"""
    if not bot_instance:
        return {}
    cfg = getattr(bot_instance, "config", {})
    proxy_cfg = cfg.get("proxy")
    if not proxy_cfg and hasattr(bot_instance, "app") and bot_instance.app:
        proxy_cfg = getattr(bot_instance.app, "proxy", None)

    if isinstance(proxy_cfg, dict):
        scheme = proxy_cfg.get("scheme", "http")
        hostname = proxy_cfg.get("hostname") or proxy_cfg.get("host")
        port = proxy_cfg.get("port")
        if hostname and port:
            proxy_url = f"{scheme}://{hostname}:{port}"
            return {"http": proxy_url, "https": proxy_url}
    elif isinstance(proxy_cfg, str) and proxy_cfg:
        return {"http": proxy_cfg, "https": proxy_cfg}
    return {}


def sync_fetch_url(url: str, post_data: bytes = None, headers: dict = None, proxy_dict: dict = None, timeout: float = 5.0) -> bytes:
    """使用 NAS 配置的代理；仅在未配置代理时直连。"""
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=post_data, headers=req_headers)

    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxy_dict or {}))
        with opener.open(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as network_err:
        logger.error(f"[PanSearch] 访问 {url} 失败: {type(network_err).__name__}")
        raise


async def fetch_url(url: str, post_data: bytes = None, headers: dict = None, timeout: float = 5.0) -> bytes:
    """异步执行网络请求"""
    proxy_dict = get_proxy_dict(_bot_instance)
    return await asyncio.to_thread(sync_fetch_url, url, post_data, headers, proxy_dict, timeout)


# ==============================================================================
# 1. 珈珈搜索引擎 (feapi.xyz)
# ==============================================================================

def parse_feapi_html(html_content: str) -> List[dict]:
    """解析 feapi.xyz 搜索结果页 HTML"""
    articles = re.findall(r'<article class="result-item.*?/article>', html_content, re.DOTALL)
    results = []

    for art in articles:
        link_m = re.search(r'data-real-link="([^"]+)"', art)
        if not link_m:
            continue
        link = link_m.group(1).strip()

        title_m = re.search(r'<h2 class="result-title"><a[^>]*>(.*?)</a>', art, re.DOTALL)
        raw_title = title_m.group(1) if title_m else "未命名资源"
        clean_title = re.sub(r'<[^>]+>', '', raw_title).strip()
        clean_title = html.unescape(clean_title)

        pan_m = re.search(r'<img class="pan-result-icon"[^>]*alt="([^"]*)"', art)
        pan_name = pan_m.group(1).strip() if pan_m else "网盘"

        date_m = re.search(r'(\d{4}-\d{2}-\d{2})', art)
        pub_date = date_m.group(1) if date_m else ""

        pwd_m = re.search(r'[?&]pwd=([a-zA-Z0-9]+)', link)
        pwd = pwd_m.group(1) if pwd_m else ""

        results.append({
            "title": clean_title,
            "pan": pan_name,
            "link": link,
            "date": pub_date,
            "pwd": pwd,
            "source": "珈珈",
            "needs_resolve": False,
        })

    return results


async def query_feapi(keyword: str, category: str, page: int = 1) -> List[dict]:
    """向 feapi.xyz 请求单页结果 (带智能自愈熔断保护)"""
    global _feapi_broken_until
    if time.time() < _feapi_broken_until:
        return []

    url = f"https://feapi.xyz/s/{urllib.parse.quote(keyword.lower())}?page={page}&__full=1"
    if category and category != "all":
        url += f"&category={urllib.parse.quote(category)}"

    try:
        raw_bytes = await fetch_url(url, headers={"X-Search-Full": "1"}, timeout=1.5)
        html_str = raw_bytes.decode("utf-8", errors="ignore")
        items = parse_feapi_html(html_str)
        if items:
            _feapi_broken_until = 0.0
        return items
    except Exception as e:
        logger.warning(f"[PanSearch] 珈珈搜索请求异常，进入 10 分钟熔断保护: {e}")
        _feapi_broken_until = time.time() + 600
        return []


# ==============================================================================
# 2. PanSearch 极速原生 API 搜索引擎 (pansearch.me)
# ==============================================================================

def detect_pan_from_link(link: str, default_pan: str = "") -> Tuple[str, str]:
    """根据链接特征精准识别网盘类型与对应分类 key"""
    link_l = link.lower()
    if "pan.quark.cn" in link_l:
        return "夸克网盘", "quark"
    elif "alipan.com" in link_l or "aliyundrive.com" in link_l:
        return "阿里云盘", "ali"
    elif "pan.baidu.com" in link_l:
        return "百度网盘", "baidu"
    elif "pan.xunlei.com" in link_l:
        return "迅雷网盘", "xunlei"
    elif "drive.uc.cn" in link_l:
        return "UC网盘", "uc"
    elif any(d in link_l for d in ["123pan.com", "123684.com", "123865.com", "123952.com"]):
        return "123网盘", "123"
    elif any(d in link_l for d in ["115.com", "115cdn.com", "anxia.com"]):
        return "115网盘", "115"
    elif "cloud.189.cn" in link_l:
        return "天翼云盘", "ty189"
    elif "caiyun.139.com" in link_l:
        return "移动云盘", "yd139"

    pan_l = (default_pan or "").lower()
    if "quark" in pan_l:
        return "夸克网盘", "quark"
    elif "ali" in pan_l:
        return "阿里云盘", "ali"
    elif "baidu" in pan_l or "bd" in pan_l:
        return "百度网盘", "baidu"
    elif "xunlei" in pan_l:
        return "迅雷网盘", "xunlei"
    elif "uc" in pan_l:
        return "UC网盘", "uc"
    elif "123" in pan_l:
        return "123网盘", "123"
    elif "115" in pan_l:
        return "115网盘", "115"

    return "网盘", "all"


def parse_pansearch_content(raw_c: str, default_pan: str, pub_date: str) -> List[dict]:
    """解析 PanSearch 返回的富文本 content 字段，拆分单条与复合多资源条目"""
    results = []
    links_found = re.findall(r'href=["\'](https?://[^\s"\']+)["\']', raw_c)
    if not links_found:
        links_found = re.findall(r'(https?://(?:pan\.|www\.alipan|www\.aliyundrive|drive\.uc|115\.com|www\.123pan|cloud\.189|caiyun\.139)[^\s\n"<>]+)', raw_c)

    links_clean = [l for l in links_found if "yiso" not in l and "pansearch" not in l]

    if len(links_clean) <= 1:
        m_t = re.search(r'名称[：:\s]*(.*?)(?:\n|<br|描述|链接|$)', raw_c)
        if not m_t:
            m_t = re.search(r'^([^\n<]+)', raw_c)
        title = m_t.group(1).strip() if m_t else "未知标题"
        title = re.sub(r'<[^>]+>', '', title).strip()
        title = html.unescape(title)

        link = links_clean[0] if links_clean else ""
        if link:
            pwd = ""
            pwd_m = re.search(r'[?&]pwd=([a-zA-Z0-9]+)', link) or re.search(r'(?:密码|提取码)[：:\s]*([a-zA-Z0-9]+)', raw_c)
            if pwd_m:
                pwd = pwd_m.group(1)
            pan_name, cat_key = detect_pan_from_link(link, default_pan)
            results.append({
                "title": title,
                "pan": pan_name,
                "cat_key": cat_key,
                "link": link,
                "pwd": pwd,
                "date": pub_date[:10] if pub_date else "",
                "source": "PanSearch",
                "needs_resolve": False
            })
    else:
        blocks = re.split(r'[\n;]+', raw_c)
        for b in blocks:
            b = b.strip()
            if not b:
                continue
            m_l = re.search(r'href=["\'](https?://[^\s"\']+)["\']', b) or re.search(r'(https?://(?:pan\.|www\.alipan|www\.aliyundrive|drive\.uc|115\.com|www\.123pan|cloud\.189|caiyun\.139)[^\s\n"<>]+)', b)
            if not m_l:
                continue
            link = m_l.group(1)
            if "yiso" in link or "pansearch" in link:
                continue
            t = re.sub(r'<a[^>]+>.*?</a>', '', b)
            t = re.sub(r'https?://[^\s]+', '', t)
            t = re.sub(r'<[^>]+>', '', t)
            t = re.sub(r'^\d+[、.\s]+', '', t)
            t = re.sub(r'[:：\s]+$', '', t).strip()
            if not t:
                t = "未命名资源"
            pwd = ""
            pwd_m = re.search(r'[?&]pwd=([a-zA-Z0-9]+)', link) or re.search(r'(?:密码|提取码)[：:\s]*([a-zA-Z0-9]+)', b)
            if pwd_m:
                pwd = pwd_m.group(1)
            pan_name, cat_key = detect_pan_from_link(link, default_pan)
            results.append({
                "title": t,
                "pan": pan_name,
                "cat_key": cat_key,
                "link": link,
                "pwd": pwd,
                "date": pub_date[:10] if pub_date else "",
                "source": "PanSearch",
                "needs_resolve": False
            })

    return results


async def query_pansearch(keyword: str, category: str = "all") -> List[dict]:
    """向 PanSearch 发起并发高速检索"""
    url = f"https://www.pansearch.me/api/search?keyword={urllib.parse.quote(keyword)}"
    try:
        raw_bytes = await fetch_url(url, timeout=3.5)
        data = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
        raw_items = data.get("data", []) if isinstance(data, dict) else []
        parsed = []
        for it in raw_items:
            sub = parse_pansearch_content(it.get("content", ""), it.get("pan", ""), it.get("time", ""))
            parsed.extend(sub)

        if category and category != "all":
            return [x for x in parsed if x.get("cat_key") == category]
        return parsed
    except Exception as e:
        logger.warning(f"[PanSearch] PanSearch 引擎检索异常: {e}")
        return []


# ==============================================================================
# 3. 爱盼全网网盘聚合引擎 (search.aipan.me / aipan.me)
# ==============================================================================

AIPAN_SERVICE_MAP = {
    "QUARK": ("夸克网盘", "quark"),
    "BAIDU": ("百度网盘", "baidu"),
    "ALIYUN": ("阿里云盘", "ali"),
    "ALI": ("阿里云盘", "ali"),
    "123": ("123网盘", "123"),
    "UC": ("UC网盘", "uc"),
    "TIANYI": ("天翼云盘", "ty189"),
    "XUNLEI": ("迅雷网盘", "xunlei"),
    "115": ("115网盘", "115"),
    "YIDONG": ("移动云盘", "yd139"),
}

AIPAN_ENDPOINTS = [
    ("https://search.aipan.me/api/sources/local", "爱盼精选", 3.0),
    ("https://search.aipan.me/api/sources/pansou", "爱盼盘搜", 3.5),
    ("https://search.aipan.me/api/sources/xiaokupan", "爱盼小库", 3.5),
    ("https://search.aipan.me/api/sources/external-pan", "爱盼外部", 2.0),
]


async def query_aipan_single_source(url: str, name: str, keyword: str, category: str, timeout: float) -> List[dict]:
    """请求爱盼单个聚合源"""
    payload = json.dumps({"name": keyword}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://search.aipan.me",
        "Referer": "https://search.aipan.me/search",
        "Accept": "*/*"
    }
    try:
        raw_bytes = await fetch_url(url, post_data=payload, headers=headers, timeout=timeout)
        data = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
        raw_list = data.get("list", [])
        if not isinstance(raw_list, list):
            return []

        results = []
        for it in raw_list:
            raw_title = it.get("name", "") or it.get("highlightedName", "")
            if not raw_title:
                continue
            clean_title = re.sub(r'<[^>]+>', '', raw_title).strip()
            clean_title = html.unescape(clean_title)

            links_arr = it.get("links", [])
            if not isinstance(links_arr, list):
                continue

            for link_obj in links_arr:
                if not isinstance(link_obj, dict):
                    continue
                raw_link = link_obj.get("link", "").strip()
                if not raw_link or not raw_link.startswith("http"):
                    continue

                service = (link_obj.get("service") or "").upper()
                if service in AIPAN_SERVICE_MAP:
                    pan_name, cat_key = AIPAN_SERVICE_MAP[service]
                else:
                    pan_name, cat_key = detect_pan_from_link(raw_link)

                # 按指定网盘类型精准过滤
                if category and category != "all" and cat_key != category:
                    continue

                pwd = (link_obj.get("pwd") or "").strip()
                if not pwd:
                    pwd_m = re.search(r'[?&]pwd=([a-zA-Z0-9]+)', raw_link) or re.search(r'(?:密码|提取码)[：:\s]*([a-zA-Z0-9]+)', raw_link)
                    if pwd_m:
                        pwd = pwd_m.group(1)

                # 清洗链接中的中文提取码文本参数并去除尾部冗余字符
                clean_link = re.sub(r'[?&](?:提取码|%EF%BC%9A|%E6%8F%90%E5%8F%96%E7%A0%81)[^&#]*', '', raw_link).rstrip('?&#')

                results.append({
                    "title": clean_title,
                    "pan": pan_name,
                    "cat_key": cat_key,
                    "link": clean_link,
                    "pwd": pwd,
                    "date": "",
                    "source": "爱盼",
                    "needs_resolve": False,
                })
        return results
    except Exception as e:
        logger.debug(f"[PanSearch] 爱盼源 [{name}] 检索异常: {e}")
        return []


async def query_aipan(keyword: str, category: str = "all") -> List[dict]:
    """向爱盼 (aipan.me) 各大源发起并发检索与去重"""
    tasks = [
        query_aipan_single_source(url, name, keyword, category, timeout)
        for url, name, timeout in AIPAN_ENDPOINTS
    ]
    groups = await asyncio.gather(*tasks, return_exceptions=True)
    all_items = []
    seen = set()
    for g in groups:
        if isinstance(g, list):
            for it in g:
                l = it.get("link")
                if l and l not in seen:
                    seen.add(l)
                    all_items.append(it)
    return all_items


# ==============================================================================
# 4. 不二 4K 精选专库引擎 (/api2/content + /api2/decrypt)
# ==============================================================================

async def get_buer_curated_library() -> List[dict]:
    """获取并缓存不二 6,800+ 每日 4K/1080P 精选影视专库（全量内存索引）"""
    global _buer_curated_cache, _buer_curated_time
    now = time.time()
    if _buer_curated_cache and (now - _buer_curated_time < 3600):
        return _buer_curated_cache

    for base_url in BUERCHEN_DOMAINS:
        try:
            url = f"{base_url}/api2/content"
            raw_bytes = await fetch_url(url, timeout=6.0)
            data = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
            d = data.get("data", {})
            items = d.get("data", []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
            if items:
                _buer_curated_cache = items
                _buer_curated_time = now
                logger.info(f"[PanSearch] 成功加载不二 4K 精选影视库，共 {len(items)} 部资源")
                return items
        except Exception as e:
            logger.warning(f"[PanSearch] 获取不二 4K 精选库 ({base_url}) 失败: {e}")

    return _buer_curated_cache


async def decrypt_buer_link(base_url: str, enc: str) -> str:
    """通过不二免风控解密接口获取真实网盘直链"""
    if not enc:
        return ""
    if enc in _resolved_link_cache:
        return _resolved_link_cache[enc][0]

    payload = json.dumps({"encryptUrl": enc}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    try:
        raw_bytes = await fetch_url(f"{base_url}/api2/decrypt", post_data=payload, headers=headers, timeout=5.0)
        data = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
        if data.get("success"):
            direct_url = data.get("data", {}).get("url", "")
            if direct_url:
                pwd = ""
                pwd_m = re.search(r"[?&]pwd=([a-zA-Z0-9]+)", direct_url)
                if pwd_m:
                    pwd = pwd_m.group(1)
                _resolved_link_cache[enc] = (direct_url, pwd)
                return direct_url
    except Exception as e:
        logger.debug(f"[PanSearch] 解密 4K 资源链接失败: {e}")
    return ""


async def query_buerchen_curated(keyword: str, category: str = "all") -> List[dict]:
    """检索不二 4K 精选库，并自动瞬时解密真实直链"""
    library = await get_buer_curated_library()
    if not library:
        return []

    tokens = [t.lower() for t in keyword.strip().split() if t]
    if not tokens:
        return []

    matched_items = []
    for it in library:
        title = it.get("title", "")
        title_lower = title.lower()
        if all(tk in title_lower for tk in tokens):
            matched_items.append(it)
        if len(matched_items) >= 10:
            break

    if not matched_items:
        return []

    base_url = BUERCHEN_DOMAINS[0]
    decrypt_tasks = []
    item_slots = []

    for it in matched_items:
        raw_t = it.get("title", "未命名精选")
        clean_t = re.sub(r"^[🔥📅🎬\s]+", "", raw_t).strip()
        display_t = f"{clean_t} [4K/1080P精选]"

        q = it.get("quarkLink")
        b = it.get("baiduLink")
        x = it.get("xunleiLink")

        if q and (category in ["all", "quark"]):
            decrypt_tasks.append(decrypt_buer_link(base_url, q))
            item_slots.append({"title": display_t, "pan": "夸克网盘", "source": "不二4K"})

        if b and (category in ["all", "baidu"]):
            decrypt_tasks.append(decrypt_buer_link(base_url, b))
            item_slots.append({"title": display_t, "pan": "百度网盘", "source": "不二4K"})

        if x and (category in ["all", "xunlei"]):
            decrypt_tasks.append(decrypt_buer_link(base_url, x))
            item_slots.append({"title": display_t, "pan": "迅雷网盘", "source": "不二4K"})

    results = []
    if decrypt_tasks:
        urls = await asyncio.gather(*decrypt_tasks)
        for slot, direct_url in zip(item_slots, urls):
            if direct_url and direct_url.startswith("http"):
                pwd = ""
                pwd_m = re.search(r"[?&]pwd=([a-zA-Z0-9]+)", direct_url)
                if pwd_m:
                    pwd = pwd_m.group(1)
                slot["link"] = direct_url
                slot["pwd"] = pwd
                slot["date"] = "4K精选"
                slot["needs_resolve"] = False
                results.append(slot)

    return results


# ==============================================================================
# 3. 不二全网爬虫搜索引擎 (web_search + save_url)
# ==============================================================================

def sync_search_buerchen_channel(base_url: str, keyword: str, type_id: int, proxy_dict: dict) -> List[dict]:
    """不二单渠道 SSE 爬虫检索"""
    global _buer_challenge_until
    if time.time() < _buer_challenge_until:
        return []

    url = f"{base_url}/api/other/web_search?title={urllib.parse.quote(keyword)}&is_type={type_id}"
    headers = {
        "Accept": "text/event-stream",
        "Referer": f"{base_url}/s/{urllib.parse.quote(keyword)}.html",
    }

    try:
        raw_bytes = sync_fetch_url(url, headers=headers, proxy_dict=proxy_dict, timeout=6.0)
        lines = raw_bytes.decode("utf-8", errors="ignore").splitlines()
        items = []
        for line in lines:
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            raw_data = line[5:].strip()
            if raw_data.startswith("[DONE]"):
                break
            try:
                data = json.loads(raw_data)
                if isinstance(data, dict):
                    if data.get("code") == 42901:
                        logger.warning(f"[PanSearch] 不二网页爬虫触发风控保护 (42901)")
                        _buer_challenge_until = time.time() + 120
                        break
                    if data.get("title") and data.get("url"):
                        raw_t = data["title"]
                        clean_t = re.sub(r"<[^>]+>", "", raw_t).strip()
                        type_num = data.get("is_type", 0)
                        pan_name = "夸克网盘" if type_num == 0 else ("百度网盘" if type_num == 2 else "迅雷网盘")
                        raw_url = data["url"]
                        is_direct = raw_url.startswith("http://") or raw_url.startswith("https://")
                        items.append({
                            "title": clean_t,
                            "pan": pan_name,
                            "url": raw_url,
                            "is_type": type_num,
                            "date": "",
                            "pwd": "",
                            "source": "不二",
                            "needs_resolve": not is_direct,
                            "link": raw_url if is_direct else "",
                            "_base_url": base_url,
                        })
            except Exception:
                pass
        return items
    except Exception as e:
        logger.warning(f"[PanSearch] 不二爬虫渠道 {type_id} 检索出错: {e}")
        return []


async def query_buerchen_web(keyword: str, category: str = "all") -> List[dict]:
    """向不二全网爬虫发起检索"""
    global _buer_challenge_until
    if time.time() < _buer_challenge_until:
        return []

    types_to_query = []
    if category == "all":
        types_to_query = [0, 2, 4]
    else:
        b_type = CATEGORIES.get(category, {}).get("buer_type")
        if b_type is not None:
            types_to_query = [b_type]
        else:
            return []

    proxy_dict = get_proxy_dict(_bot_instance)

    for base_url in BUERCHEN_DOMAINS:
        try:
            tasks = [
                asyncio.to_thread(sync_search_buerchen_channel, base_url, keyword, t_id, proxy_dict)
                for t_id in types_to_query
            ]
            groups = await asyncio.gather(*tasks)
            all_buer = []
            for g in groups:
                all_buer.extend(g)
            if all_buer:
                return all_buer
        except Exception as e:
            logger.warning(f"[PanSearch] 不二爬虫搜索访问 {base_url} 失败: {e}")

    return []


async def resolve_buerchen_item(item: dict) -> Tuple[str, str, str]:
    """
    解析不二资源的真实网盘直链与提取码
    返回: (url, pwd, error_msg)
    """
    global _buer_challenge_until, _expired_links_cache

    if item.get("link") and not item.get("needs_resolve"):
        return item["link"], item.get("pwd", ""), ""

    raw_url = item.get("url", "")
    title = item.get("title", "")
    base_url = item.get("_base_url", BUERCHEN_DOMAINS[0])

    if raw_url in _expired_links_cache:
        return "", "", "分享地址已失效"

    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        pwd = ""
        pwd_m = re.search(r"[?&]pwd=([a-zA-Z0-9]+)", raw_url)
        if pwd_m:
            pwd = pwd_m.group(1)
        item["link"] = raw_url
        item["pwd"] = pwd
        item["needs_resolve"] = False
        _resolved_link_cache[raw_url] = (raw_url, pwd)
        return raw_url, pwd, ""

    if raw_url in _resolved_link_cache:
        cached_u, cached_p = _resolved_link_cache[raw_url]
        item["link"] = cached_u
        item["pwd"] = cached_p
        item["needs_resolve"] = False
        return cached_u, cached_p, ""

    if time.time() < _buer_challenge_until:
        return "", "", "CHALLENGE_REQUIRED"

    # 注意：不二 save_url 接口接收未 quote 的原始路径参数（由 json.dumps 自动转义）
    post_payload = json.dumps({
        "url": raw_url,
        "title": title
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Origin": base_url,
        "Referer": f"{base_url}/s/{urllib.parse.quote(title[:20])}.html"
    }

    try:
        res_bytes = await fetch_url(f"{base_url}/api/other/save_url", post_data=post_payload, headers=headers, timeout=5.0)
        res_json = json.loads(res_bytes.decode("utf-8", errors="ignore"))
        code = res_json.get("code")
        if code == 200:
            direct_url = res_json.get("data", {}).get("url", "")
            pwd = ""
            pwd_m = re.search(r"[?&]pwd=([a-zA-Z0-9]+)", direct_url)
            if pwd_m:
                pwd = pwd_m.group(1)
            item["link"] = direct_url
            item["pwd"] = pwd
            item["needs_resolve"] = False
            _resolved_link_cache[raw_url] = (direct_url, pwd)
            return direct_url, pwd, ""
        elif code == 42901:
            _buer_challenge_until = time.time() + 120
            return "", "", "CHALLENGE_REQUIRED"
        else:
            err_msg = res_json.get("message", "解析失败")
            _expired_links_cache.add(raw_url)
            return "", "", err_msg
    except Exception as e:
        logger.error(f"[PanSearch] 解析不二链接异常: {e}")
        return "", "", str(e)


async def ensure_page_resolved(items: List[dict], subpage: int):
    """
    自动预解析指定子页中未解析的不二资源直链。
    若检测到死链/分享失效，直接将失效条目从 items 中剔除，并自动从后续备选资源中补齐当前页；
    若遇到平台风控限流（42901），则安全停止预解析，保留条目与提取按钮待风控解除。
    """
    global _buer_challenge_until, _expired_links_cache

    # 1. 快速剔除当前列表中已知的死链
    if _expired_links_cache:
        i = 0
        while i < len(items):
            if items[i].get("url") in _expired_links_cache:
                items.pop(i)
            else:
                i += 1

    # 最多循环尝试补位 3 轮，避免极端全是死链时死循环
    for _ in range(3):
        start_idx = (subpage - 1) * SUBPAGE_SIZE
        page_items = items[start_idx : start_idx + SUBPAGE_SIZE]
        if not page_items or time.time() < _buer_challenge_until:
            break

        tasks = []
        target_items = []
        for it in page_items:
            if it.get("needs_resolve") and not it.get("link"):
                raw_url = it.get("url", "")
                if raw_url in _resolved_link_cache:
                    it["link"], it["pwd"] = _resolved_link_cache[raw_url]
                    it["needs_resolve"] = False
                elif raw_url in _expired_links_cache:
                    target_items.append((it, ("EXPIRED",)))
                else:
                    tasks.append(resolve_buerchen_item(it))
                    target_items.append((it, None))

        if not tasks:
            dead_in_page = [it for it, res in target_items if res == ("EXPIRED",)]
            if dead_in_page:
                for dead_it in dead_in_page:
                    if dead_it in items:
                        items.remove(dead_it)
                continue
            break

        results = await asyncio.gather(*tasks, return_exceptions=True)
        task_idx = 0
        dead_found = False

        for it, pre_res in target_items:
            if pre_res == ("EXPIRED",):
                if it in items:
                    items.remove(it)
                dead_found = True
                continue

            res = results[task_idx]
            task_idx += 1

            if isinstance(res, tuple):
                direct_u, pwd, err = res
                if direct_u and direct_u.startswith("http") and "buerchen" not in direct_u:
                    it["link"] = direct_u
                    it["pwd"] = pwd
                    it["needs_resolve"] = False
                elif err == "CHALLENGE_REQUIRED":
                    # 遭遇平台限流风控，停止本轮后续探活
                    break
                elif err and any(kw in err for kw in ("失效", "取消", "不存在", "格式有误", "失败", "删除")):
                    # 确定为源头死链，彻底剔除条目
                    _expired_links_cache.add(it.get("url", ""))
                    if it in items:
                        items.remove(it)
                    dead_found = True

        if not dead_found:
            break


# ==============================================================================
# 5. 多引擎并发聚合与热搜整合
# ==============================================================================

async def query_buerchen(keyword: str, category: str = "all") -> List[dict]:
    """不二搜索聚合检索：精选 4K 专库 + 全网爬虫"""
    curated_task = query_buerchen_curated(keyword, category)
    web_task = query_buerchen_web(keyword, category)

    res_curated, res_web = await asyncio.gather(curated_task, web_task)
    return res_curated + res_web


async def query_all_engines(keyword: str, category: str = "all") -> List[dict]:
    """多引擎异步并发聚合搜索，自动熔断降级与去重合并排序"""
    cache_key = f"{keyword.strip().lower()}:{category}"
    if cache_key in _raw_cache:
        cached_items, ts = _raw_cache[cache_key]
        if time.time() - ts < 600:
            return cached_items

    # 1. 构建全量并发任务（PanSearch、爱盼、不二4K精选、不二爬虫、珈珈搜索自愈探活）
    t_pan = asyncio.create_task(query_pansearch(keyword, category))
    t_aipan = asyncio.create_task(query_aipan(keyword, category))
    t_curated = asyncio.create_task(query_buerchen_curated(keyword, category))
    t_web = asyncio.create_task(query_buerchen_web(keyword, category))
    t_feapi = asyncio.create_task(query_feapi(keyword, category, page=1))

    all_tasks = [t_pan, t_aipan, t_curated, t_web, t_feapi]

    # 2. 统一上限限时并发等待 (6.0s)，超时仅取消未完成慢任务，已完成引擎的数据 100% 完整保留
    done, pending = await asyncio.wait(all_tasks, timeout=6.0)
    if pending:
        logger.warning(f"[PanSearch] {len(pending)} 个引擎未在 6s 内返回，已平滑取消并汇聚已就绪结果")
        for p in pending:
            p.cancel()

    def safe_get_res(t):
        if t in done and not t.cancelled():
            try:
                r = t.result()
                if isinstance(r, list):
                    return r
            except Exception as e:
                logger.debug(f"[PanSearch] 引擎检索异常: {e}")
        return []

    res_pan = safe_get_res(t_pan)
    res_aipan = safe_get_res(t_aipan)
    res_buer_curated = safe_get_res(t_curated)
    res_buer_web = safe_get_res(t_web)
    res_feapi = safe_get_res(t_feapi)

    merged_items = []
    seen_links = set()
    seen_keys = set()

    def add_item(it):
        link = it.get("link") or it.get("url")
        if link and (link in _expired_links_cache or link in seen_links):
            return False
        # 归一化保留季数/集数/漫画等关键标识，杜绝粗暴误杀不同资源
        t_clean = re.sub(r'[\s\-_\(\)（）【】\[\]]', '', it.get("title", "")).lower()
        key = (t_clean, it.get("pan"))
        if key in seen_keys:
            return False
        if link:
            seen_links.add(link)
        seen_keys.add(key)
        merged_items.append(it)
        return True

    # 交叉合并：按 1 条不二4K + 2 条爱盼 + 1 条PanSearch 轮流注入，确保前几页兼顾 4K 精选与全网各大主流网盘直链
    b_idx = 0
    a_idx = 0
    p_idx = 0
    while b_idx < len(res_buer_curated) or a_idx < len(res_aipan) or p_idx < len(res_pan):
        if b_idx < len(res_buer_curated):
            add_item(res_buer_curated[b_idx])
            b_idx += 1
        if a_idx < len(res_aipan):
            add_item(res_aipan[a_idx])
            a_idx += 1
        if p_idx < len(res_pan):
            add_item(res_pan[p_idx])
            p_idx += 1
        if a_idx < len(res_aipan):
            add_item(res_aipan[a_idx])
            a_idx += 1

    # 汇入珈珈搜索（若在线）与不二全网爬虫资源
    for it in res_feapi:
        add_item(it)
    for it in res_buer_web:
        add_item(it)

    logger.info(
        f"[PanSearch] 聚合检索完成「{keyword}」({category}): "
        f"PanSearch={len(res_pan)}条, 爱盼={len(res_aipan)}条, 不二4K={len(res_buer_curated)}条, 不二爬虫={len(res_buer_web)}条, 珈珈={len(res_feapi)}条 "
        f"-> 合并后共 {len(merged_items)} 条"
    )

    _raw_cache[cache_key] = (merged_items, time.time())
    return merged_items


async def get_hot_keywords() -> List[str]:
    """获取热搜词汇与每日精选推荐（带缓存与熔断保护）"""
    global _hot_cache
    cached_keywords, cached_time = _hot_cache
    if cached_keywords and (time.time() - cached_time < 900):
        return cached_keywords

    hot_list = []
    # 1. 优先从不二每日 4K 精选影视提取最热影视词条
    try:
        curated = await get_buer_curated_library()
        for it in curated[:25]:
            t = it.get("title", "")
            t_clean = re.sub(r"[🔥📅🎬\[\]【】（）\(\)]", " ", t).strip()
            w = t_clean.split()[0] if t_clean.split() else ""
            if w and len(w) <= 8 and w not in hot_list:
                hot_list.append(w)
            if len(hot_list) >= 6:
                break
    except Exception as e:
        logger.debug(f"[PanSearch] 抓取不二每日精选推荐失败: {e}")

    # 2. 从 feapi 抓取热搜榜补齐 (若处于熔断状态直接跳过，绝不阻塞)
    if time.time() >= _feapi_broken_until:
        try:
            content_bytes = await fetch_url("https://feapi.xyz/", timeout=2.0)
            content = content_bytes.decode("utf-8", errors="ignore")
            matches = re.findall(r'<a[^>]+class="[^"]*pill[^"]*"[^>]*title="搜索:\s*([^"]+)"[^>]*>', content)
            if not matches:
                raw_matches = re.findall(r'<a[^>]+class="[^"]*hot-pill[^"]*"[^>]*>([\s\S]*?)</a>', content)
                matches = [re.sub(r'<[^>]+>', '', m).strip() for m in raw_matches if m.strip()]
            for m in matches:
                if m not in hot_list and len(m) <= 8:
                    hot_list.append(m)
                if len(hot_list) >= 10:
                    break
        except Exception as e:
            logger.debug(f"[PanSearch] 抓取珈珈热搜失败: {e}")

    final_keywords = hot_list[:10] if hot_list else ["庆余年", "一人之下", "黑神话悟空", "凡人修仙传", "三体", "大明王朝1566", "柯南", "Python"]
    _hot_cache = (final_keywords, time.time())
    return final_keywords


# ==============================================================================
# 5. 消息排版与键盘生成
# ==============================================================================

def parse_search_input(text: str) -> Tuple[str, str]:
    """解析指令参数，如 `/s 庆余年 夸克`"""
    parts = text.strip().split()
    if not parts:
        return "", "all"
    if len(parts) >= 2:
        last = parts[-1].lower()
        if last in CATEGORY_ALIASES:
            cat = CATEGORY_ALIASES[last]
            kw = " ".join(parts[:-1]).strip()
            return kw, cat
    return " ".join(parts).strip(), "all"


def format_results_message(keyword: str, category: str, items: List[dict], subpage: int, total_subpages: int, total_count: int) -> str:
    """排版输出美观的 Telegram HTML 搜索结果"""
    cat_info = CATEGORIES.get(category, CATEGORIES["all"])
    lines = [
        f"🔍 <b>网盘资源聚合搜索：</b><code>{html.escape(keyword)}</code>",
        f"🏷️ <b>筛选渠道：</b>{cat_info['icon']} {cat_info['name']}",
        f"📊 <b>检索结果：</b>共聚合 {total_count} 条资源（第 {subpage}/{total_subpages} 页）",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    start_idx = (subpage - 1) * SUBPAGE_SIZE
    page_items = items[start_idx : start_idx + SUBPAGE_SIZE]

    if not page_items:
        lines.append("\n<i>⚠️ 该渠道下暂未找到可用资源，可点击下方「切换渠道」查看其他网盘。</i>\n")
    else:
        for idx, it in enumerate(page_items, start=start_idx + 1):
            pan_name = it.get("pan", "网盘")
            source_tag = it.get("source", "聚合")
            pan_icon = "📁"
            for k, v in CATEGORIES.items():
                short_name = v["name"].replace("网盘", "").replace("云盘", "")
                if k in pan_name or short_name in pan_name:
                    pan_icon = v["icon"]
                    break

            safe_title = html.escape(it.get("title", "未知资源"))
            link = it.get("link", "")
            date = it.get("date", "")
            pwd = it.get("pwd", "")

            item_text = f"<b>{idx}. {pan_icon} [{html.escape(pan_name)} · {source_tag}]</b>\n"

            if link and "buerchen" not in link:
                item_text += f"🔗 <a href=\"{html.escape(link)}\"><b>{safe_title}</b></a>\n"
            else:
                item_text += f"📌 <b>{safe_title}</b>\n<i>(⚡ 点击下方操作按钮提取直链)</i>\n"

            meta_parts = []
            if date:
                meta_parts.append(f"📅 {date}")
            if pwd:
                meta_parts.append(f"🔑 提取码: <code>{html.escape(pwd)}</code>")
            if meta_parts:
                item_text += " · ".join(meta_parts) + "\n"

            lines.append(item_text)

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 <i>多引擎并发聚合 (PanSearch + 爱盼 + 不二4K + 容灾) · 点击蓝色标题直接打开网盘页面转存</i>")
    return "\n".join(lines)


def make_pagination_keyboard(sid: str, subpage: int, total_subpages: int, page_items: List[dict] = None) -> InlineKeyboardMarkup:
    """生成内联分页与功能操作键盘"""
    keyboard = []

    # 1. 为本页中待解析的不二爬虫资源生成单条提取按钮
    if page_items:
        start_idx = (subpage - 1) * SUBPAGE_SIZE
        resolve_btns = []
        for idx, it in enumerate(page_items, start=start_idx + 1):
            if it.get("needs_resolve") and not it.get("link"):
                resolve_btns.append(InlineKeyboardButton(f"⚡ 提取 #{idx} 直链", callback_data=f"p:get:{sid}:{idx - 1}"))
                if len(resolve_btns) == 2:
                    keyboard.append(resolve_btns)
                    resolve_btns = []
        if resolve_btns:
            keyboard.append(resolve_btns)

    # 2. 导航翻页栏
    nav_row = []
    if subpage > 1:
        nav_row.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"p:pg:{sid}:{subpage - 1}"))
    else:
        nav_row.append(InlineKeyboardButton("⏮️ 首页", callback_data="p:noop"))

    nav_row.append(InlineKeyboardButton(f"📄 {subpage}/{total_subpages}", callback_data="p:noop"))

    if subpage < total_subpages:
        nav_row.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"p:pg:{sid}:{subpage + 1}"))
    else:
        nav_row.append(InlineKeyboardButton("⏭️ 末页", callback_data="p:noop"))

    keyboard.append(nav_row)

    # 3. 功能操作栏
    keyboard.append([
        InlineKeyboardButton("🗂️ 切换网盘渠道", callback_data=f"p:flt:{sid}"),
        InlineKeyboardButton("🔄 刷新当前页", callback_data=f"p:pg:{sid}:{subpage}"),
    ])
    return InlineKeyboardMarkup(keyboard)


def make_filter_keyboard(sid: str, current_cat: str) -> InlineKeyboardMarkup:
    """生成网盘渠道切换键盘"""
    rows = []
    current_row = []
    for cat_key, cat_val in CATEGORIES.items():
        prefix = "✅ " if cat_key == current_cat else f"{cat_val['icon']} "
        btn_text = f"{prefix}{cat_val['name']}"
        current_row.append(InlineKeyboardButton(btn_text, callback_data=f"p:cat:{sid}:{cat_key}"))
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    rows.append([
        InlineKeyboardButton("🔙 返回结果列表", callback_data=f"p:back:{sid}")
    ])
    return InlineKeyboardMarkup(rows)


def make_hot_keyboard(hot_words: List[str]) -> InlineKeyboardMarkup:
    """生成热搜与推荐词快捷点击键盘"""
    rows = []
    current_row = []
    for idx, kw in enumerate(hot_words[:8]):
        current_row.append(InlineKeyboardButton(f"🔥 {kw}", callback_data=f"p:hot:{idx}"))
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    rows.append([
        InlineKeyboardButton("🔄 刷新热门榜单", callback_data="p:rf_hot")
    ])
    return InlineKeyboardMarkup(rows)


def get_pan_sessions() -> Dict[str, dict]:
    """获取全局持久化会话字典（存放在 bot 实例中，跨 /reload 不丢失）"""
    global _sessions, _bot_instance
    if _bot_instance:
        if not hasattr(_bot_instance, "_pan_search_sessions"):
            _bot_instance._pan_search_sessions = {}
        return _bot_instance._pan_search_sessions
    return _sessions


def clean_expired_sessions():
    """定期清理 30 分钟前的历史搜索会话"""
    sessions = get_pan_sessions()
    now = time.time()
    expired = [k for k, v in sessions.items() if now - v.get("ts", now) > 1800]
    for k in expired:
        sessions.pop(k, None)


# ==============================================================================
# 6. 指令与回调交互逻辑
# ==============================================================================

async def handle_search_cmd(client: pyrogram.Client, message: Message):
    """处理 /s 与 /search 搜索指令"""
    clean_expired_sessions()
    raw_text = message.text or ""
    cmd_parts = raw_text.strip().split(maxsplit=1)
    args_str = cmd_parts[1].strip() if len(cmd_parts) > 1 else ""

    # 1. 空关键词：展示使用指引与实时热搜推荐
    if not args_str:
        hot_words = await get_hot_keywords()
        guide_text = (
            "🔍 <b>网盘资源聚合搜索 (多引擎并发版)</b>\n\n"
            "<b>💡 使用说明：</b>\n"
            "• 发送 <code>/s 关键词</code> 进行全网多引擎聚合搜索\n"
            "• 发送 <code>/s 关键词 网盘名</code> 快速筛选指定渠道\n"
            "  <i>示例：<code>/s 庆余年 夸克</code>、<code>/s 大明风华 百度</code>、<code>/s 黑神话 迅雷</code></i>\n\n"
            "🔥 <b>当前热门影视与搜索榜：</b>\n"
            "<i>点击下方热门词条直接发起搜索 ⬇️</i>"
        )
        await message.reply_text(
            guide_text,
            parse_mode=ParseMode.HTML,
            reply_markup=make_hot_keyboard(hot_words),
            disable_web_page_preview=True
        )
        return

    # 2. 带有关键词：发起多引擎并发检索
    keyword, category = parse_search_input(args_str)
    cat_info = CATEGORIES.get(category, CATEGORIES["all"])
    status_msg = await message.reply_text(
        f"🔎 正在全网检索【{cat_info['name']}】<code>{html.escape(keyword)}</code>，请稍候...",
        parse_mode=ParseMode.HTML
    )

    try:
        items = await query_all_engines(keyword, category)
        await ensure_page_resolved(items, 1)

        sid = hashlib.md5(f"pan:{keyword}:{category}:{time.time()}".encode()).hexdigest()[:8]

        sessions = get_pan_sessions()
        sessions[sid] = {
            "keyword": keyword,
            "category": category,
            "items": items,
            "ts": time.time(),
        }

        total_count = len(items)
        total_subpages = max(1, (total_count + SUBPAGE_SIZE - 1) // SUBPAGE_SIZE)
        res_text = format_results_message(keyword, category, items, 1, total_subpages, total_count)
        page_items = items[:SUBPAGE_SIZE]
        keyboard = make_pagination_keyboard(sid, 1, total_subpages, page_items)

        await status_msg.edit_text(
            res_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"[PanSearch] 检索发生异常: {e}")
        await status_msg.edit_text(
            f"❌ 检索失败: {html.escape(str(e))}\n请稍后重试。",
            parse_mode=ParseMode.HTML
        )


async def handle_pan_callback(client: pyrogram.Client, callback_query: CallbackQuery):
    """处理网盘搜索界面的所有内联交互"""
    data = callback_query.data or ""
    user_id = callback_query.from_user.id if callback_query.from_user else 0
    logger.info(f"[PanSearch] 🔔 收到按钮交互回调: data='{data}', user_id={user_id}")

    # 白名单安全校验
    if _bot_instance and _bot_instance.allowed_user_ids:
        allowed = set()
        for x in _bot_instance.allowed_user_ids:
            try:
                allowed.add(int(x))
            except Exception:
                pass
            allowed.add(str(x))
        if user_id not in allowed and str(user_id) not in allowed:
            logger.warning(f"[PanSearch] 用户 {user_id} 未在白名单中: {allowed}")
            await callback_query.answer("⚠️ 您没有操作此机器人的权限", show_alert=True)
            return

    if data == "p:noop":
        await callback_query.answer()
        return

    # 1. 刷新热搜榜单
    if data == "p:rf_hot":
        global _hot_cache
        _hot_cache = ([], 0.0)
        hot_words = await get_hot_keywords()
        try:
            await callback_query.edit_message_reply_markup(reply_markup=make_hot_keyboard(hot_words))
            await callback_query.answer("🔥 热搜榜单已刷新")
        except Exception:
            await callback_query.answer()
        return

    # 2. 点击热搜词直接搜索
    if data.startswith("p:hot:"):
        idx_str = data.split(":", 2)[2]
        try:
            idx = int(idx_str)
            hot_words = await get_hot_keywords()
            if 0 <= idx < len(hot_words):
                target_kw = hot_words[idx]
                await callback_query.answer(f"🔍 正在搜索：{target_kw}")
                await callback_query.edit_message_text(
                    f"🔎 正在全网检索 <code>{html.escape(target_kw)}</code>，请稍候...",
                    parse_mode=ParseMode.HTML
                )
                items = await query_all_engines(target_kw, "all")
                await ensure_page_resolved(items, 1)

                sid = hashlib.md5(f"pan:{target_kw}:all:{time.time()}".encode()).hexdigest()[:8]

                sessions = get_pan_sessions()
                sessions[sid] = {
                    "keyword": target_kw,
                    "category": "all",
                    "items": items,
                    "ts": time.time(),
                }

                total_count = len(items)
                total_subpages = max(1, (total_count + SUBPAGE_SIZE - 1) // SUBPAGE_SIZE)
                res_text = format_results_message(target_kw, "all", items, 1, total_subpages, total_count)
                page_items = items[:SUBPAGE_SIZE]
                keyboard = make_pagination_keyboard(sid, 1, total_subpages, page_items)

                await callback_query.edit_message_text(
                    res_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
                return
        except Exception as e:
            logger.error(f"[PanSearch] 点击热搜出错: {e}")
            await callback_query.answer("⚠️ 检索失败，请重试", show_alert=True)
            return

    # 提取会话与操作类型
    parts = data.split(":")
    if len(parts) < 3:
        await callback_query.answer()
        return

    action = parts[1]
    sid = parts[2]
    sessions = get_pan_sessions()
    session = sessions.get(sid)

    if not session:
        # 尝试从历史消息卡片中自动提取关键词和分类重新恢复会话
        msg_text = (callback_query.message.text or "") if callback_query.message else ""
        clean_text = re.sub(r'<[^>]+>', '', msg_text)
        kw_m = re.search(r'网盘资源聚合搜索[：:\s]*([^\n]+)', clean_text)
        if kw_m:
            keyword = kw_m.group(1).strip()
            cat = "all"
            for k, v in CATEGORIES.items():
                if v["name"] in clean_text:
                    cat = k
                    break

            try:
                logger.info(f"[PanSearch] 正在为历史或重载后的会话 {sid} 自动恢复「{keyword}」({cat})...")
                items = await query_all_engines(keyword, cat)
                session = {
                    "keyword": keyword,
                    "category": cat,
                    "items": items,
                    "ts": time.time()
                }
                sessions[sid] = session
            except Exception as e:
                logger.error(f"[PanSearch] 自动恢复会话失败: {e}")

    if not session:
        await callback_query.answer("⚠️ 搜索会话已过期，请重新发送 /s 指令进行搜索", show_alert=True)
        return

    # 3. 翻页操作 (p:pg:{sid}:{page})
    if action == "pg":
        target_page = int(parts[3])
        items = session["items"]

        logger.info(f"[PanSearch] 用户 {user_id} 翻页请求 -> 第 {target_page} 页 (sid={sid})")
        await callback_query.answer(f"正在加载第 {target_page} 页...")
        await ensure_page_resolved(items, target_page)

        total_count = len(items)
        total_subpages = max(1, (total_count + SUBPAGE_SIZE - 1) // SUBPAGE_SIZE)
        target_page = max(1, min(target_page, total_subpages))

        start_idx = (target_page - 1) * SUBPAGE_SIZE
        page_items = items[start_idx : start_idx + SUBPAGE_SIZE]

        res_text = format_results_message(
            session["keyword"],
            session["category"],
            items,
            target_page,
            total_subpages,
            total_count
        )
        keyboard = make_pagination_keyboard(sid, target_page, total_subpages, page_items)

        try:
            await callback_query.edit_message_text(
                res_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        except pyrogram.errors.MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"[PanSearch] 翻页渲染异常: {e}")
            try:
                await callback_query.answer(f"⚠️ 翻页失败: {e}", show_alert=True)
            except Exception:
                pass
        return

    # 4. 单条资源一键解析提取 (p:get:{sid}:{idx})
    if action == "get":
        item_idx = int(parts[3])
        items = session["items"]
        if not (0 <= item_idx < len(items)):
            await callback_query.answer("⚠️ 资源编号无效", show_alert=True)
            return

        target_item = items[item_idx]
        title = target_item.get("title", "网盘资源")
        pan_name = target_item.get("pan", "网盘")

        await callback_query.answer(f"⚡ 正在解析第 {item_idx + 1} 条资源链接...")

        direct_url, pwd, err = await resolve_buerchen_item(target_item)

        if direct_url:
            target_item["link"] = direct_url
            target_item["pwd"] = pwd
            target_item["needs_resolve"] = False

            # 同步实时刷新主列表消息：将标题变为可点击蓝色直链，并移除该条目的提取按钮
            total_count = len(items)
            total_subpages = max(1, (total_count + SUBPAGE_SIZE - 1) // SUBPAGE_SIZE)
            current_subpage = max(1, min((item_idx // SUBPAGE_SIZE) + 1, total_subpages))
            res_text = format_results_message(
                session["keyword"],
                session["category"],
                items,
                current_subpage,
                total_subpages,
                total_count
            )
            page_items = items[(current_subpage - 1) * SUBPAGE_SIZE : current_subpage * SUBPAGE_SIZE]
            keyboard = make_pagination_keyboard(sid, current_subpage, total_subpages, page_items)
            try:
                await callback_query.edit_message_text(
                    res_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
            except Exception:
                pass

            # 发送独立转存卡片
            msg_lines = [
                f"✅ <b>{html.escape(pan_name)}资源提取成功！</b>",
                f"━━━━━━━━━━━━━━━━━━━━",
                f"🎬 <b>资源名称：</b>{html.escape(title)}",
                f"📁 <b>网盘平台：</b>{html.escape(pan_name)}",
                f"🔗 <b>资源链接：</b><code>{html.escape(direct_url)}</code>",
            ]
            if pwd:
                msg_lines.append(f"🔑 <b>提取密码：</b><code>{html.escape(pwd)}</code>")
            msg_lines.append("━━━━━━━━━━━━━━━━━━━━")
            msg_lines.append("💡 <i>提示：主列表已更新为直接可点链接，或点击下方按钮前往转存</i>")

            btn_row = [
                InlineKeyboardButton("🚀 前往网盘页面转存", url=direct_url)
            ]
            await callback_query.message.reply_text(
                "\n".join(msg_lines),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([btn_row]),
                disable_web_page_preview=True
            )
        else:
            if err == "CHALLENGE_REQUIRED":
                await callback_query.answer("⚠️ 不二接口当前频次风控保护中（每小时限流），请稍后重试", show_alert=True)
            else:
                # 真实失效：彻底从列表中剔除并自动补位刷新
                raw_u = target_item.get("url", "")
                if raw_u:
                    _expired_links_cache.add(raw_u)
                if target_item in items:
                    items.remove(target_item)

                total_count = len(items)
                total_subpages = max(1, (total_count + SUBPAGE_SIZE - 1) // SUBPAGE_SIZE)
                current_subpage = max(1, min((item_idx // SUBPAGE_SIZE) + 1, total_subpages))

                # 自动为当前页向前补位并探活
                await ensure_page_resolved(items, current_subpage)

                total_count = len(items)
                total_subpages = max(1, (total_count + SUBPAGE_SIZE - 1) // SUBPAGE_SIZE)
                current_subpage = max(1, min(current_subpage, total_subpages))

                res_text = format_results_message(
                    session["keyword"],
                    session["category"],
                    items,
                    current_subpage,
                    total_subpages,
                    total_count
                )
                page_items = items[(current_subpage - 1) * SUBPAGE_SIZE : current_subpage * SUBPAGE_SIZE]
                keyboard = make_pagination_keyboard(sid, current_subpage, total_subpages, page_items)

                try:
                    await callback_query.edit_message_text(
                        res_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                        disable_web_page_preview=True
                    )
                except Exception:
                    pass

                # 弹窗提示，不单独向群里发大段警告消息
                await callback_query.answer("⚠️ 该网盘资源在源头已失效，已自动从列表中移除", show_alert=True)
        return

    # 5. 显示网盘筛选菜单 (p:flt:{sid})
    if action == "flt":
        filter_text = (
            f"🗂️ <b>网盘渠道分类筛选</b>\n\n"
            f"当前搜索：<code>{html.escape(session['keyword'])}</code>\n"
            f"当前渠道：<b>{CATEGORIES.get(session['category'], {}).get('name', '全部')}</b>\n\n"
            f"请点击下方按钮切换查看特定网盘的搜索结果："
        )
        await callback_query.edit_message_text(
            filter_text,
            parse_mode=ParseMode.HTML,
            reply_markup=make_filter_keyboard(sid, session["category"])
        )
        await callback_query.answer()
        return

    # 6. 切换网盘分类 (p:cat:{sid}:{cat})
    if action == "cat":
        new_cat = parts[3]
        if new_cat == session["category"]:
            await callback_query.answer("当前已是该分类")
            return

        cat_name = CATEGORIES.get(new_cat, {}).get("name", "全部")
        await callback_query.answer(f"已切换为：{cat_name}")
        await callback_query.edit_message_text(
            f"🔎 正在重新检索【{cat_name}】渠道资源...",
            parse_mode=ParseMode.HTML
        )

        try:
            items = await query_all_engines(session["keyword"], new_cat)
            await ensure_page_resolved(items, 1)

            session["category"] = new_cat
            session["items"] = items

            total_count = len(items)
            total_subpages = max(1, (total_count + SUBPAGE_SIZE - 1) // SUBPAGE_SIZE)
            res_text = format_results_message(
                session["keyword"],
                new_cat,
                items,
                1,
                total_subpages,
                total_count
            )
            page_items = items[:SUBPAGE_SIZE]
            keyboard = make_pagination_keyboard(sid, 1, total_subpages, page_items)

            await callback_query.edit_message_text(
                res_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"[PanSearch] 切换分类检索失败: {e}")
            await callback_query.edit_message_text(
                f"❌ 切换失败: {html.escape(str(e))}",
                parse_mode=ParseMode.HTML
            )
        return

    # 7. 从筛选菜单返回结果列表 (p:back:{sid})
    if action == "back":
        items = session["items"]
        total_count = len(items)
        total_subpages = max(1, (total_count + SUBPAGE_SIZE - 1) // SUBPAGE_SIZE)
        await ensure_page_resolved(items, 1)

        res_text = format_results_message(
            session["keyword"],
            session["category"],
            items,
            1,
            total_subpages,
            total_count
        )
        page_items = items[:SUBPAGE_SIZE]
        keyboard = make_pagination_keyboard(sid, 1, total_subpages, page_items)
        await callback_query.edit_message_text(
            res_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        await callback_query.answer()
        return


def register(bot_instance):
    """注册网盘搜索插件至 DownloadBot"""
    global _bot_instance
    _bot_instance = bot_instance
    handlers = []
    whitelist = bot_instance.allowed_user_ids

    # 1. 动态修复内存中 bot.py 原生 on_query_handler 吞噬回调的底层缺陷（无需重启容器即刻生效）
    if hasattr(bot_instance.bot, "dispatcher") and hasattr(bot_instance.bot.dispatcher, "groups"):
        dp = bot_instance.bot.dispatcher
        for grp_id, h_list in list(dp.groups.items()):
            for h in list(h_list):
                cb = getattr(h, "callback", None)
                cb_name = getattr(cb, "__name__", "")
                if cb_name == "on_query_handler" and not getattr(cb, "_patched_continue", False):
                    orig_cb = cb
                    async def _patched_on_query(c, q, _orig=orig_cb):
                        try:
                            await _orig(c, q)
                        except pyrogram.ContinuePropagation:
                            raise
                        except Exception:
                            pass
                        raise pyrogram.ContinuePropagation
                    _patched_on_query._patched_continue = True
                    h.callback = _patched_on_query
                    logger.info("[PanSearch] 成功动态修复 bot.py 的 on_query_handler 回调向后穿透！")

    # 2. 注册 /s 与 /search 命令监听器
    search_filter = pyrogram.filters.command(["s", "search", "pan"]) & pyrogram.filters.user(whitelist)
    h_cmd = MessageHandler(handle_search_cmd, filters=search_filter)
    bot_instance.bot.add_handler(h_cmd)
    handlers.append(h_cmd)

    # 3. 注册内联按钮交互回调监听器
    cb_filter = pyrogram.filters.regex(r"^p:")
    h_cb = CallbackQueryHandler(handle_pan_callback, filters=cb_filter)
    bot_instance.bot.add_handler(h_cb)
    # 关键机制：将插件的回调监听器置顶到 group 0 首位，彻底免疫被其他全局回调吞噬
    if hasattr(bot_instance.bot, "dispatcher") and hasattr(bot_instance.bot.dispatcher, "groups"):
        g0 = bot_instance.bot.dispatcher.groups.get(0, [])
        if h_cb in g0:
            g0.remove(h_cb)
            g0.insert(0, h_cb)
        logger.info(f"[PanSearch] 回调监听器已前置到 group 0 首位 (当前 group 0 共有 {len(g0)} 个处理器)")
    handlers.append(h_cb)

    logger.info(f"Plugin [{PLUGIN_NAME}] v{PLUGIN_VERSION} registered successfully with {len(handlers)} handlers.")
    return handlers
