"""
Wallpaper Engine / Steam Workshop Downloader Plugin for Telegram Media Downloader
Supports A+B Dual Mode (SteamCMD official high-speed + fallback), rich metadata cards,
and auto video/preview extraction to NAS date folders.
"""

import os
import re
import time
import json
import asyncio
import urllib.request
import urllib.parse
from loguru import logger
import pyrogram
from pyrogram import filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PLUGIN_NAME = "Wallpaper Engine 创意工坊下载器"
PLUGIN_DESC = "自动识别 Steam 创意工坊壁纸链接，支持 Steam 官方高速下载、壁纸归类与视频提取"
PLUGIN_VERSION = "1.2"

STEAM_SERVICE_URL = "http://192.168.1.34:8192"
_bot_instance = None
_active_wallpaper_tasks = {}

def format_byte(b: float) -> str:
    if b < 1024:
        return f"{b:.1f} B"
    elif b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    elif b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    else:
        return f"{b / (1024 * 1024 * 1024):.2f} GB"

def get_steam_workshop_details(item_id: str, proxy_dict: dict = None) -> dict:
    """Fetch official metadata using the configured NAS network route."""
    api_url = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
    data = urllib.parse.urlencode({
        "itemcount": 1,
        "publishedfileids[0]": str(item_id)
    }).encode('utf-8')

    attempts = []
    if proxy_dict:
        attempts.append(("proxy", proxy_dict, 2.5))
    else:
        attempts.append(("direct", {}, 2.0))

    for mode, p_cfg, to in attempts:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(p_cfg))
            req = urllib.request.Request(
                api_url,
                data=data,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with opener.open(req, timeout=to) as res:
                res_data = json.loads(res.read().decode('utf-8'))
                details = res_data.get('response', {}).get('publishedfiledetails', [{}])[0]
                if details.get('result') == 1:
                    tags = [t.get('tag', '') for t in details.get('tags', []) if t.get('tag')]
                    return {
                        'success': True,
                        'title': details.get('title') or f"Wallpaper_{item_id}",
                        'description': details.get('description', ''),
                        'file_size': int(details.get('file_size', 0)),
                        'preview_url': details.get('preview_url', ''),
                        'app_id': details.get('consumer_app_id', 431960),
                        'tags': tags,
                        'creator': details.get('creator', '')
                    }
        except Exception:
            continue

    return {'success': False, 'title': f"Wallpaper_{item_id}", 'file_size': 0, 'tags': []}

def fetch_preview_image_bytes(preview_url: str, proxy_dict: dict = None) -> bytes:
    """Fetches workshop preview image bytes through NAS proxy with short timeout."""
    if not preview_url:
        return None
    attempts = []
    if proxy_dict:
        attempts.append(proxy_dict)
    else:
        attempts.append({})
    for p_cfg in attempts:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(p_cfg))
            req = urllib.request.Request(
                preview_url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with opener.open(req, timeout=3.0) as res:
                return res.read()
        except Exception:
            continue
    return None

def call_steam_service(endpoint: str, payload: dict = None, method: str = "POST", timeout: int = 60) -> dict:
    url = f"{STEAM_SERVICE_URL.rstrip('/')}{endpoint}"
    req = urllib.request.Request(url, method=method)
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        req.data = data
        req.headers['Content-Type'] = 'application/json'

    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as res:
            return json.loads(res.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8'))
        except Exception:
            return {'status': 'error', 'message': f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {'status': 'error', 'message': f"连接 Steam 辅助服务失败: {e}"}

def extract_workshop_ids(text: str) -> list:
    """Extracts all Steam Workshop item IDs from message text or commands."""
    if not text:
        return []
    ids = []

    # 1. Match steamcommunity.com links with id= anywhere in query string
    for m in re.finditer(r'steamcommunity\.com/(?:sharedfiles|workshop)/filedetails[^\s]*?[?&]id=(\d+)', text):
        item_id = m.group(1)
        if item_id not in ids:
            ids.append(item_id)

    # 2. Match steam://url/CommunityFilePage/123
    for m in re.finditer(r'steam://url/CommunityFilePage/(\d+)', text):
        item_id = m.group(1)
        if item_id not in ids:
            ids.append(item_id)

    # 3. Match commands: /we 12345 67890 or /wallpaper <link/id>
    cmd_m = re.match(r'^/(?:we|wallpaper)\s+(.+)$', text.strip(), re.IGNORECASE)
    if cmd_m:
        raw_parts = cmd_m.group(1).split()
        for p in raw_parts:
            p_clean = p.strip()
            if p_clean.isdigit() and p_clean not in ids:
                ids.append(p_clean)
            else:
                for sid in extract_workshop_ids(p_clean):
                    if sid not in ids:
                        ids.append(sid)

    return ids

def extract_workshop_id(text: str) -> str:
    """Backwards-compatible helper returning first workshop ID."""
    ids = extract_workshop_ids(text)
    return ids[0] if ids else None

async def handle_steam_status(client, message):
    res = call_steam_service("/status", method="GET", timeout=5)
    logged_in = res.get('logged_in', False)
    user = res.get('current_user')

    if logged_in:
        text = (
            f"🎮 **Steam 账号状态：已登录并就绪**\n\n"
            f"👤 当前绑定账号：`{user}`\n"
            f"⚡ 官方 CDN 下载引擎：**正常待命**\n"
            f"📁 下载目录：`downloads/wallpaper_engine/`\n\n"
            f"💡 发送任意创意工坊壁纸链接即可极速下载！"
        )
    else:
        text = (
            f"⚠️ **Steam 账号状态：未登录**\n\n"
            f"由于 Wallpaper Engine (小红车) 属于付费软件，下载工坊壁纸需要已购正版账号授权。\n\n"
            f"📌 **首次绑定登录指令：**\n"
            f"`/steam_login <账号> <密码>`\n\n"
            f"若开启了手机/邮箱验证码（Steam Guard）：\n"
            f"`/steam_login <账号> <密码> <6位验证码>`\n\n"
            f"🔒 *登录成功后系统会自动保存授权凭据，后续无需再输密码。建议在成功后删除包含密码的聊天记录。*"
        )
    await message.reply_text(text)

async def handle_steam_logout(client, message):
    res = call_steam_service("/logout", payload={}, method="POST", timeout=5)
    await message.reply_text(f"🔒 {res.get('message', '已登出 Steam 账号')}")

async def handle_steam_login(client, message):
    parts = message.text.strip().split()
    if len(parts) < 3:
        await message.reply_text(
            "❌ 参数不足！用法示例：\n"
            "`/steam_login <Steam账号> <密码>`\n"
            "或带有验证码：\n"
            "`/steam_login <Steam账号> <密码> <验证码>`"
        )
        return

    username = parts[1]
    password = parts[2]
    code = parts[3] if len(parts) >= 4 else ""

    status_msg = await message.reply_text("⏳ 正在连接 Steam 官方服务器进行认证授权，请稍候...")

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        None,
        lambda: call_steam_service(
            "/login",
            {"username": username, "password": password, "code": code},
            timeout=70
        )
    )

    status = res.get('status')
    msg = res.get('message', '')

    if status == 'ok':
        await status_msg.edit_text(
            f"🎉 **Steam 登录授权成功！**\n\n"
            f"👤 账号：`{username}`\n"
            f"✅ 凭据已持久化保存在 NAS 中，以后无需再输入密码。\n\n"
            f"⚠️ **安全提醒**：请及时在 Telegram 中【删除】刚才包含密码的消息！"
        )
    elif status == 'need_guard':
        await status_msg.edit_text(
            f"🔐 **需要输入 Steam Guard 验证码**\n\n"
            f"Steam 已向您的手机 App 或邮箱发送了验证码。\n"
            f"请带上验证码重新发送指令完成登录：\n"
            f"`/steam_login {username} {password} <验证码>`"
        )
    else:
        await status_msg.edit_text(f"❌ 登录失败：\n{msg}")

def create_progress_bar(progress: int, total_bars: int = 10) -> str:
    """Generates visual progress bar: [██████░░░░]"""
    completed_bars = int(progress * total_bars / 100)
    remaining_bars = total_bars - completed_bars
    return "█" * completed_bars + "░" * remaining_bars

make_progress_bar = create_progress_bar

async def process_single_workshop(client, message, item_id: str):
    """Processes a single workshop item download with live card status."""
    logger.info(f"Processing Wallpaper Engine item {item_id} for user {message.from_user.id}")

    # 1. Fetch Steam Web API metadata (fast timeout, non-blocking fallback)
    proxy_dict = None
    if _bot_instance and getattr(_bot_instance, "app", None):
        proxy_cfg = getattr(_bot_instance.app, "proxy", None)
        if proxy_cfg and proxy_cfg.get("hostname"):
            scheme = proxy_cfg.get("scheme", "http")
            host = proxy_cfg.get("hostname")
            port = proxy_cfg.get("port")
            p_url = f"{scheme}://{host}:{port}"
            proxy_dict = {"http": p_url, "https": p_url}

    loop = asyncio.get_running_loop()
    details = await loop.run_in_executor(None, lambda: get_steam_workshop_details(item_id, proxy_dict))

    title = details.get('title', f"Wallpaper_{item_id}")
    file_size = details.get('file_size', 0)
    size_str = format_byte(file_size) if file_size else "计算中..."
    tags_str = " | ".join(details.get('tags', [])) if details.get('tags') else "Wallpaper"
    preview_url = details.get('preview_url')

    card_text = (
        f"🎨 **【Wallpaper Engine 壁纸信息】**\n\n"
        f"📌 **标题**：{title}\n"
        f"🆔 **工坊 ID**：`{item_id}`\n"
        f"📦 **文件大小**：{size_str}\n"
        f"🏷 **分类标签**：{tags_str}\n\n"
        f"⏳ **正在连接 Steam 官方 CDN 高速通道...**"
    )

    cancel_btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消下载", callback_data=f"wp:cancel:{item_id}")]
    ])

    # 2. Send initial card to Telegram immediately
    status_msg = None
    if preview_url:
        img_bytes = await loop.run_in_executor(None, lambda: fetch_preview_image_bytes(preview_url, proxy_dict))
        if img_bytes:
            try:
                import io
                photo_buf = io.BytesIO(img_bytes)
                photo_buf.name = f"{item_id}.jpg"
                status_msg = await message.reply_photo(photo=photo_buf, caption=card_text, reply_markup=cancel_btn)
            except Exception as e:
                logger.debug(f"reply_photo with BytesIO failed: {e}")
                status_msg = None

    if not status_msg:
        try:
            status_msg = await message.reply_text(card_text, reply_markup=cancel_btn)
        except Exception as e:
            logger.warning(f"reply_text failed: {e}")

    cancel_event = asyncio.Event()
    _active_wallpaper_tasks[item_id] = {
        "cancel_event": cancel_event,
        "status_msg": status_msg,
        "title": title,
    }

    # 3. Submit download task to steam_service
    payload = {
        "item_id": item_id,
        "title": title,
        "total_bytes": file_size,
    }

    init_res = await loop.run_in_executor(
        None,
        lambda: call_steam_service("/download", payload, timeout=30)
    )

    if init_res.get('status') == 'not_logged_in':
        _active_wallpaper_tasks.pop(item_id, None)
        err_text = (
            f"⚠️ **下载未执行：尚未登录 Steam 账号**\n\n"
            f"{init_res.get('message')}\n\n"
            f"💡 **绑定指令**：\n"
            f"`/steam_login <账号> <密码> [验证码]`"
        )
        if status_msg:
            try:
                if status_msg.caption:
                    await status_msg.edit_caption(err_text, reply_markup=None)
                else:
                    await status_msg.edit_text(err_text, reply_markup=None)
            except Exception:
                await message.reply_text(err_text)
        else:
            await message.reply_text(err_text)
        return

    elif init_res.get('status') == 'error':
        _active_wallpaper_tasks.pop(item_id, None)
        err_msg = init_res.get('message', '未知错误')
        err_text = f"❌ **壁纸下载失败**：\n{err_msg}"
        if status_msg:
            try:
                if status_msg.caption:
                    await status_msg.edit_caption(err_text, reply_markup=None)
                else:
                    await status_msg.edit_text(err_text, reply_markup=None)
            except Exception:
                await message.reply_text(err_text)
        else:
            await message.reply_text(err_text)
        return

    last_text = card_text
    last_edit_time = time.time()
    final_res = None
    poll_start = time.time()

    if init_res.get('status') == 'finished':
        final_res = init_res
        _active_wallpaper_tasks.pop(item_id, None)
    else:
        # Dynamic smart polling loop:
        # 1. 'queued' state NEVER times out as long as the service is alive.
        # 2. 'downloading' state refreshes timeout whenever bytes increase or speed > 0.
        # 3. Only triggers timeout if 'downloading' has completely stalled for > 10 minutes (600s).
        last_active_time = time.time()
        last_downloaded_bytes = 0
        consecutive_net_errors = 0
        idle_count = 0

        while True:
            if cancel_event.is_set():
                logger.info(f"Wallpaper task {item_id} cancelled by user.")
                _active_wallpaper_tasks.pop(item_id, None)
                return
            await asyncio.sleep(1.5)
            if cancel_event.is_set():
                logger.info(f"Wallpaper task {item_id} cancelled by user.")
                _active_wallpaper_tasks.pop(item_id, None)
                return
            now = time.time()
            try:
                p_res = await loop.run_in_executor(
                    None,
                    lambda: call_steam_service(f"/progress?item_id={item_id}", method="GET", timeout=5)
                )
                consecutive_net_errors = 0
            except Exception as e:
                consecutive_net_errors += 1
                logger.debug(f"Progress poll error ({consecutive_net_errors}): {e}")
                if consecutive_net_errors >= 40:  # ~60 seconds continuous disconnection
                    final_res = {"status": "error", "message": "无法连接 Steam 辅助服务 (网络通信中断超过60秒)"}
                    break
                continue

            p_status = p_res.get('status')

            if p_status == 'queued':
                # 排队中：后台正在处理前面任务，排队过程绝不超时！
                last_active_time = now
                idle_count = 0
                q_pos = p_res.get('queue_pos', 1)
                queue_text = (
                    f"🎨 **【Wallpaper Engine 壁纸信息】**\n\n"
                    f"📌 **标题**：{title}\n"
                    f"🆔 **工坊 ID**：`{item_id}`\n"
                    f"📦 **文件大小**：{size_str}\n"
                    f"🏷 **分类标签**：{tags_str}\n\n"
                    f"⏳ **排队等待下载中**：前面有壁纸正在下载，已加入队列 (当前第 {q_pos} 位)..."
                )
                if queue_text != last_text and (now - last_edit_time >= 1.5) and status_msg:
                    try:
                        if status_msg.caption:
                            await status_msg.edit_caption(queue_text, reply_markup=cancel_btn)
                        else:
                            await status_msg.edit_text(queue_text, reply_markup=cancel_btn)
                        last_text = queue_text
                        last_edit_time = now
                    except Exception:
                        pass

            elif p_status == 'downloading':
                idle_count = 0
                pct = min(100, max(0, int(float(p_res.get('percent', 0.0)))))
                cur_bytes = int(p_res.get('downloaded_bytes', 0))
                d_str = p_res.get('downloaded_str', '0 B')
                t_str = p_res.get('total_str', size_str)
                spd_str = p_res.get('speed_str', '0 KB/s')
                bar = create_progress_bar(pct, 10)

                # 只要有数据增加或速度大于0，就持续刷新活跃时间
                if cur_bytes > last_downloaded_bytes or cur_bytes == 0:
                    last_active_time = now
                    if cur_bytes > last_downloaded_bytes:
                        last_downloaded_bytes = cur_bytes

                # 仅在真正处于下载中且超过 10 分钟没有 1 字节进展时才判定停滞超时
                if now - last_active_time > 600:
                    final_res = {"status": "error", "message": "下载停滞超时 (已超过10分钟无新数据流)"}
                    break

                clean_display_title = re.sub(r'[\r\n\t]+', ' ', title).strip()
                if len(clean_display_title) > 30:
                    clean_display_title = clean_display_title[:27] + "..."

                progress_text = (
                    f"🎨 **【Wallpaper Engine 壁纸信息】**\n\n"
                    f"📌 **标题**：{title}\n"
                    f"🆔 **工坊 ID**：`{item_id}`\n"
                    f"🏷 **分类标签**：{tags_str}\n\n"
                    f"📥 **Steam 创意工坊壁纸下载中**:\n"
                    f" ├─ 📁 : {clean_display_title}\n"
                    f" ├─ 📏 : {d_str} / {t_str}\n"
                    f" ├─ ⏬ : {spd_str}\n"
                    f" └─ 📊 : [{bar}] ({pct}%)\n"
                )

                if progress_text != last_text and (now - last_edit_time >= 1.5) and status_msg:
                    try:
                        if status_msg.caption:
                            await status_msg.edit_caption(progress_text, reply_markup=cancel_btn)
                        else:
                            await status_msg.edit_text(progress_text, reply_markup=cancel_btn)
                        last_text = progress_text
                        last_edit_time = now
                    except Exception:
                        pass

            elif p_status in ('finished', 'error', 'not_logged_in'):
                final_res = p_res
                break

            elif p_status == 'idle':
                # 任务可能在轮询前已完成，或后端服务经历了短暂重启，进行双重校验
                idle_count += 1
                if idle_count >= 4:
                    recheck = await loop.run_in_executor(
                        None,
                        lambda: call_steam_service("/download", payload, timeout=10)
                    )
                    if recheck.get('status') == 'finished':
                        final_res = recheck
                        break
                    elif idle_count >= 15:
                        final_res = {"status": "error", "message": "任务状态丢失或已取消"}
                        break

    _active_wallpaper_tasks.pop(item_id, None)
    if cancel_event.is_set():
        return

    res = final_res

    if res.get('status') == 'finished' or res.get('status') == 'ok':
        real_title = res.get('real_title') or title
        target_dir = res.get('target_dir', '')
        has_video = res.get('has_video', False)
        final_size_str = res.get('final_size_str') or size_str
        wp_type = res.get('wallpaper_type', 'wallpaper').capitalize()

        type_desc = "🎬 视频壁纸 (Video)" if has_video else f"🎨 场景工程壁纸 ({wp_type})"

        finish_text = (
            f"✅ **【Wallpaper Engine 壁纸下载完成】**\n\n"
            f"📌 **标题**：{real_title}\n"
            f"🆔 **工坊 ID**：`{item_id}`\n"
            f"📦 **实际大小**：{final_size_str}\n"
            f"🏷 **壁纸类型**：{type_desc}\n"
            f"📁 **NAS 保存路径**：\n`{target_dir}`\n\n"
            f"🎮 已保留完整工程包与小红车配置文件 (project.json)，可直接导入使用。"
        )

        if status_msg:
            try:
                if status_msg.caption:
                    await status_msg.edit_caption(finish_text, reply_markup=None)
                else:
                    await status_msg.edit_text(finish_text, reply_markup=None)
            except Exception:
                await message.reply_text(finish_text)
        else:
            await message.reply_text(finish_text)

    elif res.get('status') == 'not_logged_in':
        err_text = (
            f"⚠️ **下载未执行：尚未登录 Steam 账号**\n\n"
            f"{res.get('message')}\n\n"
            f"💡 **绑定指令**：\n"
            f"`/steam_login <账号> <密码> [验证码]`"
        )
        if status_msg:
            try:
                if status_msg.caption:
                    await status_msg.edit_caption(err_text, reply_markup=None)
                else:
                    await status_msg.edit_text(err_text, reply_markup=None)
            except Exception:
                await message.reply_text(err_text)
        else:
            await message.reply_text(err_text)
    else:
        err_msg = res.get('message', '未知错误')
        err_text = f"❌ **壁纸下载失败**：\n{err_msg}"
        if status_msg:
            try:
                if status_msg.caption:
                    await status_msg.edit_caption(err_text, reply_markup=None)
                else:
                    await status_msg.edit_text(err_text, reply_markup=None)
            except Exception:
                await message.reply_text(err_text)
        else:
            await message.reply_text(err_text)


async def handle_workshop_link(client, message):
    text = message.text or message.caption or ""
    item_ids = extract_workshop_ids(text)
    if not item_ids:
        return

    logger.info(f"Received Wallpaper Engine link(s) for {len(item_ids)} item(s): {item_ids} from {message.from_user.id}")

    for item_id in item_ids:
        asyncio.create_task(process_single_workshop(client, message, item_id))


async def on_wallpaper_callback(client: pyrogram.Client, query: pyrogram.types.CallbackQuery):
    """Handles cancellation of wallpaper engine downloads."""
    if not query.data or not query.data.startswith("wp:cancel:"):
        return
    item_id = query.data.split(":", 2)[2]
    task_info = _active_wallpaper_tasks.get(item_id)
    if task_info:
        task_info["cancel_event"].set()
        title = task_info.get("title", f"Wallpaper_{item_id}")
        status_msg = task_info.get("status_msg")
        cancel_text = (
            f"🛑 **【Wallpaper Engine 壁纸下载已取消】**\n\n"
            f"📌 **标题**：{title}\n"
            f"🆔 **工坊 ID**：`{item_id}`\n"
            f"⚠️ 壁纸下载已被用户手动取消。"
        )
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: call_steam_service(f"/cancel?item_id={item_id}", timeout=5))
        except Exception:
            pass

        if status_msg:
            try:
                if status_msg.caption:
                    await status_msg.edit_caption(cancel_text, reply_markup=None)
                else:
                    await status_msg.edit_text(cancel_text, reply_markup=None)
            except Exception:
                pass
        _active_wallpaper_tasks.pop(item_id, None)
        await query.answer("🛑 已取消壁纸下载", show_alert=False)
    else:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer("⚠️ 该壁纸任务已完成或已被取消", show_alert=False)


def register(bot_instance):
    """Registers Wallpaper Engine plugin onto DownloadBot."""
    global _bot_instance, STEAM_SERVICE_URL
    _bot_instance = bot_instance
    cfg = getattr(bot_instance, "config", {}) or {}
    p_cfg = cfg.get("plugins", {}).get("wallpaper", {}) if isinstance(cfg, dict) else {}
    STEAM_SERVICE_URL = p_cfg.get("steam_service_url", STEAM_SERVICE_URL)

    handlers = []
    whitelist = bot_instance.allowed_user_ids

    # 1. /steam_status
    async def _status_handler(client, message):
        await handle_steam_status(client, message)

    h_status = MessageHandler(
        _status_handler,
        filters=pyrogram.filters.command(["steam_status", "we_status"]) & pyrogram.filters.user(whitelist)
    )
    bot_instance.bot.add_handler(h_status)
    handlers.append(h_status)

    # 2. /steam_logout
    async def _logout_handler(client, message):
        await handle_steam_logout(client, message)

    h_logout = MessageHandler(
        _logout_handler,
        filters=pyrogram.filters.command(["steam_logout"]) & pyrogram.filters.user(whitelist)
    )
    bot_instance.bot.add_handler(h_logout)
    handlers.append(h_logout)

    # 3. /steam_login
    async def _login_handler(client, message):
        await handle_steam_login(client, message)

    h_login = MessageHandler(
        _login_handler,
        filters=pyrogram.filters.command(["steam_login"]) & pyrogram.filters.user(whitelist)
    )
    bot_instance.bot.add_handler(h_login)
    handlers.append(h_login)

    # 4. Link & /we listener
    async def _link_handler(client, message):
        await handle_workshop_link(client, message)

    workshop_filter = (
        pyrogram.filters.regex(r'steamcommunity\.com/(?:sharedfiles|workshop)/filedetails') |
        pyrogram.filters.regex(r'steam://url/CommunityFilePage/\d+') |
        pyrogram.filters.command(["wp", "we", "wallpaper"])
    ) & pyrogram.filters.user(whitelist)

    h_link = MessageHandler(_link_handler, filters=workshop_filter)
    bot_instance.bot.add_handler(h_link)
    handlers.append(h_link)

    # 5. Cancel callback listener
    h_cb = CallbackQueryHandler(
        on_wallpaper_callback,
        filters=pyrogram.filters.regex(r"^wp:cancel:") & pyrogram.filters.user(whitelist)
    )
    bot_instance.bot.add_handler(h_cb)
    handlers.append(h_cb)

    logger.info(f"Plugin [{PLUGIN_NAME}] v{PLUGIN_VERSION} registered successfully with {len(handlers)} handlers.")
    return handlers

