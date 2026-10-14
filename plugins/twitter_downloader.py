import os
import re
import time
import json
import html
import urllib.parse
import asyncio
import threading
import secrets
import requests
from loguru import logger
import pyrogram
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PLUGIN_NAME = "Twitter/X 视频下载器"
PLUGIN_DESC = "识别推特链接，下载最高画质视频并显示实时下载进度条"
PLUGIN_VERSION = "2.1"

_active_twitter_tasks = {}

def format_byte(b: float) -> str:
    """Formats bytes into human readable string."""
    if b < 1024:
        return f"{b:.1f} B"
    elif b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    elif b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    else:
        return f"{b / (1024 * 1024 * 1024):.2f} GB"

def create_progress_bar(progress: int, total_bars: int = 10) -> str:
    """Generates visual progress bar: [██████░░░░]"""
    completed_bars = int(progress * total_bars / 100)
    remaining_bars = total_bars - completed_bars
    return "█" * completed_bars + "░" * remaining_bars

def parse_resolution_score(res_str: str) -> int:
    res_str = str(res_str).lower()
    m = re.search(r'(\d{3,4})p', res_str)
    if m:
        return int(m.group(1))
    m2 = re.search(r'(\d+)x(\d+)', res_str)
    if m2:
        return min(int(m2.group(1)), int(m2.group(2)))
    if 'hd' in res_str or 'high' in res_str:
        return 720
    if 'sd' in res_str or 'medium' in res_str:
        return 480
    return 0

def extract_from_snapvid(url: str, proxies: dict = None) -> dict:
    api_url = 'https://snapvid.net/api/ajaxSearch'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://snapvid.net/zh-cn1/twitter-downloader',
        'Origin': 'https://snapvid.net',
        'X-Requested-With': 'XMLHttpRequest'
    }
    data = {'q': url, 'vt': 'twitter'}

    res_json = None
    try:
        r = requests.post(api_url, data=data, headers=headers, proxies=proxies or {}, timeout=12)
        if r.status_code == 200:
            res_json = r.json()
    except Exception as e:
        logger.debug(f"Snapvid request failed: {type(e).__name__}")

    if not res_json or res_json.get('status') != 'ok':
        return None

    html = res_json.get('data', '')
    if not html:
        return None

    candidates = []
    a_tags = re.findall(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE)
    for dl_url, label in a_tags:
        clean_url = dl_url.replace('&amp;', '&')
        if not clean_url.startswith('http') or 'javascript' in clean_url:
            continue
        label_text = re.sub(r'<[^>]+>', '', label).strip()
        score = parse_resolution_score(label_text)
        if score > 0 or 'download' in label_text.lower() or 'mp4' in clean_url:
            res_name = f"{score}p" if score > 0 else label_text
            candidates.append({
                'url': clean_url,
                'score': score,
                'resolution': res_name,
                'is_direct': True
            })

    if not candidates:
        raw_links = re.findall(r'href=["\'](https?://[^"\']+(?:\.mp4|token=|dl\.snapvid)[^"\']*)["\']', html)
        for rl in set(raw_links):
            clean_url = rl.replace('&amp;', '&')
            candidates.append({
                'url': clean_url,
                'score': 720,
                'resolution': 'HD',
                'is_direct': True
            })

    if candidates:
        candidates.sort(key=lambda x: x['score'], reverse=True)
        title_match = re.search(r'<div class=["\']content["\'][^>]*>.*?<h3>(.*?)</h3>', html, re.DOTALL)
        title = title_match.group(1).strip() if title_match else 'Twitter_Video'
        return {
            'success': True,
            'title': title[:80],
            'author': 'Twitter User',
            'screen_name': 'twitter',
            'candidates': candidates,
            'resolution': candidates[0]['resolution'],
            'source': 'snapvid'
        }

    return None

def extract_from_download_x_video(url: str, proxies: dict = None) -> dict:
    """Extracts video stream information from download-x-video.com using proxy and Seroval RPC."""
    m = re.search(r'status/(\d+)', url)
    if not m:
        return None
    tweet_id = m.group(1)
    norm_url = f"https://x.com/i/status/{tweet_id}"

    api_endpoint = "https://download-x-video.com/_serverFn/a04c9b1be74937995b6f0fb81ca5502a27a85a4248d3b7c3a6a17d42c04d317d"

    payload_obj = {
        "t": {
            "t": 10,
            "i": 0,
            "p": {
                "k": ["data"],
                "v": [
                    {
                        "t": 10,
                        "i": 1,
                        "p": {
                            "k": ["url"],
                            "v": [{"t": 1, "s": norm_url}]
                        },
                        "o": 0
                    }
                ]
            },
            "o": 0
        },
        "f": 63,
        "m": []
    }

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Content-Type': 'application/json',
        'x-tsr-serverFn': 'true',
        'Accept': 'application/json',
        'Origin': 'https://download-x-video.com',
        'Referer': 'https://download-x-video.com/'
    }

    try:
        logger.info(f"Attempting Twitter resolution via download-x-video.com: {norm_url}")
        r = requests.post(api_endpoint, json=payload_obj, headers=headers, proxies=proxies, timeout=15)
        if r.status_code != 200:
            logger.warning(f"download-x-video.com returned HTTP status {r.status_code}")
            return None

        resp_json = r.json()

        # Recursively collect video items
        raw_entries = []
        def walk(obj):
            if isinstance(obj, dict):
                if "p" in obj and isinstance(obj["p"], dict):
                    p = obj["p"]
                    keys = p.get("k", [])
                    vals = p.get("v", [])
                    if "url" in keys:
                        entry = {}
                        for k, v in zip(keys, vals):
                            if isinstance(v, dict) and "s" in v:
                                entry[k] = v["s"]
                            else:
                                entry[k] = v
                        raw_entries.append(entry)
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        walk(resp_json)

        candidates = []
        for entry in raw_entries:
            dl_url = entry.get("url", "")
            if not dl_url:
                continue
            bitrate = entry.get("bitrate", 0)
            try:
                bitrate = int(bitrate)
            except Exception:
                bitrate = 0

            # Extract resolution if present in URL, e.g. /720x1280/ or /480x852/
            m_res = re.search(r'/(\d+)x(\d+)/', dl_url)
            if m_res:
                w, h = int(m_res.group(1)), int(m_res.group(2))
                res_label = f"{w}x{h}"
                score = w * h
            else:
                res_label = f"Bitrate {bitrate}" if bitrate else entry.get("quality", "最高画质")
                score = bitrate or 999999

            candidates.append({
                'url': dl_url,
                'score': score,
                'resolution': res_label,
                'is_direct': False
            })

        if candidates:
            # Sort descending: highest resolution/bitrate first!
            candidates.sort(key=lambda x: x['score'], reverse=True)
            return {
                'success': True,
                'title': f"x_video_{tweet_id}",
                'author': 'Twitter User',
                'screen_name': 'twitter',
                'tweet_id': tweet_id,
                'candidates': candidates,
                'resolution': candidates[0]['resolution'],
                'source': 'download-x-video.com'
            }
    except Exception as e:
        logger.warning(f"download-x-video.com extraction failed: {e}")

    return None

FLARESOLVERR_SESSION_ID = "snapvid_session"
_TWITTER_CACHE = {} # {tweet_id: (result_dict, expire_timestamp)}
CACHE_TTL_SECONDS = 1800 # 30 minutes

def get_cached_twitter_result(tweet_id: str):
    """Retrieves valid cached resolution result if available."""
    now = time.time()
    if tweet_id in _TWITTER_CACHE:
        data, expire_ts = _TWITTER_CACHE[tweet_id]
        if now < expire_ts:
            logger.info(f"Hit memory resolution cache for tweet {tweet_id} (0ms fast return)")
            return data
        else:
            del _TWITTER_CACHE[tweet_id]
    return None

def set_cached_twitter_result(tweet_id: str, data: dict):
    """Stores resolution result into local memory cache."""
    if tweet_id and data and data.get('success'):
        _TWITTER_CACHE[tweet_id] = (data, time.time() + CACHE_TTL_SECONDS)

def ensure_flaresolverr_session(endpoint: str, session_id: str, proxies: dict = None) -> bool:
    """Maintains a persistent, warm headless browser session in FlareSolverr with cached Cloudflare cookies."""
    try:
        r = requests.post(endpoint, json={"cmd": "sessions.list"}, timeout=5).json()
        if session_id in r.get("sessions", []):
            return True
        payload = {"cmd": "sessions.create", "session": session_id}
        if proxies:
            payload["proxy"] = {"url": proxies.get("https") or proxies["http"]}
        r_create = requests.post(endpoint, json=payload, timeout=15).json()
        return r_create.get("status") == "ok"
    except Exception as e:
        logger.debug(f"FlareSolverr session management note: {e}")
        return False

FLARESOLVERR_URL = "http://flaresolverr:8191"

def extract_from_snapvid_flaresolverr(url: str, flaresolverr_host: str = None, proxies: dict = None) -> dict:
    """Uses FlareSolverr headless browser with warm Session & Cookie cache to solve Cloudflare Turnstile."""
    if not flaresolverr_host:
        flaresolverr_host = FLARESOLVERR_URL
    endpoint = f"{flaresolverr_host.rstrip('/')}/v1"
    
    # Ensure warm session exists (Chromium instance & Cloudflare cookies kept alive)
    session_id = FLARESOLVERR_SESSION_ID + ("_nas_proxy" if proxies else "")
    has_session = ensure_flaresolverr_session(endpoint, session_id, proxies)
    
    payload = {
        "cmd": "request.post",
        "url": "https://snapvid.net/api/ajaxSearch",
        "postData": f"q={urllib.parse.quote(url)}&vt=twitter",
        "maxTimeout": 25000
    }
    if proxies:
        payload["proxy"] = {"url": proxies.get("https") or proxies["http"]}
    if has_session:
        payload["session"] = session_id

    try:
        logger.info(f"Attempting Twitter resolution via FlareSolverr (方案 A, session_cache={has_session}): {url}")
        r = requests.post(endpoint, json=payload, timeout=30)
        if r.status_code != 200:
            return None

        data = r.json()
        if data.get('status') != 'ok':
            # If session was corrupted, destroy it so it regenerates fresh next time
            if has_session:
                try:
                    requests.post(endpoint, json={"cmd": "sessions.destroy", "session": session_id}, timeout=5)
                except Exception:
                    pass
            return None

        resp_text = data.get('solution', {}).get('response', '')
        m_pre = re.search(r'<pre>(.*?)</pre>', resp_text, re.DOTALL)
        raw_json_str = m_pre.group(1) if m_pre else resp_text
        raw_json_str = html.unescape(raw_json_str)

        parsed = None
        try:
            parsed = json.loads(raw_json_str)
        except Exception as e_json:
            logger.warning(f"FlareSolverr returned non-JSON ({e_json}). Resetting session and retrying once...")
            if has_session:
                try:
                    requests.post(endpoint, json={"cmd": "sessions.destroy", "session": session_id}, timeout=5)
                except Exception:
                    pass
            payload.pop("session", None)
            try:
                r2 = requests.post(endpoint, json=payload, timeout=30)
                if r2.status_code == 200:
                    d2 = r2.json()
                    resp_text2 = d2.get('solution', {}).get('response', '')
                    m_pre2 = re.search(r'<pre>(.*?)</pre>', resp_text2, re.DOTALL)
                    raw2 = m_pre2.group(1) if m_pre2 else resp_text2
                    parsed = json.loads(html.unescape(raw2))
            except Exception as e2:
                logger.warning(f"FlareSolverr fresh retry failed: {e2}")

        if not parsed or not isinstance(parsed, dict):
            return None

        html_content = parsed.get('data', '')
        if not html_content:
            return None

        candidates = []
        a_tags = re.findall(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html_content, re.DOTALL | re.IGNORECASE)
        for dl_url, label in a_tags:
            clean_url = dl_url.replace('&amp;', '&')
            if not clean_url.startswith('http') or 'javascript' in clean_url:
                continue
            label_text = re.sub(r'<[^>]+>', '', label).strip()
            score = parse_resolution_score(label_text)
            if score > 0 or 'download' in label_text.lower() or 'mp4' in clean_url:
                res_name = f"{score}p" if score > 0 else label_text
                candidates.append({
                    'url': clean_url,
                    'score': score,
                    'resolution': res_name,
                    'is_direct': True # Direct download from snapcdn!
                })

        if candidates:
            candidates.sort(key=lambda x: x['score'], reverse=True)
            m = re.search(r'status/(\d+)', url)
            tweet_id = m.group(1) if m else str(int(time.time()))
            title_match = re.search(r'<div class=["\']content["\'][^>]*>.*?<h3>(.*?)</h3>', html_content, re.DOTALL)
            title = title_match.group(1).strip() if title_match else f"Twitter_Video_{tweet_id}"
            return {
                'success': True,
                'title': title[:80],
                'author': 'Twitter User',
                'screen_name': 'twitter',
                'tweet_id': tweet_id,
                'candidates': candidates,
                'resolution': candidates[0]['resolution'],
                'source': 'snapvid (FlareSolverr 无头浏览器)'
            }
    except Exception as e:
        logger.warning(f"FlareSolverr SnapVid extraction error: {e}")

    return None

def extract_twitter_video(url: str, proxies: dict = None) -> dict:
    m = re.search(r'status/(\d+)', url)
    if not m:
        return {'success': False, 'error': '无效的 Twitter/X 链接 (未识别到推文 ID)'}
    tweet_id = m.group(1)
    norm_url = f"https://x.com/i/status/{tweet_id}"

    # 0. 内存极速缓存层 (0ms 返回，避免重复请求)
    cached_res = get_cached_twitter_result(tweet_id)
    if cached_res:
        return cached_res

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    # 1. 方案 A (第一优先级)：FlareSolverr 无头浏览器穿透 Cloudflare，直接提取 SnapVid 免代理直连源
    try:
        fs_res = extract_from_snapvid_flaresolverr(norm_url, proxies=proxies)
        if fs_res and fs_res.get('candidates'):
            fs_res['tweet_id'] = tweet_id
            set_cached_twitter_result(tweet_id, fs_res)
            return fs_res
    except Exception as e:
        logger.warning(f"方案 A (FlareSolverr + SnapVid) 解析异常: {e}")

    # 2. 方案 A 备用：常规直接请求 Snapvid (若 Cloudflare 未阻断)
    try:
        snap_res = extract_from_snapvid(norm_url, proxies)
        if snap_res and snap_res.get('candidates'):
            snap_res['tweet_id'] = tweet_id
            set_cached_twitter_result(tweet_id, snap_res)
            return snap_res
    except Exception as e:
        logger.warning(f"常规 Snapvid 解析异常: {e}")

    # 3. 方案 B 顺位一：download-x-video.com (代理通道，智能画质计算)
    try:
        dx_res = extract_from_download_x_video(norm_url, proxies)
        if dx_res and dx_res.get('candidates'):
            dx_res['tweet_id'] = tweet_id
            set_cached_twitter_result(tweet_id, dx_res)
            return dx_res
    except Exception as e:
        logger.warning(f"方案 B (download-x-video.com) 解析异常: {e}")

    # 4. 方案 B 顺位二：FixTweet
    try:
        fx_url = f'https://api.fxtwitter.com/status/{tweet_id}'
        r = requests.get(fx_url, headers=headers, proxies=proxies, timeout=10)
        if r.status_code == 200:
            data = r.json()
            tweet = data.get('tweet', {})
            media = tweet.get('media', {})
            videos = media.get('videos', []) or media.get('all', [])

            candidates = []
            for item in videos:
                if item.get('type') in ('video', 'gif'):
                    variants = item.get('variants', [])
                    if variants:
                        for v in variants:
                            v_url = v.get('url', '')
                            if v_url and ('mp4' in v_url or v.get('content_type') == 'video/mp4'):
                                m_res = re.search(r'/(\d+)x(\d+)/', v_url)
                                if m_res:
                                    w, h = int(m_res.group(1)), int(m_res.group(2))
                                    res_label = f"{w}x{h}"
                                    score = w * h
                                else:
                                    res_label = f"{item.get('width', 0)}x{item.get('height', 0)}"
                                    score = parse_resolution_score(res_label) or v.get('bitrate', 0)
                                candidates.append({
                                    'url': v_url,
                                    'score': score,
                                    'resolution': res_label,
                                    'is_direct': False
                                })
                    elif item.get('url'):
                        v_url = item.get('url')
                        m_res = re.search(r'/(\d+)x(\d+)/', v_url)
                        if m_res:
                            w, h = int(m_res.group(1)), int(m_res.group(2))
                            res_label = f"{w}x{h}"
                            score = w * h
                        else:
                            res_label = f"{item.get('width', 0)}x{item.get('height', 0)}"
                            score = parse_resolution_score(res_label) or 999999
                        candidates.append({
                            'url': v_url,
                            'score': score,
                            'resolution': res_label,
                            'is_direct': False
                        })

            if candidates:
                candidates.sort(key=lambda x: x['score'], reverse=True)
                res = {
                    'success': True,
                    'title': tweet.get('text', '')[:100],
                    'author': tweet.get('author', {}).get('name', 'Twitter User'),
                    'screen_name': tweet.get('author', {}).get('screen_name', 'twitter'),
                    'tweet_id': tweet_id,
                    'candidates': candidates,
                    'resolution': candidates[0]['resolution'],
                    'source': 'fxtwitter'
                }
                set_cached_twitter_result(tweet_id, res)
                return res
    except Exception as e:
        logger.warning(f"fxtwitter extraction error: {e}")

    # 5. 方案 B 顺位三：vxtwitter
    try:
        vx_url = f'https://api.vxtwitter.com/Twitter/status/{tweet_id}'
        r = requests.get(vx_url, headers=headers, proxies=proxies, timeout=10)
        if r.status_code == 200:
            data = r.json()
            media_ext = data.get('media_extended', [])
            candidates = []
            for item in media_ext:
                if item.get('type') in ('video', 'gif') and item.get('url'):
                    v_url = item.get('url')
                    m_res = re.search(r'/(\d+)x(\d+)/', v_url)
                    if m_res:
                        w, h = int(m_res.group(1)), int(m_res.group(2))
                        res_label = f"{w}x{h}"
                        score = w * h
                    else:
                        size = item.get('size', {})
                        res_label = f"{size.get('width', 0)}x{size.get('height', 0)}"
                        score = parse_resolution_score(res_label) or 999999
                    candidates.append({
                        'url': v_url,
                        'score': score,
                        'resolution': res_label,
                        'is_direct': False
                    })
            if candidates:
                candidates.sort(key=lambda x: x['score'], reverse=True)
                res = {
                    'success': True,
                    'title': data.get('text', '')[:100],
                    'author': data.get('user_name', 'Twitter User'),
                    'screen_name': data.get('user_screen_name', 'twitter'),
                    'tweet_id': tweet_id,
                    'candidates': candidates,
                    'resolution': candidates[0]['resolution'],
                    'source': 'vxtwitter'
                }
                set_cached_twitter_result(tweet_id, res)
                return res
    except Exception as e:
        logger.warning(f"vxtwitter extraction error: {e}")

    return {'success': False, 'error': '未在推文中找到可下载的视频内容，可能该推文不含视频或属于私密推文'}

def download_with_fallback(candidates: list, output_path: str, proxies: dict = None, progress_callback = None, cancel_event: threading.Event = None) -> dict:
    """Downloads video with automatic HTTP Range resume, multi-resolution fallback, and network retry."""
    if os.path.exists(output_path) and os.path.getsize(output_path) > 1024 * 1024:
        logger.info(f"File already exists on disk: {output_path}")
        return {'success': True, 'used_resolution': '已存在文件', 'connection': 'local'}

    temp_path = output_path + ".part"
    base_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Connection': 'keep-alive',
    }

    last_error = None

    for idx, c in enumerate(candidates):
        if cancel_event and cancel_event.is_set():
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            return {'success': False, 'error': '用户已取消下载', 'cancelled': True}

        url = c.get('url')
        res_str = c.get('resolution', '最高画质')
        logger.info(f"Downloading option {idx+1}/{len(candidates)} [{res_str}]...")

        # A configured NAS proxy remains authoritative across retries and mirrors.
        modes = [('proxy', proxies, (15, 60))] if proxies else [('direct', {}, (15, 60))]

        for mode_name, active_proxy, (conn_timeout, read_timeout) in modes:
            if cancel_event and cancel_event.is_set():
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                return {'success': False, 'error': '用户已取消下载', 'cancelled': True}

            logger.info(f"Attempting [{res_str}] via {mode_name}...")

            max_retries = 5
            total_size = 0
            last_downloaded_bytes = 0
            stagnant_retries = 0

            for attempt in range(max_retries):
                if cancel_event and cancel_event.is_set():
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass
                    return {'success': False, 'error': '用户已取消下载', 'cancelled': True}

                existing_bytes = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0

                req_headers = dict(base_headers)
                if existing_bytes > 0:
                    req_headers['Range'] = f'bytes={existing_bytes}-'
                    logger.info(f"Resuming [{res_str}] via {mode_name} from byte {existing_bytes} (attempt {attempt+1}/{max_retries})...")
                else:
                    logger.info(f"Starting [{res_str}] via {mode_name} (attempt {attempt+1}/{max_retries})...")

                # Fresh requests.Session per attempt prevents contaminated SSL socket reuse
                session = requests.Session()
                session.trust_env = False
                try:
                    r = session.get(
                        url,
                        headers=req_headers,
                        proxies=active_proxy,
                        stream=True,
                        timeout=(conn_timeout, read_timeout)
                    )

                    if r.status_code == 206:
                        file_mode = 'ab'
                        if not total_size:
                            cr = r.headers.get('Content-Range', '')
                            m_tot = re.search(r'/(\d+)', cr)
                            if m_tot:
                                total_size = int(m_tot.group(1))
                            else:
                                cl = int(r.headers.get('Content-Length', 0))
                                total_size = existing_bytes + cl
                    elif r.status_code == 200:
                        file_mode = 'wb'
                        existing_bytes = 0
                        total_size = int(r.headers.get('Content-Length', 0))
                    elif r.status_code == 416:
                        # Range Not Satisfiable
                        if total_size > 0 and existing_bytes >= total_size:
                            logger.info(f"File already fully downloaded ({existing_bytes} bytes).")
                            break
                        logger.warning("Range 416 Not Satisfiable. Resetting download to byte 0.")
                        existing_bytes = 0
                        file_mode = 'wb'
                        req_headers.pop('Range', None)
                        r = session.get(url, headers=req_headers, proxies=active_proxy, stream=True, timeout=(conn_timeout, read_timeout))
                        if r.status_code == 200:
                            total_size = int(r.headers.get('Content-Length', 0))
                        else:
                            raise Exception(f"HTTP error {r.status_code}")
                    else:
                        raise Exception(f"HTTP error {r.status_code}")

                    if not total_size:
                        total_size = existing_bytes + int(r.headers.get('Content-Length', 0))

                    chunk_size = 64 * 1024  # 64KB for frequent I/O & keep-alive activity
                    downloaded_in_session = 0
                    start_time = time.time()

                    with open(temp_path, file_mode) as f:
                        for chunk in r.iter_content(chunk_size=chunk_size):
                            if cancel_event and cancel_event.is_set():
                                break
                            if chunk:
                                f.write(chunk)
                                downloaded_in_session += len(chunk)
                                cur_bytes = existing_bytes + downloaded_in_session
                                now = time.time()
                                spd = downloaded_in_session / max(0.001, now - start_time)
                                if progress_callback:
                                    progress_callback(cur_bytes, total_size, spd, res_str)

                    if cancel_event and cancel_event.is_set():
                        if os.path.exists(temp_path):
                            try:
                                os.remove(temp_path)
                            except Exception:
                                pass
                        return {'success': False, 'error': '用户已取消下载', 'cancelled': True}

                    # Check completion
                    final_bytes = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
                    if (total_size > 0 and final_bytes >= total_size) or (total_size == 0 and final_bytes > 1024):
                        if os.path.exists(output_path):
                            try:
                                os.remove(output_path)
                            except Exception:
                                pass
                        os.replace(temp_path, output_path)
                        logger.info(f"Download succeeded for [{res_str}] via {mode_name} ({final_bytes} bytes)!")
                        return {'success': True, 'used_resolution': res_str, 'connection': mode_name}

                except Exception as ex_attempt:
                    last_error = str(ex_attempt)
                    curr_bytes = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
                    logger.warning(f"Download stream error at {curr_bytes}/{total_size} bytes via {mode_name}: {ex_attempt}")

                    if curr_bytes > last_downloaded_bytes:
                        last_downloaded_bytes = curr_bytes
                        stagnant_retries = 0
                    else:
                        stagnant_retries += 1
                        if stagnant_retries >= 3:
                            logger.warning(f"No bytes transferred for 3 consecutive retries via {mode_name}. Giving up on this mode.")
                            break

                    time.sleep(2)
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass

        # Candidate failed completely across all modes. Clean partial file before falling back to next resolution
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    return {'success': False, 'error': f"所有可用分辨率下载均失败: {last_error}"}

async def on_twitter_message(client: pyrogram.Client, message: pyrogram.types.Message, save_base_path: str, proxy_cfg: dict):
    """Handler for Twitter/X link messages."""
    if not message.text:
        return

    url_match = re.search(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\S+", message.text)
    if not url_match:
        return

    target_url = url_match.group(0)

    tw_task_id = secrets.token_hex(4)
    cancel_event = threading.Event()
    cancel_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消下载", callback_data=f"tw:cancel:{tw_task_id}")]
    ])

    status_msg = await client.send_message(
        message.chat.id,
        "🔍 **正在识别 X/Twitter 视频链接...**\n正在检索最高分辨率媒体流...",
        reply_to_message_id=message.id,
        reply_markup=cancel_markup,
    )

    proxy_dict = None
    if proxy_cfg and proxy_cfg.get("hostname"):
        scheme = proxy_cfg.get("scheme", "http")
        host = proxy_cfg.get("hostname")
        port = proxy_cfg.get("port")
        proxy_url = f"{scheme}://{host}:{port}"
        proxy_dict = {"http": proxy_url, "https": proxy_url}

    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, extract_twitter_video, target_url, proxy_dict)

    if not res.get("success"):
        err_text = res.get("error", "未找到可下载的视频")
        await status_msg.edit_text(f"❌ **解析失败**: {err_text}\n\n(提示: 该推文可能只包含纯图片/文字，或是私密推文)", reply_markup=None)
        return

    title = res.get("title", "twitter_video")
    screen_name = res.get("screen_name", "twitter")
    candidates = res.get("candidates", [])
    tweet_id = res.get("tweet_id", "")
    best_res = res.get("resolution", "未知")

    clean_title = re.sub(r'[\\/*?:"<>|\n\r]', " ", title)[:35].strip() or "video"
    tweet_folder = f"{tweet_id} - {clean_title}"
    output_dir = os.path.join(save_base_path, "twitter", tweet_folder)
    os.makedirs(output_dir, exist_ok=True)
    output_filename = f"{tweet_id} - {clean_title}.mp4"
    output_path = os.path.join(output_dir, output_filename)

    _active_twitter_tasks[tw_task_id] = {
        "cancel_event": cancel_event,
        "status_msg": status_msg,
        "title": clean_title,
        "temp_path": output_path + ".part",
    }

    last_update_ts = [0.0]

    def on_progress(down_bytes, total_bytes, speed, res_name):
        now = time.time()
        if now - last_update_ts[0] < 2.0 and (total_bytes > 0 and down_bytes < total_bytes):
            return
        last_update_ts[0] = now

        if total_bytes > 0:
            pct = min(100, int(down_bytes / total_bytes * 100))
            bar = create_progress_bar(pct)
            down_str = format_byte(down_bytes)
            tot_str = format_byte(total_bytes)
            spd_str = format_byte(speed) + "/s"
            text = (
                f"📥 **X (Twitter) 视频下载中**:\n"
                f" ├─ 📁 : {clean_title}.mp4\n"
                f" ├─ 📐 : {res_name}\n"
                f" ├─ 📏 : {down_str} / {tot_str}\n"
                f" ├─ ⏬ : {spd_str}\n"
                f" └─ 📊 : [{bar}] ({pct}%)\n"
            )
        else:
            down_str = format_byte(down_bytes)
            spd_str = format_byte(speed) + "/s"
            text = (
                f"📥 **X (Twitter) 视频下载中**:\n"
                f" ├─ 📁 : {clean_title}.mp4\n"
                f" ├─ 📐 : {res_name}\n"
                f" ├─ 📏 : 已下载 {down_str}\n"
                f" └─ ⏬ : {spd_str}\n"
            )

        async def _edit():
            try:
                await status_msg.edit_text(text, reply_markup=cancel_markup)
            except Exception:
                pass

        asyncio.run_coroutine_threadsafe(_edit(), loop)

    await status_msg.edit_text(
        f"⏳ **已捕获视频！**\n"
        f"📐 目标画质: **{best_res}** (最高画质优先)\n"
        f"📥 正在连接并开始下载，请稍候...",
        reply_markup=cancel_markup,
    )

    dl_res = await loop.run_in_executor(None, download_with_fallback, candidates, output_path, proxy_dict, on_progress, cancel_event)

    _active_twitter_tasks.pop(tw_task_id, None)

    if dl_res.get("cancelled") or cancel_event.is_set():
        return

    if not dl_res.get("success"):
        await status_msg.edit_text(f"❌ **视频下载失败**: {dl_res.get('error')}\n已尝试全部可用分辨率回退。", reply_markup=None)
        return

    used_resolution = dl_res.get("used_resolution", best_res)
    conn_raw = dl_res.get("connection", "proxy")
    if conn_raw == "local":
        conn_label = "⚡ 本地已有缓存"
    elif conn_raw == "direct":
        conn_label = "🚀 本地高速直连 (免代理)"
    else:
        conn_label = "🌐 代理中转通道 (支持断点续传)"

    filesize_bytes = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    filesize_mb = filesize_bytes / (1024 * 1024)

    msg_content = (
        f"✅ **X (Twitter) 视频下载完成！**\n\n"
        f"👤 **作者**: @{screen_name}\n"
        f"📝 **描述**: {title}\n"
        f"📐 **分辨率**: {used_resolution} (最高可用画质)\n"
        f"📦 **大小**: {filesize_mb:.2f} MB\n"
        f"⚡ **线路**: {conn_label}\n"
        f"📁 **飞牛存储路径**:\n`Docker/tg-downloader/downloads/twitter/{tweet_folder}/{output_filename}`"
    )

    await status_msg.edit_text(msg_content, reply_markup=None)


async def on_twitter_callback(client: pyrogram.Client, query: pyrogram.types.CallbackQuery):
    """Handles cancellation of Twitter video downloads."""
    if not query.data or not query.data.startswith("tw:cancel:"):
        return
    tw_id = query.data.split(":", 2)[2]
    task_info = _active_twitter_tasks.get(tw_id)
    if task_info:
        task_info["cancel_event"].set()
        status_msg = task_info.get("status_msg")
        title = task_info.get("title", "Twitter 视频")
        temp_path = task_info.get("temp_path")
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        cancel_text = (
            f"🛑 **【X (Twitter) 视频下载已取消】**\n\n"
            f"📌 **文件**：`{title}.mp4`\n"
            f"⚠️ 该视频下载已被用户手动取消。"
        )
        if status_msg:
            try:
                await status_msg.edit_text(cancel_text, reply_markup=None)
            except Exception:
                pass
        _active_twitter_tasks.pop(tw_id, None)
        await query.answer("🛑 已取消视频下载", show_alert=False)
    else:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer("⚠️ 该下载任务已结束或已被取消", show_alert=False)


def register(bot_instance):
    """Registers Twitter plugin onto DownloadBot."""
    global FLARESOLVERR_URL
    cfg = getattr(bot_instance, "config", {}) or {}
    p_cfg = cfg.get("plugins", {}).get("twitter", {}) if isinstance(cfg, dict) else {}
    FLARESOLVERR_URL = p_cfg.get("flaresolverr_url", FLARESOLVERR_URL)

    async def _handler(client, message):
        await on_twitter_message(client, message, bot_instance.app.save_path, getattr(bot_instance.app, "proxy", None))

    handler = MessageHandler(
        _handler,
        filters=pyrogram.filters.regex(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\S+")
        & pyrogram.filters.user(bot_instance.allowed_user_ids)
    )
    bot_instance.bot.add_handler(handler)

    cb_handler = CallbackQueryHandler(
        on_twitter_callback,
        filters=pyrogram.filters.regex(r"^tw:cancel:") & pyrogram.filters.user(bot_instance.allowed_user_ids)
    )
    bot_instance.bot.add_handler(cb_handler)
    return [handler, cb_handler]
