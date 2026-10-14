import os
import re
import shutil
import asyncio
from loguru import logger
from PIL import Image, ImageSequence
import pyrogram
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PLUGIN_NAME = "表情包转 GIF 导出器"
PLUGIN_DESC = "接收表情包转为 GIF 文件发回，默认不占 NAS 硬盘，支持指令与按键按需保存"
PLUGIN_VERSION = "2.2"

# Default setting: False (do NOT save to NAS automatically)
AUTO_SAVE_TO_NAS = False
_save_base_path = "/app/download"

def convert_webp_to_gif(webp_path: str, gif_path: str) -> bool:
    try:
        with Image.open(webp_path) as im:
            frames = []
            durations = []
            for frame in ImageSequence.Iterator(im):
                rgba_frame = frame.convert('RGBA')
                alpha = rgba_frame.split()[-1]
                p_frame = rgba_frame.convert('P', palette=Image.ADAPTIVE)
                mask = Image.eval(alpha, lambda a: 255 if a < 128 else 0)
                p_frame.paste(255, mask)
                frames.append(p_frame)
                durations.append(frame.info.get('duration', 100))
                
            if len(frames) > 1:
                frames[0].save(
                    gif_path,
                    'GIF',
                    save_all=True,
                    append_images=frames[1:],
                    duration=durations,
                    loop=0,
                    transparency=255,
                    disposal=2
                )
            else:
                frames[0].save(gif_path, 'GIF', transparency=255)
        return os.path.exists(gif_path) and os.path.getsize(gif_path) > 0
    except Exception as e:
        logger.error(f"Failed to convert WEBP to GIF: {e}")
        return False

async def handle_sticker_message(client: pyrogram.Client, message: pyrogram.types.Message):
    if not message.sticker:
        return

    tmp_dir = "/tmp/stickers"
    os.makedirs(tmp_dir, exist_ok=True)

    status_msg = await client.send_message(
        message.chat.id,
        "🎨 **正在将表情包转换为原画 GIF...**",
        reply_to_message_id=message.id,
    )

    try:
        file_unique_id = message.sticker.file_unique_id
        emoji = message.sticker.emoji or ""

        tmp_webp = os.path.join(tmp_dir, f"{file_unique_id}.webp")
        downloaded_path = await client.download_media(message, file_name=tmp_webp)

        if not downloaded_path or not os.path.exists(downloaded_path):
            await status_msg.edit_text("❌ 表情包接收失败，请重试。")
            return

        is_video = getattr(message.sticker, "is_video", False)
        is_animated = getattr(message.sticker, "is_animated", False)

        send_file_path = downloaded_path
        clean_emoji = re.sub(r'[\\/*?:"<>|\s]', "_", emoji) if emoji else "emoji"
        doc_name = f"sticker_{clean_emoji}_{file_unique_id[:8]}.webp"
        format_label = "WEBP"

        if is_video:
            format_label = "WEBM (视频表情)"
            doc_name = f"sticker_{clean_emoji}_{file_unique_id[:8]}.webm"
        elif is_animated:
            format_label = "TGS (矢量动图)"
            doc_name = f"sticker_{clean_emoji}_{file_unique_id[:8]}.tgs"
        else:
            tmp_gif = os.path.join(tmp_dir, f"{file_unique_id}.gif")
            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(
                None, convert_webp_to_gif, downloaded_path, tmp_gif
            )
            if success:
                send_file_path = tmp_gif
                doc_name = f"sticker_{clean_emoji}_{file_unique_id[:8]}.gif"
                format_label = "WEBP ➔ GIF (原画动图)"

        # Check if auto-save is enabled
        saved_to_nas = False
        if AUTO_SAVE_TO_NAS:
            nas_dir = os.path.join(_save_base_path, "stickers")
            os.makedirs(nas_dir, exist_ok=True)
            nas_dest = os.path.join(nas_dir, doc_name)
            shutil.copy2(send_file_path, nas_dest)
            saved_to_nas = True

        if saved_to_nas:
            caption = (
                f"✅ **表情包已转换并保存至飞牛 NAS！**\n\n"
                f"🎨 **对应 Emoji**: {emoji or '无'}\n"
                f"🖼 **格式**: `{format_label}`\n"
                f"📁 **飞牛路径**:\n`Docker/tg-downloader/downloads/stickers/{doc_name}`"
            )
            keyboard = None
        else:
            caption = (
                f"✅ **表情包已转换导出！**\n\n"
                f"🎨 **对应 Emoji**: {emoji or '无'}\n"
                f"🖼 **格式**: `{format_label}`\n"
                f"💡 *状态: 默认未存入 NAS，点击下方按键或回复 /save 可永久存入飞牛*"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 保存此表情到 NAS 硬盘", callback_data=f"save_stk:{file_unique_id}:{doc_name}")]
            ])

        await client.send_document(
            chat_id=message.chat.id,
            document=send_file_path,
            file_name=doc_name,
            caption=caption,
            reply_markup=keyboard,
            reply_to_message_id=message.id,
        )

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Error handling sticker: {e}")
        await status_msg.edit_text(f"❌ 表情包处理失败: {e}")

async def on_save_callback(client: pyrogram.Client, callback_query: pyrogram.types.CallbackQuery):
    """Handles inline button click to save sticker to NAS."""
    data = callback_query.data
    parts = data.split(":")
    if len(parts) < 3:
        await callback_query.answer("⚠️ 参数解析错误", show_alert=True)
        return

    file_unique_id = parts[1]
    doc_name = parts[2]

    tmp_dir = "/tmp/stickers"
    src_file = os.path.join(tmp_dir, f"{file_unique_id}.gif")
    if not os.path.exists(src_file):
        src_file = os.path.join(tmp_dir, f"{file_unique_id}.webp")

    if not os.path.exists(src_file):
        await callback_query.answer("⚠️ 临时文件已过期，请重新发送表情包保存。", show_alert=True)
        return

    nas_dir = os.path.join(_save_base_path, "stickers")
    os.makedirs(nas_dir, exist_ok=True)
    nas_dest = os.path.join(nas_dir, doc_name)
    shutil.copy2(src_file, nas_dest)

    await callback_query.answer("✅ 已成功保存到飞牛 NAS 硬盘！", show_alert=True)
    await callback_query.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 已保存至飞牛 NAS 硬盘", callback_data="saved_done")]
        ])
    )

async def on_save_cmd(client: pyrogram.Client, message: pyrogram.types.Message):
    """Command /save to save replied sticker/document to NAS."""
    if not message.reply_to_message:
        await client.send_message(
            message.chat.id,
            "💡 **使用方法**：请直接【回复】想要保存的表情包消息，发送 `/save` 即可永久存入飞牛 NAS！",
            reply_to_message_id=message.id
        )
        return

    target = message.reply_to_message
    nas_dir = os.path.join(_save_base_path, "stickers")
    os.makedirs(nas_dir, exist_ok=True)
    tmp_dir = "/tmp/stickers"

    status = await client.send_message(message.chat.id, "💾 正在保存至飞牛 NAS...", reply_to_message_id=message.id)

    if target.sticker:
        uid = target.sticker.file_unique_id
        src = os.path.join(tmp_dir, f"{uid}.gif")
        if not os.path.exists(src):
            src = os.path.join(tmp_dir, f"{uid}.webp")
            if not os.path.exists(src):
                await client.download_media(target, file_name=src)

        dest = os.path.join(nas_dir, f"sticker_{uid[:8]}.gif")
        if src.endswith(".webp"):
            convert_webp_to_gif(src, dest)
        else:
            shutil.copy2(src, dest)

        await status.edit_text(
            f"✅ **表情包已成功保存到飞牛 NAS！**\n📁 路径: `Docker/tg-downloader/downloads/stickers/{os.path.basename(dest)}`"
        )
        return

    if target.document:
        doc_dest = os.path.join(nas_dir, target.document.file_name or "sticker.gif")
        await client.download_media(target, file_name=doc_dest)
        await status.edit_text(
            f"✅ **文件已成功保存到飞牛 NAS！**\n📁 路径: `Docker/tg-downloader/downloads/stickers/{os.path.basename(doc_dest)}`"
        )
        return

    await status.edit_text("⚠️ 所回复的消息不是表情包或动图文件。")

async def on_save_mode_cmd(client: pyrogram.Client, message: pyrogram.types.Message):
    """Command /save_mode on|off to toggle default saving behavior."""
    global AUTO_SAVE_TO_NAS
    args = message.text.split()
    if len(args) > 1 and args[1].lower() in ("on", "1", "true", "开启"):
        AUTO_SAVE_TO_NAS = True
        await client.send_message(
            message.chat.id,
            "⚙️ **表情包自动保存模式已【开启】**：后续发送表情将默认存入飞牛 NAS 硬盘。",
            reply_to_message_id=message.id
        )
    elif len(args) > 1 and args[1].lower() in ("off", "0", "false", "关闭"):
        AUTO_SAVE_TO_NAS = False
        await client.send_message(
            message.chat.id,
            "⚙️ **表情包自动保存模式已【关闭】**：后续发送表情仅转为 GIF 文件发回，不占飞牛 NAS 硬盘空间。",
            reply_to_message_id=message.id
        )
    else:
        state = "【开启】" if AUTO_SAVE_TO_NAS else "【关闭 (默认)】"
        await client.send_message(
            message.chat.id,
            f"⚙️ **当前表情包自动保存状态**: {state}\n\n"
            f"• 发送 `/save_mode on` 开启默认存入 NAS\n"
            f"• 发送 `/save_mode off` 关闭默认存入 NAS",
            reply_to_message_id=message.id
        )

def register(bot_instance):
    """Registers Sticker plugin onto DownloadBot with high priority."""
    global _save_base_path
    _save_base_path = bot_instance.app.save_path
    handlers = []

    # 1. Sticker receiver (group=-1)
    async def _sticker_handler(client, message):
        await handle_sticker_message(client, message)

    h_stk = MessageHandler(
        _sticker_handler,
        filters=pyrogram.filters.sticker & pyrogram.filters.user(bot_instance.allowed_user_ids)
    )
    bot_instance.bot.add_handler(h_stk, group=-1)
    handlers.append(h_stk)

    # 2. Callback button handler
    h_cb = CallbackQueryHandler(
        on_save_callback,
        filters=pyrogram.filters.regex(r"^save_stk:") & pyrogram.filters.user(bot_instance.allowed_user_ids)
    )
    bot_instance.bot.add_handler(h_cb)
    if hasattr(bot_instance.bot, "dispatcher") and hasattr(bot_instance.bot.dispatcher, "groups"):
        g0 = bot_instance.bot.dispatcher.groups.get(0, [])
        if h_cb in g0:
            g0.remove(h_cb)
            g0.insert(0, h_cb)
    handlers.append(h_cb)

    # 3. /save command handler
    h_save = MessageHandler(
        on_save_cmd,
        filters=pyrogram.filters.command(["save", "save_sticker"]) & pyrogram.filters.user(bot_instance.allowed_user_ids)
    )
    bot_instance.bot.add_handler(h_save)
    handlers.append(h_save)

    # 4. /save_mode command handler
    h_mode = MessageHandler(
        on_save_mode_cmd,
        filters=pyrogram.filters.command(["save_mode"]) & pyrogram.filters.user(bot_instance.allowed_user_ids)
    )
    bot_instance.bot.add_handler(h_mode)
    handlers.append(h_mode)

    return handlers
