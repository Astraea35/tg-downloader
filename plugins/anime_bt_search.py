"""
Nyaa / Sukebei / VCB-Studio 动漫与 BT 资源检索及 NAS 自动离线下载插件
支持：
- /nyaa [关键词] [new/hot]：全年龄动漫番剧/RAW/无损音乐检索
- /sukebei [关键词] [new/hot]：R18 绅士里区资源检索
- /vcb [关键词]：VCB-Studio 蓝光高清精压收藏检索
- 直接发送磁力链接 (magnet:?xt=...) 或种子文件：全自动推送到 NAS 下载
- 搜索结果交互卡片：一键「🚀 推送至 NAS 下载」，支持内联分页
- 存储目标：/vol1/1000/总和/娱乐
"""

import os
import re
import time
import html
import asyncio
import hashlib
import tempfile
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Set, Tuple

import pyrogram
from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.enums import ParseMode
from loguru import logger

PLUGIN_NAME = "动漫 BT 检索与 NAS 自动下载"
PLUGIN_DESC = "聚合 Nyaa/Sukebei/VCB-Studio 动漫磁力检索，支持一键推送与直发磁链下载至 NAS 娱乐目录"
PLUGIN_VERSION = "1.0"

# 宿主机辅助微服务地址 (负责桥接至 fnOS 内置 qBittorrent 引擎)
STEAM_SERVICE_URL = "http://192.168.1.34:8192"
DEFAULT_SAVE_PATH = "/vol1/1000/总和/娱乐"
DEFAULT_PROXY = "http://192.168.1.34:7890"

# 每页展示条数
SUBPAGE_SIZE = 4

# 内存会话与缓存 (sid -> session_dict)
_sessions: Dict[str, dict] = {}
_hash_to_torrent: Dict[str, dict] = {}  # info_hash -> {title, magnet, size}
_cancelled_qbt_hashes: Set[str] = set()
_bot_instance = None


def get_proxy_dict(bot_instance) -> dict:
    """获取代理配置，默认走 192.168.1.34:7890"""
    if bot_instance:
        cfg = getattr(bot_instance, "config", {})
        proxy_cfg = cfg.get("proxy")
        if not proxy_cfg and hasattr(bot_instance, "app") and bot_instance.app:
            proxy_cfg = getattr(bot_instance.app, "proxy", None)

        if isinstance(proxy_cfg, dict):
            scheme = proxy_cfg.get("scheme", "http")
            hostname = proxy_cfg.get("hostname") or proxy_cfg.get("host")
            port = proxy_cfg.get("port")
            if hostname and port:
                p_url = f"{scheme}://{hostname}:{port}"
                return {"http": p_url, "https": p_url}
        elif isinstance(proxy_cfg, str) and proxy_cfg:
            return {"http": proxy_cfg, "https": proxy_cfg}

    return {"http": DEFAULT_PROXY, "https": DEFAULT_PROXY}


def sync_fetch_rss(url: str, proxy_dict: dict = None, timeout: float = 12.0) -> bytes:
    """通过代理拉取 Nyaa/Sukebei 官方 RSS XML 数据"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    req = urllib.request.Request(url, headers=headers)
    p_handler = urllib.request.ProxyHandler(proxy_dict) if proxy_dict else urllib.request.ProxyHandler()
    opener = urllib.request.build_opener(p_handler)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


async def fetch_rss_async(url: str) -> bytes:
    """异步非阻塞拉取 RSS"""
    proxy = get_proxy_dict(_bot_instance)
    return await asyncio.to_thread(sync_fetch_rss, url, proxy)


def push_to_nas_qbt(magnet: str, save_path: str = DEFAULT_SAVE_PATH, category: str = "动漫") -> dict:
    """调用宿主机微服务将磁力链接推送到 qBittorrent 下载队列"""
    api_url = f"{STEAM_SERVICE_URL.rstrip('/')}/qbt/add"
    payload = json_dumps = urllib.request.json.dumps({
        "magnet": magnet,
        "save_path": save_path,
        "category": category,
    }).encode("utf-8") if hasattr(urllib.request, "json") else __import__("json").dumps({
        "magnet": magnet,
        "save_path": save_path,
        "category": category,
    }).encode("utf-8")

    req = urllib.request.Request(api_url, data=payload, headers={"Content-Type": "application/json"})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=8) as resp:
            return __import__("json").loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"[AnimeSearch] 推送至 NAS 失败: {e}")
        return {"status": "error", "message": f"连接 NAS 下载服务失败: {e}"}


async def push_to_nas_qbt_async(magnet: str, save_path: str = DEFAULT_SAVE_PATH, category: str = "动漫") -> dict:
    """异步执行推送"""
    return await asyncio.to_thread(push_to_nas_qbt, magnet, save_path, category)


def delete_from_nas_qbt(info_hash: str, delete_files: bool = True) -> dict:
    """调用宿主机微服务将任务从 qBittorrent 中删除/取消"""
    api_url = f"{STEAM_SERVICE_URL.rstrip('/')}/qbt/delete"
    payload = json_dumps = urllib.request.json.dumps({
        "hash": info_hash,
        "delete_files": delete_files,
    }).encode("utf-8") if hasattr(urllib.request, "json") else __import__("json").dumps({
        "hash": info_hash,
        "delete_files": delete_files,
    }).encode("utf-8")

    req = urllib.request.Request(api_url, data=payload, headers={"Content-Type": "application/json"})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=8) as resp:
            return __import__("json").loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"[AnimeSearch] 从 NAS 删除任务失败: {e}")
        return {"status": "error", "message": f"连接 NAS 删除服务失败: {e}"}


async def delete_from_nas_qbt_async(info_hash: str, delete_files: bool = True) -> dict:
    """异步执行取消/删除"""
    return await asyncio.to_thread(delete_from_nas_qbt, info_hash, delete_files)


def create_progress_bar(progress: float, total_bars: int = 10) -> str:
    """生成字符进度条: [██████░░░░]"""
    completed = min(total_bars, max(0, int(progress * total_bars / 100)))
    remaining = total_bars - completed
    return "█" * completed + "░" * remaining


def fetch_qbt_progress(info_hash: str) -> dict:
    """从宿主机微服务查询 qbt 任务实时状态"""
    url = f"{STEAM_SERVICE_URL.rstrip('/')}/qbt/progress?hash={urllib.parse.quote(info_hash)}"
    try:
        req = urllib.request.Request(url)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5) as resp:
            return __import__("json").loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def fetch_qbt_progress_async(info_hash: str) -> dict:
    return await asyncio.to_thread(fetch_qbt_progress, info_hash)


def fetch_qbt_list() -> dict:
    """从宿主机微服务查询 qbt 全部任务列表"""
    url = f"{STEAM_SERVICE_URL.rstrip('/')}/qbt/list"
    try:
        req = urllib.request.Request(url)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5) as resp:
            return __import__("json").loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"status": "error", "message": str(e), "items": []}


async def fetch_qbt_list_async() -> dict:
    return await asyncio.to_thread(fetch_qbt_list)


async def monitor_qbt_progress(client: pyrogram.Client, chat_id: int, message_id: int, info_hash: str, fallback_title: str):
    """在后台平滑刷新电报消息，展示下载进度条与实时速度"""
    start_time = time.time()
    poll_count = 0
    short_hash = info_hash[:12] if info_hash else ""
    action_btns = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 刷新进度", callback_data=f"b:rt:{short_hash}"),
            InlineKeyboardButton("❌ 取消下载", callback_data=f"b:cancel:{short_hash}")
        ]
    ]) if short_hash else None

    # 最长后台轮询 60 分钟
    while time.time() - start_time < 3600:
        if (info_hash and info_hash.lower() in _cancelled_qbt_hashes) or (short_hash and short_hash.lower() in _cancelled_qbt_hashes):
            break

        elapsed = time.time() - start_time
        # 前 2 分钟每 3 秒刷新，之后每 8 秒刷新
        interval = 3 if elapsed < 120 else 8
        await asyncio.sleep(interval)

        if (info_hash and info_hash.lower() in _cancelled_qbt_hashes) or (short_hash and short_hash.lower() in _cancelled_qbt_hashes):
            break

        info = await fetch_qbt_progress_async(info_hash)
        if info.get("status") != "ok" or not info.get("found"):
            poll_count += 1
            if poll_count > 12:
                break
            continue

        pct = info.get("percent", 0.0)
        dlspeed_str = info.get("dlspeed_str", "0 KB/s")
        eta_str = info.get("eta_str", "计算中...")
        downloaded_str = info.get("downloaded_str", "0 B")
        total_size_str = info.get("total_size_str", "未知")
        state_desc = info.get("state_desc", "下载中")
        seeds = info.get("seeds", 0)
        leechs = info.get("leechs", 0)
        bar = create_progress_bar(pct, 10)
        safe_title = html.escape(info.get("name") or fallback_title)
        actual_save_path = info.get("save_path") or DEFAULT_SAVE_PATH

        # 检查是否已完成
        if info.get("is_complete") or pct >= 100.0:
            complete_text = (
                f"🎉 <b>NAS 离线下载完成！</b>\n\n"
                f"🎬 <b>资源名称：</b>{safe_title}\n"
                f"📁 <b>保存路径：</b><code>{html.escape(actual_save_path)}</code>\n"
                f"📦 <b>总计大小：</b>{total_size_str}\n"
                f"⚡ <i>已入库完成，可随时通过飞牛影视或播放器观看！</i>"
            )
            try:
                await client.edit_message_text(
                    chat_id,
                    message_id,
                    complete_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                    disable_web_page_preview=True
                )
            except Exception:
                pass
            break

        # 构造实时进度展示卡片
        progress_text = (
            f"🎬 <b>{safe_title}</b>\n"
            f"📁 <b>目标目录：</b><code>{html.escape(actual_save_path)}</code>\n\n"
            f"📥 <b>NAS 离线下载中:</b>\n"
            f" ├─ 📊 <code>[{bar}]</code> <b>{pct}%</b>\n"
            f" ├─ 📏 <b>进度：</b>{downloaded_str} / {total_size_str}\n"
            f" ├─ ⏬ <b>速度：</b>{dlspeed_str} (⏱ 剩余: {eta_str})\n"
            f" └─ 📁 <b>状态：</b>{state_desc} (🟢 做种: {seeds} · 🔴 下载: {leechs})"
        )

        try:
            await client.edit_message_text(
                chat_id,
                message_id,
                progress_text,
                parse_mode=ParseMode.HTML,
                reply_markup=action_btns,
                disable_web_page_preview=True
            )
        except Exception:
            pass


def parse_nyaa_rss(xml_bytes: bytes) -> List[dict]:
    """解析 Nyaa/Sukebei 返回的 RSS 2.0 XML"""
    try:
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        logger.error(f"[AnimeSearch] XML 解析失败: {e}")
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    items = channel.findall("item")
    results = []

    for it in items:
        title_el = it.find("title")
        title = title_el.text.strip() if title_el is not None and title_el.text else "未命名资源"

        size_el = it.find("{https://nyaa.si/xmlns/nyaa}size")
        size = size_el.text.strip() if size_el is not None and size_el.text else "未知"

        seeders_el = it.find("{https://nyaa.si/xmlns/nyaa}seeders")
        seeders = seeders_el.text.strip() if seeders_el is not None and seeders_el.text else "0"

        leechers_el = it.find("{https://nyaa.si/xmlns/nyaa}leechers")
        leechers = leechers_el.text.strip() if leechers_el is not None and leechers_el.text else "0"

        date_el = it.find("pubDate")
        pub_date = date_el.text.strip() if date_el is not None and date_el.text else ""
        if pub_date:
            # 简写日期：Wed, 23 Sep 2026 14:00:00 -> 2026-09-23
            try:
                dt = time.strptime(pub_date[:16], "%a, %d %b %Y")
                pub_date = time.strftime("%Y-%m-%d", dt)
            except Exception:
                pass

        hash_el = it.find("{https://nyaa.si/xmlns/nyaa}infoHash")
        info_hash = hash_el.text.strip().lower() if hash_el is not None and hash_el.text else ""

        link_el = it.find("link")
        link = link_el.text.strip() if link_el is not None and link_el.text else ""

        # 构建规范 magnet 链接
        if info_hash:
            magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(title)}"
        else:
            magnet = ""

        item_data = {
            "title": title,
            "size": size,
            "seeders": seeders,
            "leechers": leechers,
            "date": pub_date,
            "info_hash": info_hash,
            "magnet": magnet,
            "link": link,
        }
        results.append(item_data)
        if info_hash and magnet:
            _hash_to_torrent[info_hash] = item_data

    return results


def parse_args_and_sort(raw_text: str) -> Tuple[str, str]:
    """解析指令参数与排序模式（默认 hot/seeders，若包含 new 则按时间排）"""
    parts = raw_text.strip().split()
    if not parts:
        return "", "seeders"

    sort_type = "seeders"
    clean_parts = []
    for p in parts:
        lower = p.lower()
        if lower in ("new", "最新", "time", "date"):
            sort_type = "id"
        elif lower in ("hot", "热门", "seeder", "seeders"):
            sort_type = "seeders"
        else:
            clean_parts.append(p)

    keyword = " ".join(clean_parts).strip()
    return keyword, sort_type


def format_anime_message(platform_name: str, keyword: str, sort_name: str, items: List[dict], subpage: int, total_subpages: int) -> str:
    """排版输出 Telegram HTML 卡片 (精简版，不直接展示长磁链)"""
    start_idx = (subpage - 1) * SUBPAGE_SIZE
    page_items = items[start_idx : start_idx + SUBPAGE_SIZE]

    lines = [
        f"🌸 <b>{html.escape(platform_name)} 检索：</b><code>{html.escape(keyword)}</code>",
        f"⚡ <b>排序：</b>{sort_name} · <b>共 {len(items)} 条</b> (第 {subpage}/{total_subpages} 页)",
        f"📁 <b>NAS 下载目录：</b><code>{html.escape(DEFAULT_SAVE_PATH)}</code>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    if not page_items:
        lines.append("\n<i>⚠️ 未找到符合条件的资源，建议更换或精简关键词后重新搜索。</i>\n")
    else:
        for idx, it in enumerate(page_items, start=start_idx + 1):
            safe_title = html.escape(it.get("title", "未命名"))
            size = it.get("size", "未知")
            seeders = it.get("seeders", "0")
            leechers = it.get("leechers", "0")
            date = it.get("date", "")
            link = it.get("link", "")

            if link:
                item_header = f"<b>{idx}. 🎬 <a href=\"{html.escape(link)}\">{safe_title}</a></b>"
            else:
                item_header = f"<b>{idx}. 🎬 {safe_title}</b>"

            meta_parts = [f"📦 {size}", f"🟢 做种: {seeders}", f"🔴 下载: {leechers}"]
            if date:
                meta_parts.append(f"📅 {date}")

            lines.append(f"{item_header}\n   {' · '.join(meta_parts)}\n")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 <i>点击【🚀 下载】直接入库 NAS，点击【🧲 磁链】提取并复制 ⬇️</i>")
    return "\n".join(lines)


def make_anime_keyboard(sid: str, items: List[dict], subpage: int, total_subpages: int) -> InlineKeyboardMarkup:
    """生成包含双动作按钮(下载/磁链)与翻页控制的键盘"""
    start_idx = (subpage - 1) * SUBPAGE_SIZE
    page_items = items[start_idx : start_idx + SUBPAGE_SIZE]

    rows = []
    for idx, it in enumerate(page_items, start=start_idx + 1):
        info_hash = it.get("info_hash") or ""
        short_hash = info_hash[:12] if info_hash else ""
        if short_hash:
            rows.append([
                InlineKeyboardButton(f"🚀 下载 #{idx}", callback_data=f"b:dl:{short_hash}"),
                InlineKeyboardButton(f"🧲 磁链 #{idx}", callback_data=f"b:cp:{short_hash}"),
            ])

    # 翻页按钮
    nav_row = []
    if subpage > 1:
        nav_row.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"b:pg:{sid}:{subpage - 1}"))
    else:
        nav_row.append(InlineKeyboardButton("⏮️ 首页", callback_data="b:noop"))

    nav_row.append(InlineKeyboardButton(f"📄 {subpage}/{total_subpages}", callback_data="b:noop"))

    if subpage < total_subpages:
        nav_row.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"b:pg:{sid}:{subpage + 1}"))
    else:
        nav_row.append(InlineKeyboardButton("⏭️ 末页", callback_data="b:noop"))

    rows.append(nav_row)
    return InlineKeyboardMarkup(rows)


async def execute_anime_search(client: pyrogram.Client, message: Message, platform_key: str):
    """统一执行 Nyaa/Sukebei/VCB 检索逻辑"""
    raw_text = message.text or ""
    parts = raw_text.strip().split(maxsplit=1)
    args_str = parts[1].strip() if len(parts) > 1 else ""

    # 空参数帮助
    if not args_str:
        platform_help = {
            "nyaa": "<b>🌸 Nyaa 全年龄动漫/番剧检索</b>\n\n用法: <code>/nyaa 关键词 [new/hot]</code>\n示例: <code>/nyaa 葬送的芙莉莲</code>、<code>/nyaa 咒术回战 new</code>",
            "sukebei": "<b>🔞 Sukebei Nyaa 绅士里区资源检索</b>\n\n用法: <code>/sukebei 关键词 [new/hot]</code>\n示例: <code>/sukebei 同人志</code>、<code>/sukebei 游戏 new</code>",
            "vcb": "<b>💎 VCB-Studio 蓝光精压合集检索</b>\n\n用法: <code>/vcb 关键词 [new/hot]</code>\n示例: <code>/vcb 新世纪福音战士</code>、<code>/vcb 紫罗兰永恒花园</code>",
        }
        await message.reply_text(platform_help.get(platform_key, "请输入搜索关键词"), parse_mode=ParseMode.HTML)
        return

    keyword, sort_type = parse_args_and_sort(args_str)
    sort_name = "🔥 最多做种优先" if sort_type == "seeders" else "🕒 最新发布优先"

    platform_names = {
        "nyaa": "Nyaa 动漫",
        "sukebei": "Sukebei 里区",
        "vcb": "VCB-Studio 蓝光",
    }
    p_name = platform_names.get(platform_key, "BT")

    status_msg = await message.reply_text(
        f"🔎 正在向 <b>{p_name}</b> 检索 <code>{html.escape(keyword)}</code>，请稍候...",
        parse_mode=ParseMode.HTML
    )

    # 构造请求 URL
    if platform_key == "sukebei":
        base_url = "https://sukebei.nyaa.si/?page=rss"
        query_val = keyword
    elif platform_key == "vcb":
        base_url = "https://nyaa.si/?page=rss"
        query_val = f"VCB-Studio {keyword}"
    else:  # nyaa
        base_url = "https://nyaa.si/?page=rss"
        query_val = keyword

    rss_url = f"{base_url}&q={urllib.parse.quote(query_val)}&s={sort_type}&o=desc"

    try:
        xml_data = await fetch_rss_async(rss_url)
        items = parse_nyaa_rss(xml_data)

        if not items:
            await status_msg.edit_text(
                f"⚠️ <b>{p_name}</b> 未找到与 <code>{html.escape(keyword)}</code> 相关的资源。\n建议更换英文名/罗马音或精简关键词重试。",
                parse_mode=ParseMode.HTML
            )
            return

        sid = hashlib.md5(f"{platform_key}:{keyword}:{sort_type}:{time.time()}".encode()).hexdigest()[:8]
        _sessions[sid] = {
            "platform_name": p_name,
            "keyword": keyword,
            "sort_name": sort_name,
            "items": items,
            "ts": time.time(),
        }

        total_subpages = max(1, (len(items) + SUBPAGE_SIZE - 1) // SUBPAGE_SIZE)
        msg_text = format_anime_message(p_name, keyword, sort_name, items, 1, total_subpages)
        keyboard = make_anime_keyboard(sid, items, 1, total_subpages)

        await status_msg.edit_text(
            msg_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"[AnimeSearch] 检索 {p_name} 发生错误: {e}")
        await status_msg.edit_text(
            f"❌ 检索失败，连接到 {p_name} 服务超时或受阻: {html.escape(str(e))}\n请稍后重试。",
            parse_mode=ParseMode.HTML
        )


async def handle_direct_magnet(client: pyrogram.Client, message: Message):
    """直接监听消息中的磁力链接，自动推送到 NAS 下载"""
    text = message.text or message.caption or ""
    # 提取磁力链接
    m = re.search(r"(magnet:\?xt=urn:btih:[a-zA-Z0-9]+[^\s]*)", text)
    if not m:
        return

    magnet = m.group(1).strip()
    status_msg = await message.reply_text("🧲 检测到磁力链接，正在连接 NAS 添加下载...", parse_mode=ParseMode.HTML)

    info_hash = ""
    hm = re.search(r"urn:btih:([a-zA-Z0-9]+)", magnet, re.I)
    if hm:
        info_hash = hm.group(1).lower()

    res = await push_to_nas_qbt_async(magnet, DEFAULT_SAVE_PATH, "动漫")
    if res.get("status") == "ok":
        if res.get("already_completed"):
            act_path = res.get("content_path") or res.get("save_path") or DEFAULT_SAVE_PATH
            await status_msg.edit_text(
                f"ℹ️ <b>该资源此前已下载完成并存在于 NAS 硬盘中！无需重复下载</b>\n\n"
                f"🎬 <b>资源名称：</b>{html.escape(res.get('name') or '磁力资源')}\n"
                f"📁 <b>实际位置：</b><code>{html.escape(act_path)}</code>\n"
                f"📦 <b>文件大小：</b>{res.get('total_size_str', '未知')}\n\n"
                f"⚡ <i>可随时通过飞牛影视或本地播放器直接观看！</i>",
                parse_mode=ParseMode.HTML
            )
            return

        cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ 取消下载", callback_data=f"b:cancel:{info_hash[:12]}")]
        ]) if info_hash else None

        if res.get("readded"):
            await status_msg.edit_text(
                f"🚀 <b>已自动清理历史残留记录，重新开始 NAS 离线下载！</b>\n\n"
                f"📁 <b>保存路径：</b><code>{html.escape(DEFAULT_SAVE_PATH)}</code>\n"
                f"🧲 <b>磁链：</b><code>{html.escape(magnet[:60])}...</code>\n\n"
                f"⏳ <i>正在连接做种者并拉取实时进度条...</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=cancel_kb
            )
        elif res.get("already_exists"):
            act_path = res.get("save_path") or DEFAULT_SAVE_PATH
            await status_msg.edit_text(
                f"ℹ️ <b>该磁力任务已在 NAS 下载队列中！</b>\n\n"
                f"📁 <b>下载路径：</b><code>{html.escape(act_path)}</code>\n"
                f"🧲 <b>磁链：</b><code>{html.escape(magnet[:60])}...</code>\n\n"
                f"⏳ <i>正在获取实时下载进度...</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=cancel_kb
            )
        else:
            await status_msg.edit_text(
                f"🚀 <b>磁力任务已成功推送到 NAS 下载！</b>\n\n"
                f"📁 <b>保存路径：</b><code>{html.escape(DEFAULT_SAVE_PATH)}</code>\n"
                f"🧲 <b>磁链：</b><code>{html.escape(magnet[:60])}...</code>\n\n"
                f"⏳ <i>正在连接做种者并拉取进度条...</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=cancel_kb
            )
        if info_hash:
            asyncio.create_task(
                monitor_qbt_progress(
                    client,
                    status_msg.chat.id,
                    status_msg.id,
                    info_hash,
                    "磁力下载任务"
                )
            )
    else:
        err = res.get("message", "未知错误")
        await status_msg.edit_text(f"❌ 添加失败: {html.escape(err)}", parse_mode=ParseMode.HTML)


async def handle_torrent_document(client: pyrogram.Client, message: Message):
    """直接监听上传的 .torrent 种子文件，自动转存并推送到 NAS 下载"""
    doc = message.document
    if not doc or not (doc.file_name and doc.file_name.lower().endswith(".torrent")):
        return

    status_msg = await message.reply_text("📥 收到种子文件，正在解析并推送到 NAS 下载...", parse_mode=ParseMode.HTML)

    try:
        # 下载种子临时文件
        with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as tmp:
            tmp_path = tmp.name

        await message.download(tmp_path)

        await status_msg.edit_text(
            f"✅ <b>种子文件已接收！</b>\n\n"
            f"📄 <b>文件：</b><code>{html.escape(doc.file_name)}</code>\n"
            f"📁 <b>目标目录：</b><code>{html.escape(DEFAULT_SAVE_PATH)}</code>\n"
            f"⚡ <i>正在推送到后台下载引擎。</i>",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"[AnimeSearch] 处理种子文件异常: {e}")
        await status_msg.edit_text(f"❌ 种子文件处理失败: {e}", parse_mode=ParseMode.HTML)


async def handle_bt_command(client: pyrogram.Client, message: Message):
    """查看 NAS 当前所有下载任务进度列表"""
    data = await fetch_qbt_list_async()
    if data.get("status") != "ok":
        await message.reply_text(f"❌ 获取下载任务失败: {html.escape(data.get('message', '未知错误'))}", parse_mode=ParseMode.HTML)
        return

    items = data.get("items", [])
    if not items:
        await message.reply_text("📭 <b>NAS 当前没有活跃的 BT 下载任务。</b>", parse_mode=ParseMode.HTML)
        return

    lines = [
        f"📋 <b>NAS 离线下载任务列表 (共 {len(items)} 个):</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    for idx, it in enumerate(items[:10], start=1):
        name = it.get("name") or "未命名"
        if len(name) > 36:
            name = name[:33] + "..."
        pct = it.get("percent", 0.0)
        bar = create_progress_bar(pct, 8)
        state_desc = it.get("state_desc", "运行中")
        spd = it.get("dlspeed_str", "0 B/s")
        total = it.get("total_size_str", "")
        down = it.get("downloaded_str", "")

        status_line = f"🟢 <b>{state_desc}</b>" if it.get("is_complete") else f"⏬ <b>{spd}</b> · <b>{state_desc}</b>"
        lines.append(
            f"<b>{idx}. 🎬 {html.escape(name)}</b>\n"
            f"   ├─ <code>[{bar}]</code> <b>{pct}%</b> ({down}/{total})\n"
            f"   └─ {status_line}\n"
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 <i>提示：所有下载资源默认保存至 <code>/vol1/1000/总和/娱乐</code></i>")

    btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 刷新任务列表", callback_data="b:bt_refresh")]])
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=btn, disable_web_page_preview=True)


async def handle_anime_callback(client: pyrogram.Client, callback_query: CallbackQuery):
    """处理内联翻页与一键下载推送"""
    data = callback_query.data or ""
    user_id = callback_query.from_user.id if callback_query.from_user else 0

    if _bot_instance and _bot_instance.allowed_user_ids:
        if user_id not in _bot_instance.allowed_user_ids:
            await callback_query.answer("⚠️ 您没有操作此机器人的权限", show_alert=True)
            return

    if data == "b:noop":
        await callback_query.answer()
        return

    # 0. 刷新全局任务列表 (b:bt_refresh)
    if data == "b:bt_refresh":
        data_list = await fetch_qbt_list_async()
        items = data_list.get("items", [])
        if not items:
            await callback_query.edit_message_text("📭 <b>NAS 当前没有活跃的 BT 下载任务。</b>", parse_mode=ParseMode.HTML)
            await callback_query.answer("列表已刷新")
            return

        lines = [
            f"📋 <b>NAS 离线下载任务列表 (共 {len(items)} 个):</b>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        for idx, it in enumerate(items[:10], start=1):
            name = it.get("name") or "未命名"
            if len(name) > 36:
                name = name[:33] + "..."
            pct = it.get("percent", 0.0)
            bar = create_progress_bar(pct, 8)
            state_desc = it.get("state_desc", "运行中")
            spd = it.get("dlspeed_str", "0 B/s")
            total = it.get("total_size_str", "")
            down = it.get("downloaded_str", "")

            status_line = f"🟢 <b>{state_desc}</b>" if it.get("is_complete") else f"⏬ <b>{spd}</b> · <b>{state_desc}</b>"
            lines.append(
                f"<b>{idx}. 🎬 {html.escape(name)}</b>\n"
                f"   ├─ <code>[{bar}]</code> <b>{pct}%</b> ({down}/{total})\n"
                f"   └─ {status_line}\n"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 <i>提示：所有下载资源默认保存至 <code>/vol1/1000/总和/娱乐</code></i>")

        btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 刷新任务列表", callback_data="b:bt_refresh")]])
        try:
            await callback_query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=btn, disable_web_page_preview=True)
            await callback_query.answer("任务列表已刷新")
        except Exception:
            await callback_query.answer("任务列表无变化")
        return

    # 1. 单任务手动刷新进度 (b:rt:{short_hash})
    if data.startswith("b:rt:"):
        short_hash = data.split(":", 2)[2].lower()
        info = await fetch_qbt_progress_async(short_hash)
        if info.get("status") != "ok" or not info.get("found"):
            await callback_query.answer("⚠️ 未查询到任务或任务已从队列移除", show_alert=True)
            return

        pct = info.get("percent", 0.0)
        dlspeed_str = info.get("dlspeed_str", "0 KB/s")
        eta_str = info.get("eta_str", "计算中...")
        downloaded_str = info.get("downloaded_str", "0 B")
        total_size_str = info.get("total_size_str", "未知")
        state_desc = info.get("state_desc", "下载中")
        seeds = info.get("seeds", 0)
        leechs = info.get("leechs", 0)
        bar = create_progress_bar(pct, 10)
        safe_title = html.escape(info.get("name") or "下载任务")
        actual_save_path = info.get("save_path") or DEFAULT_SAVE_PATH

        if info.get("is_complete") or pct >= 100.0:
            text = (
                f"🎉 <b>NAS 离线下载完成！</b>\n\n"
                f"🎬 <b>资源名称：</b>{safe_title}\n"
                f"📁 <b>保存路径：</b><code>{html.escape(actual_save_path)}</code>\n"
                f"📦 <b>总计大小：</b>{total_size_str}\n"
                f"⚡ <i>已入库完成，可随时通过飞牛影视或播放器观看！</i>"
            )
            reply_markup = None
        else:
            text = (
                f"🎬 <b>{safe_title}</b>\n"
                f"📁 <b>目标目录：</b><code>{html.escape(actual_save_path)}</code>\n\n"
                f"📥 <b>NAS 离线下载中:</b>\n"
                f" ├─ 📊 <code>[{bar}]</code> <b>{pct}%</b>\n"
                f" ├─ 📏 <b>进度：</b>{downloaded_str} / {total_size_str}\n"
                f" ├─ ⏬ <b>速度：</b>{dlspeed_str} (⏱ 剩余: {eta_str})\n"
                f" └─ 📁 <b>状态：</b>{state_desc} (🟢 做种: {seeds} · 🔴 下载: {leechs})"
            )
            reply_markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 刷新进度", callback_data=f"b:rt:{short_hash}"),
                    InlineKeyboardButton("❌ 取消下载", callback_data=f"b:cancel:{short_hash}")
                ]
            ])

        try:
            await callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup, disable_web_page_preview=True)
            await callback_query.answer("已刷新最新进度")
        except Exception:
            await callback_query.answer("进度无变化")
        return

    # 1.5 取消 NAS 离线下载 (b:cancel:{short_hash})
    if data.startswith("b:cancel:"):
        short_hash = data.split(":", 2)[2].lower()
        _cancelled_qbt_hashes.add(short_hash)
        await callback_query.answer("🛑 正在取消 NAS 离线下载任务...")

        await delete_from_nas_qbt_async(short_hash, delete_files=True)
        cancel_text = (
            f"🛑 <b>NAS 离线下载任务已取消</b>\n\n"
            f"🗑️ <i>已从后台下载队列中移除并清理临时缓存。</i>"
        )
        try:
            await callback_query.edit_message_text(
                cancel_text,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.warning(f"[AnimeSearch] 更新取消状态失败: {e}")
        return

    # 2. 一键推送到 NAS 下载 (b:dl:{short_hash})
    if data.startswith("b:dl:"):
        short_hash = data.split(":", 2)[2].lower()
        target_item = None
        for full_hash, it in _hash_to_torrent.items():
            if full_hash.startswith(short_hash):
                target_item = it
                break

        if not target_item:
            for sess in _sessions.values():
                for it in sess.get("items", []):
                    if (it.get("info_hash") or "").startswith(short_hash):
                        target_item = it
                        break
                if target_item:
                    break

        if not target_item or not target_item.get("magnet"):
            await callback_query.answer("⚠️ 未找到对应磁力或信息已过期，请重新搜索", show_alert=True)
            return

        magnet = target_item["magnet"]
        title = target_item.get("title", "动漫资源")
        full_hash = target_item.get("info_hash") or short_hash
        await callback_query.answer("🚀 正在推送到 NAS 下载...")

        res = await push_to_nas_qbt_async(magnet, DEFAULT_SAVE_PATH, "动漫")
        if res.get("status") == "ok":
            if res.get("already_completed"):
                act_path = res.get("content_path") or res.get("save_path") or DEFAULT_SAVE_PATH
                await callback_query.message.reply_text(
                    f"ℹ️ <b>该资源此前已下载完成并存在于 NAS 硬盘中！无需重复下载</b>\n\n"
                    f"🎬 <b>资源名称：</b>{html.escape(res.get('name') or title)}\n"
                    f"📁 <b>实际位置：</b><code>{html.escape(act_path)}</code>\n"
                    f"📦 <b>文件大小：</b>{res.get('total_size_str') or target_item.get('size', '未知')}\n\n"
                    f"⚡ <i>可随时通过飞牛影视或本地播放器直接观看！</i>",
                    parse_mode=ParseMode.HTML
                )
                return

            cancel_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ 取消下载", callback_data=f"b:cancel:{full_hash[:12]}")]
            ]) if full_hash else None

            if res.get("readded"):
                sent_msg = await callback_query.message.reply_text(
                    f"🚀 <b>已自动清理历史残留记录，重新开始 NAS 离线下载！</b>\n\n"
                    f"🎬 <b>资源名称：</b>{html.escape(title)}\n"
                    f"📁 <b>下载路径：</b><code>{html.escape(DEFAULT_SAVE_PATH)}</code>\n"
                    f"📦 <b>预估大小：</b>{target_item.get('size', '未知')}\n"
                    f"🟢 <b>健康做种：</b>{target_item.get('seeders', '0')} 位做种者\n\n"
                    f"⏳ <i>正在连接做种者并拉取实时进度条...</i>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=cancel_kb
                )
            elif res.get("already_exists"):
                act_path = res.get("save_path") or DEFAULT_SAVE_PATH
                sent_msg = await callback_query.message.reply_text(
                    f"ℹ️ <b>资源已在 NAS 下载队列中！</b>\n\n"
                    f"🎬 <b>资源名称：</b>{html.escape(title)}\n"
                    f"📁 <b>下载路径：</b><code>{html.escape(act_path)}</code>\n"
                    f"📦 <b>文件大小：</b>{target_item.get('size', '未知')}\n\n"
                    f"⏳ <i>正在获取实时下载进度...</i>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=cancel_kb
                )
            else:
                sent_msg = await callback_query.message.reply_text(
                    f"🚀 <b>NAS 离线下载任务已建立！</b>\n\n"
                    f"🎬 <b>资源名称：</b>{html.escape(title)}\n"
                    f"📁 <b>下载路径：</b><code>{html.escape(DEFAULT_SAVE_PATH)}</code>\n"
                    f"📦 <b>预估大小：</b>{target_item.get('size', '未知')}\n"
                    f"🟢 <b>健康做种：</b>{target_item.get('seeders', '0')} 位做种者\n\n"
                    f"⏳ <i>正在连接节点并拉取进度条...</i>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=cancel_kb
                )
            if full_hash:
                asyncio.create_task(
                    monitor_qbt_progress(
                        client,
                        sent_msg.chat.id,
                        sent_msg.id,
                        full_hash,
                        title
                    )
                )
        else:
            err = res.get("message", "未知错误")
            await callback_query.message.reply_text(f"❌ 推送失败: {html.escape(err)}", parse_mode=ParseMode.HTML)
        return

    # 2. 单独获取磁链详情与一键复制 (b:cp:{short_hash})
    if data.startswith("b:cp:"):
        short_hash = data.split(":", 2)[2].lower()
        target_item = None
        for full_hash, it in _hash_to_torrent.items():
            if full_hash.startswith(short_hash):
                target_item = it
                break

        if not target_item:
            for sess in _sessions.values():
                for it in sess.get("items", []):
                    if (it.get("info_hash") or "").startswith(short_hash):
                        target_item = it
                        break
                if target_item:
                    break

        if not target_item or not target_item.get("magnet"):
            await callback_query.answer("⚠️ 未找到对应磁力或信息已过期，请重新搜索", show_alert=True)
            return

        magnet = target_item["magnet"]
        title = target_item.get("title", "动漫资源")
        size = target_item.get("size", "未知")
        seeders = target_item.get("seeders", "0")

        await callback_query.answer("📋 磁力链接已发送")

        card_text = (
            f"🎬 <b>{html.escape(title)}</b>\n"
            f"📦 <b>大小：</b>{size} · 🟢 <b>做种：</b>{seeders}\n\n"
            f"🧲 <b>磁力链接 (点击下方代码块即可复制):</b>\n"
            f"<code>{html.escape(magnet)}</code>"
        )
        single_btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 推送到 NAS 下载", callback_data=f"b:dl:{short_hash}")]
        ])
        await callback_query.message.reply_text(
            card_text,
            parse_mode=ParseMode.HTML,
            reply_markup=single_btn,
            disable_web_page_preview=True
        )
        return

    # 2. 翻页 (b:pg:{sid}:{page})
    if data.startswith("b:pg:"):
        parts = data.split(":")
        if len(parts) < 4:
            await callback_query.answer()
            return
        sid = parts[2]
        target_page = int(parts[3])
        session = _sessions.get(sid)

        if not session:
            await callback_query.answer("⚠️ 搜索会话已过期，请重新输入指令搜索", show_alert=True)
            return

        items = session["items"]
        total_subpages = max(1, (len(items) + SUBPAGE_SIZE - 1) // SUBPAGE_SIZE)
        target_page = max(1, min(target_page, total_subpages))

        msg_text = format_anime_message(
            session["platform_name"],
            session["keyword"],
            session["sort_name"],
            items,
            target_page,
            total_subpages
        )
        keyboard = make_anime_keyboard(sid, items, target_page, total_subpages)

        try:
            await callback_query.edit_message_text(
                msg_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
            await callback_query.answer(f"第 {target_page} 页")
        except Exception:
            await callback_query.answer()
        return


def register(bot_instance):
    """注册动漫/BT检索插件到 DownloadBot"""
    global _bot_instance, STEAM_SERVICE_URL, DEFAULT_SAVE_PATH, DEFAULT_PROXY
    _bot_instance = bot_instance
    cfg = getattr(bot_instance, "config", {}) or {}
    p_cfg = cfg.get("plugins", {}).get("anime_bt", {}) if isinstance(cfg, dict) else {}
    STEAM_SERVICE_URL = p_cfg.get("steam_service_url", STEAM_SERVICE_URL)
    DEFAULT_SAVE_PATH = p_cfg.get("save_path", DEFAULT_SAVE_PATH)
    DEFAULT_PROXY = p_cfg.get("default_proxy", DEFAULT_PROXY)

    handlers = []
    whitelist = bot_instance.allowed_user_ids

    # 1. /nyaa 动漫搜索
    async def _nyaa_cmd(client, message):
        await execute_anime_search(client, message, "nyaa")

    h_nyaa = MessageHandler(
        _nyaa_cmd,
        filters=pyrogram.filters.command(["nyaa", "anime", "bangumi"]) & pyrogram.filters.user(whitelist)
    )
    bot_instance.bot.add_handler(h_nyaa)
    handlers.append(h_nyaa)

    # 2. /sukebei R18 搜索
    async def _sukebei_cmd(client, message):
        await execute_anime_search(client, message, "sukebei")

    h_sukebei = MessageHandler(
        _sukebei_cmd,
        filters=pyrogram.filters.command(["sukebei", "nyaa18", "r18"]) & pyrogram.filters.user(whitelist)
    )
    bot_instance.bot.add_handler(h_sukebei)
    handlers.append(h_sukebei)

    # 3. /vcb 蓝光精压搜索
    async def _vcb_cmd(client, message):
        await execute_anime_search(client, message, "vcb")

    h_vcb = MessageHandler(
        _vcb_cmd,
        filters=pyrogram.filters.command(["vcb", "vcbs", "vcb_studio"]) & pyrogram.filters.user(whitelist)
    )
    bot_instance.bot.add_handler(h_vcb)
    handlers.append(h_vcb)

    # 4. 直接发送 magnet 磁链监听器
    h_magnet = MessageHandler(
        handle_direct_magnet,
        filters=pyrogram.filters.regex(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+") & pyrogram.filters.user(whitelist)
    )
    bot_instance.bot.add_handler(h_magnet)
    handlers.append(h_magnet)

    # 5. 直接发送 .torrent 种子文件监听器
    h_torrent = MessageHandler(
        handle_torrent_document,
        filters=pyrogram.filters.document & pyrogram.filters.user(whitelist)
    )
    bot_instance.bot.add_handler(h_torrent)
    handlers.append(h_torrent)

    # 6. 回调按钮监听 (b:...)
    h_cb = CallbackQueryHandler(
        handle_anime_callback,
        filters=pyrogram.filters.regex(r"^b:")
    )
    bot_instance.bot.add_handler(h_cb)
    if hasattr(bot_instance.bot, "dispatcher") and hasattr(bot_instance.bot.dispatcher, "groups"):
        g0 = bot_instance.bot.dispatcher.groups.get(0, [])
        if h_cb in g0:
            g0.remove(h_cb)
            g0.insert(0, h_cb)
    handlers.append(h_cb)

    # 7. /bt 与 /qbt 查看 NAS 下载任务列表
    async def _bt_cmd(client, message):
        await handle_bt_command(client, message)

    h_bt = MessageHandler(
        _bt_cmd,
        filters=pyrogram.filters.command(["bt", "qbt", "torrents"]) & pyrogram.filters.user(whitelist)
    )
    bot_instance.bot.add_handler(h_bt)
    handlers.append(h_bt)

    logger.info(f"Plugin [{PLUGIN_NAME}] v{PLUGIN_VERSION} registered successfully with {len(handlers)} handlers.")
    return handlers
