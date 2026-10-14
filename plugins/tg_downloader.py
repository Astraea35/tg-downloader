import re
import pyrogram
from pyrogram.handlers import MessageHandler
from loguru import logger

PLUGIN_NAME = "Telegram 媒体与链接下载器"
PLUGIN_DESC = "自动识别并下载 Telegram 频道/群组的消息链接与转发媒体"
PLUGIN_VERSION = "2.1"

def register(bot_instance):
    """Registers Telegram media and link handlers."""
    handlers = []
    
    # download_from_link
    if hasattr(bot_instance, "download_from_link"):
        h1 = MessageHandler(
            bot_instance.download_from_link,
            filters=pyrogram.filters.regex(r"^https://t.me.*")
            & pyrogram.filters.user(bot_instance.allowed_user_ids)
        )
        bot_instance.bot.add_handler(h1)
        handlers.append(h1)

    return handlers
