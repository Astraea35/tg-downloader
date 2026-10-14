"""Forward personal Telegram features to AstrBot while downloads stay on the NAS."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
import urllib.request
import uuid
from collections import OrderedDict
from pathlib import Path

import pyrogram
from loguru import logger
from pyrogram.handlers import MessageHandler

PLUGIN_NAME = "AstrBot 私人插件联动"
PLUGIN_DESC = "NAS 保留下载，个人插件由本机 AstrBot 提供，文字结果更新原消息"
PLUGIN_VERSION = "1.0.4"

NAS_COMMANDS = {
    "start",
    "help",
    "download",
    "forward",
    "plugins",
    "reload",
    "restart",
    "listen_forward",
    "get_info",
    "set_language",
    "add_filter",
    "stop",
    "forward_to_comments",
    "pan",
    "nyaa",
    "anime",
    "bangumi",
    "sukebei",
    "nyaa18",
    "r18",
    "vcb",
    "vcbs",
    "vcb_studio",
    "bt",
    "qbt",
    "torrents",
    "wp",
    "we",
    "wallpaper",
    "steam_status",
    "we_status",
    "steam_login",
    "steam_logout",
    "save",
    "save_sticker",
    "save_mode",
}
COMMAND_ALIASES = {
    "bili": "总结",
    "bili_list": "订阅列表",
    "news": "日报",
    "daily_news": "日报",
    "ribao": "日报",
    "fortune": "今日运势",
    "upscale": "升图",
    "image": "生图",
    "latest": "最新视频",
    "subscribe": "订阅",
    "unsubscribe": "取消订阅",
    "status": "status",
    "invert": "反色",
    "flip": "左右翻转",
    "gif": "转GIF",
    "mc": "mc成就",
    "mc_challenge": "mc挑战",
    "music": "点歌",
    "qqmusic": "QQ点歌",
    "lyrics": "查歌词",
}
REMOVED_COMMANDS = {
    "ocr", "识别图片", "convert", "表情转换", "pig", "今日小猪",
    "eat", "吃什么", "reminder_add", "添加提醒", "task_add", "添加任务",
    "reminder_run", "立即提醒", "task_run", "立即执行",
    "reminder_delete", "删除提醒", "task_delete", "删除任务",
}
DOWNLOAD_LINK = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me/|twitter\.com/|x\.com/|"
    r"steamcommunity\.com/(?:sharedfiles|workshop)/)|steam://|magnet:\?",
    re.IGNORECASE,
)

_bot_instance = None
_loop = None
_states: OrderedDict[str, dict] = OrderedDict()
_delivered: OrderedDict[str, float] = OrderedDict()
_input_root: Path | None = None


def config() -> dict:
    return (
        _bot_instance.config.get("plugins", {}).get("astrbot_bridge", {})
        if _bot_instance
        else {}
    )


def should_forward(text: str, has_media: bool = False) -> bool:
    text = (text or "").strip()
    if DOWNLOAD_LINK.search(text):
        return False
    if text.startswith("/"):
        command = text[1:].split(maxsplit=1)[0].split("@", 1)[0].lower()
        return command not in NAS_COMMANDS
    return bool(text) and not has_media


def normalize_command(text: str) -> str:
    if not text.startswith("/"):
        return text
    first, *rest = text.split(maxsplit=1)
    command = first[1:].split("@", 1)[0]
    translated = COMMAND_ALIASES.get(command.lower(), command)
    return "/" + translated + (" " + rest[0] if rest else "")


def split_text(text: str, limit: int = 3500) -> list[str]:
    """Split by UTF-16 units so emoji cannot exceed Telegram's message limit."""
    chunks: list[str] = []
    current = ""
    units = 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            split_at = current.rfind("\n")
            if split_at >= len(current) // 2:
                chunks.append(current[: split_at + 1])
                current = current[split_at + 1 :]
                units = len(current.encode("utf-16-le")) // 2
            else:
                chunks.append(current)
                current = ""
                units = 0
        current += char
        units += size
    if current:
        chunks.append(current)
    return chunks


def telegram_markdown(text: str) -> str:
    """Space headings and list entries for reading on a narrow Telegram screen."""
    lines = []
    in_code = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        if not in_code:
            line = re.sub(r"^#{1,6}\s+(.+)$", r"**\1**", line)
            line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
            if re.fullmatch(r"\*\*.+\*\*", line) or re.match(r"^\s*(?:\d+[.)]\s+|• )", line):
                if lines and lines[-1]:
                    lines.append("")
                lines.extend((line, ""))
                continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _http_json(url: str, payload: dict, token: str, timeout: int = 60) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "X-Bridge-Token": token},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("AstrBot rejected the bridge request")
    return result


async def _capture_media(client, message) -> list[dict]:
    if not message or _input_root is None:
        return []
    media_type = (
        "image"
        if message.photo
        else "image"
        if message.sticker
        else "image"
        if message.animation
        else "video"
        if message.video
        else "record"
        if message.voice or message.audio
        else "file"
        if message.document
        else ""
    )
    if not media_type:
        return []
    media = (
        message.photo
        or message.sticker
        or message.animation
        or message.video
        or message.voice
        or message.audio
        or message.document
    )
    if int(getattr(media, "file_size", 0) or 0) > 100 * 1024 * 1024:
        raise ValueError("用于插件处理的附件超过 100 MB")
    extension = (
        ".jpg"
        if message.photo
        else Path(getattr(message.sticker, "file_name", "") or "").suffix or ".webp"
        if message.sticker
        else Path(getattr(message.animation, "file_name", "") or "").suffix or ".mp4"
        if message.animation
        else Path(getattr(media, "file_name", "") or "").suffix
        or (
            ".mp4"
            if media_type == "video"
            else ".ogg"
            if media_type == "record"
            else ".bin"
        )
    )
    filename = uuid.uuid4().hex + extension[:12]
    destination = _input_root / filename
    await client.download_media(message, file_name=str(destination))
    return [
        {
            "type": media_type,
            "filename": filename,
            "name": getattr(media, "file_name", "") or filename,
        }
    ]


async def forward_message(client, message) -> None:
    text = message.text or message.caption or ""
    if text.startswith("/"):
        command = text[1:].split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command in REMOVED_COMMANDS:
            await message.reply_text("该功能未在 TG 启用。", quote=True)
            message.stop_propagation()
            return
    settings = config()
    request_id = f"{message.chat.id}-{message.id}"
    if request_id in _states:
        message.stop_propagation()
        return
    reply = await message.reply_text("正在处理…", quote=True)
    state = {
        "chat_id": str(message.chat.id),
        "status_id": reply.id,
        "pages": [],
        "result_text": "",
        "done": False,
        "created": time.monotonic(),
        "lock": asyncio.Lock(),
    }
    _states[request_id] = state
    while len(_states) > 1000:
        _states.popitem(last=False)
    try:
        payload = {
            "request_id": request_id,
            "message_id": str(message.id),
            "chat_id": str(message.chat.id),
            "chat_type": "private",
            "sender_id": str(message.from_user.id),
            "sender_name": message.from_user.first_name or "",
            "text": normalize_command(message.text or message.caption or ""),
            "media": await _capture_media(client, message),
        }
        if original := message.reply_to_message:
            payload["reply"] = {
                "message_id": str(original.id),
                "sender_id": str(original.from_user.id) if original.from_user else "",
                "sender_name": original.from_user.first_name
                if original.from_user
                else "",
                "text": original.text or original.caption or "",
                "media": await _capture_media(client, original),
            }
        await asyncio.to_thread(
            _http_json,
            settings["astrbot_url"].rstrip("/") + "/message",
            payload,
            settings["shared_secret"],
        )
    except Exception as error:
        state["done"] = True
        await client.edit_message_text(
            message.chat.id,
            reply.id,
            "本机 AstrBot 暂时不可用，NAS 下载功能仍可使用。"
            if not isinstance(error, ValueError)
            else str(error),
        )
        logger.warning("AstrBot bridge request failed: {}", type(error).__name__)
    message.stop_propagation()


async def _edit_text(client, chat_id: int, message_id: int, text: str) -> None:
    try:
        await client.edit_message_text(
            chat_id,
            message_id,
            text,
            parse_mode=pyrogram.enums.ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
    except pyrogram.errors.MessageNotModified:
        pass
    except pyrogram.errors.BadRequest:
        await client.edit_message_text(
            chat_id,
            message_id,
            text,
            parse_mode=pyrogram.enums.ParseMode.DISABLED,
            disable_web_page_preview=True,
        )


async def _update_text(client, chat_id: int, state: dict, text: str) -> None:
    chunks = split_text(telegram_markdown(text))
    if not chunks:
        return
    try:
        await _edit_text(client, chat_id, state["status_id"], chunks[0])
    except pyrogram.errors.MessageIdInvalid:
        replacement = await client.send_message(
            chat_id, chunks[0], parse_mode=pyrogram.enums.ParseMode.DISABLED,
            disable_web_page_preview=True,
        )
        state["status_id"] = replacement.id
    for index, chunk in enumerate(chunks[1:]):
        if index < len(state["pages"]):
            await _edit_text(client, chat_id, state["pages"][index], chunk)
        else:
            try:
                result = await client.send_message(
                    chat_id,
                    chunk,
                    parse_mode=pyrogram.enums.ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
            except pyrogram.errors.BadRequest:
                result = await client.send_message(
                    chat_id,
                    chunk,
                    parse_mode=pyrogram.enums.ParseMode.DISABLED,
                    disable_web_page_preview=True,
                )
            state["pages"].append(result.id)
    # Old pages remain readable; they are marked when a shorter result replaces them.
    for extra in state["pages"][max(0, len(chunks) - 1) :]:
        await _edit_text(client, chat_id, extra, "此页内容已更新，请查看上方结果。")


def _with_saved_paths(text: str, state: dict) -> str:
    paths = state.get("saved_paths", [])
    if not paths:
        return text
    locations = "\n".join(f"`{path}`" for path in paths)
    return (text + "\n\n" if text else "") + "**已保存到 NAS**\n" + locations


def _download_asset(asset: dict, destination: Path) -> None:
    settings = config()
    asset_id = str(asset.get("asset_id", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", asset_id):
        raise ValueError("Invalid media asset ID")
    request = urllib.request.Request(
        settings["astrbot_url"].rstrip("/") + "/files/" + asset_id,
        headers={"X-Bridge-Token": settings["shared_secret"]},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with (
        opener.open(request, timeout=180) as response,
        destination.open("wb") as output,
    ):
        size = 0
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > 2 * 1024 * 1024 * 1024:
                raise ValueError("Bridge media exceeds the Telegram upload limit")
            output.write(block)


async def receive_push(payload: dict) -> dict:
    chat_id = int(payload["chat_id"])
    whitelist = {str(uid) for uid in _bot_instance.allowed_user_ids}
    if str(chat_id) not in whitelist:
        raise ValueError("Delivery target is outside the whitelist")
    request_id = str(payload.get("request_id", ""))
    state = _states.get(request_id) if request_id else None
    if request_id and (not state or state["chat_id"] != str(chat_id)):
        raise ValueError("Unknown Telegram request")
    delivery_id = str(payload.get("delivery_id", ""))
    if delivery_id and delivery_id in _delivered:
        return {"ok": True, "duplicate": True}
    client = _bot_instance.bot
    kind = payload.get("kind", "result")
    if kind == "typing":
        await client.send_chat_action(chat_id, pyrogram.enums.ChatAction.TYPING)
        return {"ok": True}
    if kind == "complete":
        if state:
            state["done"] = True
            if not payload.get("had_output"):
                await _update_text(
                    client,
                    chat_id,
                    state,
                    "该消息没有触发已启用的私人插件，可发送 /help 查看命令。",
                )
        return {"ok": True}
    parts = payload.get("parts", [])
    text = "\n\n".join(
        str(part.get("text", "")) for part in parts if part.get("type") == "text"
    ).strip()
    media_parts = [part for part in parts if part.get("type") != "text"]
    if state is None:
        message = await client.send_message(chat_id, "正在发送…")
        state = {
            "status_id": message.id,
            "pages": [],
            "lock": asyncio.Lock(),
            "result_text": "",
        }
    async with state["lock"]:
        if text:
            if kind != "status":
                state["result_text"] = text
            await _update_text(client, chat_id, state, _with_saved_paths(text, state))
        saved_paths = []
        for media in media_parts:
            name = Path(str(media.get("name", "file")).replace("\\", "/")).name
            if name in {"", ".", ".."}:
                name = "attachment.bin"
            target = (
                Path(_bot_instance.app.save_path) / "astrbot" / uuid.uuid4().hex / name
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(_download_asset, media, target)
            host_root = Path(
                config().get("nas_download_path", _bot_instance.app.save_path)
            )
            saved_paths.append(
                str(host_root / target.relative_to(Path(_bot_instance.app.save_path))),
            )
            media_type = media.get("type")
            if media_type == "image":
                await client.send_photo(chat_id, str(target))
            elif media_type == "animation":
                await client.send_animation(chat_id, str(target))
            elif media_type == "video":
                await client.send_video(chat_id, str(target))
            elif media_type == "record":
                await client.send_audio(chat_id, str(target))
            elif media_type == "file":
                await client.send_document(chat_id, str(target))
            else:
                raise ValueError("Unsupported media type")
        if media_parts:
            body = text or state.get("result_text", "")
            if re.search(r"正在|请稍候|请稍等|已开始生图任务", body):
                body = ""
            state.setdefault("saved_paths", []).extend(saved_paths)
            await _update_text(
                client,
                chat_id,
                state,
                _with_saved_paths(body, state),
            )
    if delivery_id:
        _delivered[delivery_id] = time.monotonic()
        while len(_delivered) > 2000:
            _delivered.popitem(last=False)
    return {"ok": True, "message_id": state["status_id"]}


def handle_web_push(payload: dict, token: str) -> dict:
    settings = config()
    secret = str(settings.get("shared_secret", ""))
    if len(secret) < 32 or not hmac.compare_digest(token, secret):
        raise PermissionError("Invalid bridge token")
    if _loop is None or not isinstance(payload, dict):
        raise ValueError("Bridge is not initialized")
    future = asyncio.run_coroutine_threadsafe(receive_push(payload), _loop)
    return future.result(timeout=240)


def register(bot_instance):
    global _bot_instance, _loop, _input_root
    _bot_instance = bot_instance
    settings = config()
    if not settings.get("enabled", False):
        return []
    if len(str(settings.get("shared_secret", ""))) < 32 or not settings.get(
        "astrbot_url"
    ):
        raise ValueError("Configure the AstrBot URL and a shared secret first")
    _loop = asyncio.get_running_loop()
    _input_root = Path(bot_instance.app.save_path) / ".astrbot_bridge" / "input"
    _input_root.mkdir(parents=True, exist_ok=True)

    async def match(_, client, message):
        return should_forward(
            message.text or message.caption or "",
            bool(message.media),
        )

    handler = MessageHandler(
        forward_message,
        filters=pyrogram.filters.create(match)
        & pyrogram.filters.private
        & pyrogram.filters.user(bot_instance.allowed_user_ids),
    )
    bot_instance.bot.add_handler(handler, group=-2)
    handler.bridge_group = -2
    return [handler]
