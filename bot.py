import importlib.util
import importlib
import json
import urllib.request
import urllib.parse
"""Bot for media downloader"""

import asyncio
import os
from datetime import datetime
from typing import Callable, List, Union

import pyrogram
from loguru import logger
from pyrogram import types
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from ruamel import yaml

import utils
from module.app import (
    Application,
    ChatDownloadConfig,
    ForwardStatus,
    QueryHandler,
    QueryHandlerStr,
    TaskNode,
    TaskType,
    UploadStatus,
)
from module.filter import Filter
from module.get_chat_history_v2 import get_chat_history_v2
from module.language import Language, _t
from module.pyrogram_extension import (
    check_user_permission,
    parse_link,
    proc_cache_forward,
    report_bot_forward_status,
    report_bot_status,
    retry,
    set_max_concurrent_transmissions,
    set_meta_data,
    upload_telegram_chat_message,
)
from utils.format import replace_date_time, validate_title
from utils.meta_data import MetaData

# pylint: disable = C0301, R0902


class DownloadBot:
    """Download bot"""

    def __init__(self):
        self.bot = None
        self.client = None
        self.add_download_task: Callable = None
        self.download_chat_task: Callable = None
        self.app = None
        self.listen_forward_chat: dict = {}
        self.config: dict = {}
        self._yaml = yaml.YAML()
        self.config_path = os.getenv("CONFIG_PATH", os.path.join(os.path.abspath("."), "config.yaml"))
        if not os.path.exists(self.config_path) and os.path.exists(os.path.join(os.path.abspath("."), "bot.yaml")):
            self.config_path = os.path.join(os.path.abspath("."), "bot.yaml")
        self.download_command: dict = {}
        self.filter = Filter()
        self.bot_info = None
        self.task_node: dict = {}
        self.is_running = True
        self.allowed_user_ids: List[Union[int, str]] = []

        meta = MetaData(datetime(2022, 8, 5, 14, 35, 12), 0, "", 0, 0, 0, "", 0)
        self.filter.set_meta_data(meta)

        self.download_filter: List[str] = []
        self.task_id: int = 0
        self.reply_task = None

    def gen_task_id(self) -> int:
        """Gen task id"""
        self.task_id += 1
        return self.task_id

    def add_task_node(self, node: TaskNode):
        """Add task node"""
        self.task_node[node.task_id] = node

    def remove_task_node(self, task_id: int):
        """Remove task node"""
        self.task_node.pop(task_id)

    def stop_task(self, task_id: str):
        """Stop task"""
        if task_id == "all":
            for value in self.task_node.values():
                value.stop_transmission()
        else:
            try:
                task = self.task_node.get(int(task_id))
                if task:
                    task.stop_transmission()
            except Exception:
                return

    async def update_reply_message(self):
        """Update reply message"""
        while self.is_running:
            for key, value in self.task_node.copy().items():
                if value.is_running and not value.is_finish():
                    await report_bot_status(self.bot, value)

            for key, value in self.task_node.copy().items():
                if value.is_running and value.is_finish():
                    success = await report_bot_status(self.bot, value, immediate_reply=True)
                    retry_count = getattr(value, "_finish_retries", 0)
                    if success or getattr(value, "is_finished_reported", False) or retry_count >= 5:
                        self.remove_task_node(key)
                    else:
                        value._finish_retries = retry_count + 1
            await asyncio.sleep(1)


    def load_plugins(self):
        """Dynamically loads plugins from module/plugins/ directory."""
        self.plugins = getattr(self, "plugins", {})
        plugins_dir = os.path.join(os.path.dirname(__file__), "plugins")
        if not os.path.exists(plugins_dir):
            os.makedirs(plugins_dir, exist_ok=True)

        for filename in sorted(os.listdir(plugins_dir)):
            if filename.endswith(".py") and not filename.startswith("_"):
                plugin_id = filename[:-3]
                plugin_cfg = self.get_plugin_config(plugin_id)
                if plugin_cfg.get("enabled") is False:
                    continue
                plugin_path = os.path.join(plugins_dir, filename)
                try:
                    spec = importlib.util.spec_from_file_location(f"module.plugins.{plugin_id}", plugin_path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    handlers = []
                    if hasattr(module, "register"):
                        handlers = module.register(self) or []

                    self.plugins[plugin_id] = {
                        "name": getattr(module, "PLUGIN_NAME", plugin_id),
                        "desc": getattr(module, "PLUGIN_DESC", "无描述"),
                        "version": getattr(module, "PLUGIN_VERSION", "1.0"),
                        "handlers": handlers,
                        "module": module,
                    }
                    logger.info(f"Loaded plugin [{self.plugins[plugin_id]['name']}] from {filename}")
                except Exception as e:
                    logger.error(f"Failed to load plugin {filename}: {e}")

    def reload_plugins(self):
        """Unregisters old handlers and hot-reloads all plugins."""
        if hasattr(self, "plugins"):
            for p_id, p_info in self.plugins.items():
                for handler in p_info.get("handlers", []):
                    try:
                        self.bot.remove_handler(
                            handler, group=getattr(handler, "bridge_group", 0),
                        )
                    except Exception as e:
                        logger.debug(f"Error removing handler: {e}")
        self.plugins = {}
        self.load_plugins()
        return len(self.plugins)

    def get_bot_commands(self) -> List[types.BotCommand]:
        """Returns all available bot commands for Telegram menu."""
        return [
            types.BotCommand("start", "👋 欢迎与核心功能指南"),
            types.BotCommand("help", "📋 显示所有功能与可用命令帮助"),
            types.BotCommand("bili", "📺 B站视频/动态总结 (直接发链接或 /bili)"),
            types.BotCommand("bili_list", "📋 查看当前会话的B站订阅"),
            types.BotCommand("pan", "🔍 聚合网盘搜索 (秒解4K/夸克/阿里/百度等)"),
            types.BotCommand("nyaa", "🎬 Nyaa 动漫检索并自动推送到 NAS 离线"),
            types.BotCommand("wp", "🖼️ Wallpaper Engine 壁纸下载提取到 NAS"),
            types.BotCommand("bt", "📊 查看 NAS 当前 BT 任务与实时下载进度"),
            types.BotCommand("sukebei", "🔞 Sukebei R18 绅士里区磁力检索下载"),
            types.BotCommand("vcb", "💿 VCB-Studio 蓝光精压原盘合集检索"),
            types.BotCommand("save", "🎭 贴纸表情包保存为图片/动图"),
            types.BotCommand("save_mode", "🔄 切换贴纸保存格式 (WebP/GIF/PNG)"),
            types.BotCommand("ainews", "📰 每日 AI 资讯早报 (大模型提炼核心)"),
            types.BotCommand("news", "🗞️ 真寻综合早报 (一言/摸鱼/微语/热搜)"),
            types.BotCommand("fortune", "今日运势"),
            types.BotCommand("image", "生成图片"),
            types.BotCommand("upscale", "回复图片进行AI升图"),
            types.BotCommand("latest", "查询UP主最新视频并总结"),
            types.BotCommand("subscribe", "订阅B站UP主"),
            types.BotCommand("unsubscribe", "取消B站UP主订阅"),
            types.BotCommand("status", "/状态 - 查看本机 AstrBot 系统状态"),
            types.BotCommand("invert", "/反色 - 回复图片或动图"),
            types.BotCommand("flip", "/左右翻转 - 回复图片或动图"),
            types.BotCommand("gif", "/转GIF - 回复图片或视频"),
            types.BotCommand("aqt", "回复文字或图片生成名言梗图"),
            types.BotCommand("mc", "/mc成就 - 标题 描述；可回复图片"),
            types.BotCommand("mc_challenge", "/mc挑战 - 标题 描述；可回复图片"),
            types.BotCommand("music", "/点歌 - 歌名；文字列表选歌"),
            types.BotCommand("qqmusic", "/QQ点歌 - 歌名 序号"),
            types.BotCommand("lyrics", "/查歌词 - 歌名；原版歌词输出"),
            types.BotCommand("download", "📥 下载指定频道/群组/链接的媒体文件"),
            types.BotCommand("forward", "➡️ 转发消息媒体至其他频道/群组"),
            types.BotCommand("plugins", "🧩 查看当前已启用的扩展插件列表"),
            types.BotCommand("reload", "⚡ 免重启热重载所有插件"),
            types.BotCommand("restart", "🔄 重启机器人服务以加载最新配置"),
        ]

    async def update_bot_commands(self):
        """Refreshes bot commands registered in Telegram."""
        if self.bot and getattr(self.bot, "is_connected", False):
            commands = self.get_bot_commands()
            scopes = [
                types.BotCommandScopeDefault(),
                types.BotCommandScopeAllPrivateChats(),
                *[types.BotCommandScopeChat(int(uid)) for uid in set(self.allowed_user_ids)],
            ]
            for scope in scopes:
                try:
                    for language in ("", "zh", "en"):
                        await self.bot.set_bot_commands(
                            commands, scope=scope, language_code=language,
                        )
                except Exception as error:
                    if not isinstance(scope, types.BotCommandScopeChat):
                        raise
                    logger.warning("Could not update private command menu: {}", type(error).__name__)
            await self.bot.set_chat_menu_button(menu_button=types.MenuButtonCommands())

    def assign_config(self, _config: dict):
        """assign config from str.

        Parameters
        ----------
        _config: dict
            application config dict

        Returns
        -------
        bool
        """

        self.download_filter = _config.get("download_filter", self.download_filter)

        return True

    def get_plugin_config(self, plugin_id: str) -> dict:
        """Returns the configuration dictionary for the specified plugin."""
        plugins_cfg = self.config.get("plugins", {})
        if isinstance(plugins_cfg, dict):
            return plugins_cfg.get(plugin_id, {})
        return {}

    def update_config(self):
        """Update config from str."""
        self.config["download_filter"] = self.download_filter

        with open("d", "w", encoding="utf-8") as yaml_file:
            self._yaml.dump(self.config, yaml_file)

    async def start(
        self,
        app: Application,
        client: pyrogram.Client,
        add_download_task: Callable,
        download_chat_task: Callable,
    ):
        """Start bot"""
        self.bot = pyrogram.Client(
            app.application_name + "_bot",
            api_hash=app.api_hash,
            api_id=app.api_id,
            bot_token=app.bot_token,
            workdir=app.session_file_path,
            proxy=app.proxy,
        )

        # 命令列表
        self.app = app
        self.client = client
        self.add_download_task = add_download_task
        self.download_chat_task = download_chat_task
        set_max_concurrent_transmissions(self.bot, self.app.max_concurrent_transmissions)

        # load config
        if os.path.exists(self.config_path):
            with open(self.config_path, encoding="utf-8") as f:
                config = self._yaml.load(f.read())
                if config:
                    self.config = config
                    self.assign_config(self.config)

        await self.bot.start()

        self.bot_info = await self.bot.get_me()

        for allowed_user_id in self.app.allowed_user_ids:
            try:
                chat = await self.client.get_chat(allowed_user_id)
                self.allowed_user_ids.append(chat.id)
            except Exception as e:
                logger.warning(f"set allowed_user_ids error: {e}, adding raw id")
                try:
                    self.allowed_user_ids.append(int(allowed_user_id))
                except Exception:
                    pass
        logger.info(f"Initialized allowed_user_ids whitelist: {self.allowed_user_ids}")

        admin = await self.client.get_me()
        self.allowed_user_ids.append(admin.id)

        await self.update_bot_commands()

        self.bot.add_handler(
            MessageHandler(
                download_from_bot,
                filters=pyrogram.filters.command(["download"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                forward_messages,
                filters=pyrogram.filters.command(["forward"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                download_forward_media,
                filters=(pyrogram.filters.media & ~pyrogram.filters.sticker)
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                download_from_link,
                filters=pyrogram.filters.regex(r"^https://t.me.*")
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                list_plugins_cmd,
                filters=pyrogram.filters.command(["plugins"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                reload_plugins_cmd,
                filters=pyrogram.filters.command(["reload"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                restart_bot_cmd,
                filters=pyrogram.filters.command(["restart"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.load_plugins()
        self.bot.add_handler(
            MessageHandler(
                set_listen_forward_msg,
                filters=pyrogram.filters.command(["listen_forward"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                help_command,
                filters=pyrogram.filters.command(["help"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                get_info,
                filters=pyrogram.filters.command(["get_info"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                help_command,
                filters=pyrogram.filters.command(["start"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                set_language,
                filters=pyrogram.filters.command(["set_language"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                add_filter,
                filters=pyrogram.filters.command(["add_filter"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )

        self.bot.add_handler(
            MessageHandler(
                stop,
                filters=pyrogram.filters.command(["stop"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )

        self.bot.add_handler(
            CallbackQueryHandler(
                on_query_handler, filters=pyrogram.filters.user(self.allowed_user_ids)
            )
        )

        self.client.add_handler(MessageHandler(listen_forward_msg))

        try:
            await send_help_str(self.bot, admin.id)
        except Exception:
            pass

        self.reply_task = _bot.app.loop.create_task(_bot.update_reply_message())

        self.bot.add_handler(
            MessageHandler(
                forward_to_comments,
                filters=pyrogram.filters.command(["forward_to_comments"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )

        async def fallback_private_message(client, message):
            """Intelligent AI conversation fallback or guidance response for non-command private text messages."""
            text = (message.text or "").strip()
            if not text:
                return
            if self.get_plugin_config("astrbot_bridge").get("enabled", False):
                return

            # 如果用户明确发送帮助关键词
            if text in ["帮助", "help", "/help", "/start", "菜单"]:
                await send_help_str(client, message.chat.id)
                return

            # 尝试大模型智能对话
            cfg = getattr(_bot, "config", {}) or {}
            p_cfg = cfg.get("plugins", {}).get("bili_summary", {}) if isinstance(cfg, dict) else {}
            api_key = p_cfg.get("llm_api_key", "").strip()
            api_base = p_cfg.get("llm_api_base", "https://api.deepseek.com/v1").rstrip("/")
            model = p_cfg.get("llm_model", "deepseek-chat")
            proxy_url = p_cfg.get("proxy", "http://192.168.1.34:7890")

            if api_key and not text.startswith("/"):
                try:
                    await client.send_chat_action(message.chat.id, pyrogram.enums.ChatAction.TYPING)
                    loop = asyncio.get_running_loop()

                    def _call_chat():
                        payload = {
                            "model": model,
                            "messages": [
                                {
                                    "role": "system",
                                    "content": (
                                        "你是部署在飞牛私有云 NAS 上的 Telegram 私人专属 AI 助理。"
                                        "请用亲切、友好、客观、条理清晰的中文回答用户，使用优雅的 Markdown 格式排版。"
                                    ),
                                },
                                {"role": "user", "content": text},
                            ],
                            "temperature": 0.6,
                            "max_tokens": 1500,
                        }
                        req = urllib.request.Request(
                            f"{api_base}/chat/completions",
                            data=json.dumps(payload).encode("utf-8"),
                            headers={
                                "Content-Type": "application/json",
                                "Authorization": f"Bearer {api_key}",
                            },
                        )
                        handlers = []
                        if proxy_url:
                            handlers.append(
                                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                            )
                        opener = urllib.request.build_opener(*handlers)
                        with opener.open(req, timeout=35) as resp:
                            res = json.loads(resp.read().decode("utf-8"))
                            return res["choices"][0]["message"]["content"].strip()

                    reply_text = await loop.run_in_executor(None, _call_chat)
                    if reply_text:
                        await message.reply_text(
                            reply_text,
                            parse_mode=pyrogram.enums.ParseMode.MARKDOWN,
                            disable_web_page_preview=True,
                        )
                        return
                except Exception as e:
                    logger.debug(f"AI chat fallback notice: {e}")

            # 若未配置大模型或调用失败，发送友好指南卡片
            await send_help_str(client, message.chat.id)

        self.bot.add_handler(
            MessageHandler(
                fallback_private_message,
                filters=pyrogram.filters.private
                & pyrogram.filters.text
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )



_bot = DownloadBot()


async def start_download_bot(
    app: Application,
    client: pyrogram.Client,
    add_download_task: Callable,
    download_chat_task: Callable,
):
    """Start download bot"""
    await _bot.start(app, client, add_download_task, download_chat_task)


async def stop_download_bot():
    """Stop download bot"""
    _bot.update_config()
    _bot.is_running = False
    if _bot.reply_task:
        _bot.reply_task.cancel()
    _bot.stop_task("all")
    if _bot.bot:
        await _bot.bot.stop()


async def send_help_str(client: pyrogram.Client, chat_id):
    """
    Sends a help string to the specified chat ID using the provided client.

    Parameters:
        client (pyrogram.Client): The Pyrogram client used to send the message.
        chat_id: The ID of the chat to which the message will be sent.

    Returns:
        str: The help string that was sent.

    Note:
        The help string includes information about the Telegram Media Downloader bot,
        its version, and the available commands.
    """

    update_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Github",
                    url="https://github.com/tangyoha/telegram_media_downloader/releases",
                ),
                InlineKeyboardButton(
                    "Join us", url="https://t.me/TeegramMediaDownload"
                ),
            ]
        ]
    )
    latest_release_str = ""
    # try:
    #     latest_release = get_latest_release(_bot.app.proxy)

    #     latest_release_str = (
    #         f"{_t('New Version')}: [{latest_release['name']}]({latest_release['html_url']})\an"
    #         if latest_release
    #         else ""
    #     )
    # except Exception:
    #     latest_release_str = ""

    msg = (
        "👋 **你好！我是你的私人专属 AI 助理**\n\n"
        "🚀 **常用核心指令：**\n"
        "/bili - 📺 视频总结：提取字幕并生成 Markdown 结构化笔记\n"
        "/bili_list - 📋 订阅清单：查看当前会话的 B 站订阅\n"
        "/pan - 🔍 网盘搜索：秒解 4K 精选影视/夸克/阿里/百度等 (后接关键词)\n"
        "/nyaa - 🎬 动漫 BT：番剧检索并推送到 NAS qBittorrent 离线下载\n"
        "/wp - 🖼️ 壁纸工坊：Steam 创意工坊壁纸高速下载提取至 NAS\n"
        "/bt - 📊 任务进度：查看 NAS 当前所有 BT 离线下载进度与速度\n"
        "/save - 🎭 贴纸保存：将表情包/贴纸保存为图片或动图\n"
        "/save_mode - 🔄 格式切换：切换贴纸转换格式 (WebP/GIF/PNG)\n"
        "/ainews - 📰 AI 早报：橘鸦 AI 每日资讯速递与大模型提炼总结\n"
        "/news - 🗞️ 综合早报：小真寻每日一言/摸鱼倒计时/微语/IT快讯/番剧\n\n"
        "/fortune - 今日运势\n"
        "/image - 生图；/upscale - 回复图片升图\n"
        "/latest - 查询 UP 主最新视频\n"
        "/subscribe - B站订阅；/unsubscribe - 取消订阅\n\n"
        "/状态 - 本机系统状态\n"
        "/反色 - 反色；/左右翻转 - 左右翻转；/转GIF - 转 GIF\n"
        "/aqt - 回复内容生成名言梗图\n"
        "/mc成就 标题 描述 - MC 成就；/mc挑战 标题 描述 - MC 挑战\n"
        "/点歌 歌名 - 点歌；/QQ点歌 歌名 序号 - QQ 音源\n"
        "/查歌词 歌名 - 歌词\n\n"
        "📋 **进阶功能与管理指令 (点击即可发送)：**\n"
        "/sukebei - 🔞 绅士里区：检索 Sukebei R18 磁力直下 NAS\n"
        "/vcb - 💿 蓝光精压：检索 VCB-Studio 10-bit 高清原盘合集\n"
        "/download - 📥 媒体下载：下载指定频道/群组/链接的媒体文件\n"
        "/forward - ➡️ 消息转发：转发消息媒体至其他频道/群组\n"
        "/plugins - 🧩 插件列表：查看当前已启用的扩展插件\n"
        "/reload - ⚡ 插件重载：免重启热重载所有插件\n"
        "/restart - 🔄 服务重启：重启机器人容器以加载新代码与配置\n"
        "/help - 📋 命令帮助：重新显示本帮助信息卡片\n\n"
        "💡 **快捷特性 (无需指令，直接发送链接/内容即可)：**\n"
        "• 直接发送任意文本 ➔ 自动与大模型智能对话交流\n"
        "• 直接发送 B 站视频/动态链接 ➔ 自动提取字幕生成结构化图文总结\n"
        "• 直接发送 X (Twitter) 推文链接 ➔ 自动解析下载最高画质视频\n"
        "• 直接发送 Steam 创意工坊壁纸链接 ➔ 自动下载并提取视频\n"
        "• 直接发送磁力链接 (magnet:?xt=...) 或种子 ➔ 自动推送到 NAS 下载\n\n"
        "⚙️ *通道：NAS 专属代理直连 | 存储：飞牛私有云 | 仅限授权使用*"
    )

    await client.send_message(chat_id, msg, reply_markup=update_keyboard, disable_web_page_preview=True)


async def help_command(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Sends a message with the available commands and their usage.

    Parameters:
        client (pyrogram.Client): The client instance.
        message (pyrogram.types.Message): The message object.

    Returns:
        None
    """

    await send_help_str(client, message.chat.id)


async def set_language(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Set the language of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    if len(message.text.split()) != 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /set_language en/ru/zh/ua"),
        )
        return

    language = message.text.split()[1]

    try:
        language = Language[language.upper()]
        _bot.app.set_language(language)
        await client.send_message(
            message.from_user.id, f"{_t('Language set to')} {language.name}"
        )
    except KeyError:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /set_language en/ru/zh/ua"),
        )


async def get_info(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Async function that retrieves information from a group message link.
    """

    msg = _t("Invalid command format. Please use /get_info group_message_link")

    args = message.text.split()
    if len(args) != 2:
        await client.send_message(
            message.from_user.id,
            msg,
        )
        return

    chat_id, message_id, _ = await parse_link(_bot.client, args[1])

    entity = None
    if chat_id:
        entity = await _bot.client.get_chat(chat_id)

    if entity:
        if message_id:
            _message = await retry(_bot.client.get_messages, args=(chat_id, message_id))
            if _message:
                meta_data = MetaData()
                set_meta_data(meta_data, _message)
                msg = (
                    f"`\n"
                    f"{_t('Group/Channel')}\n"
                    f"├─ {_t('id')}: {entity.id}\n"
                    f"├─ {_t('first name')}: {entity.first_name}\n"
                    f"├─ {_t('last name')}: {entity.last_name}\n"
                    f"└─ {_t('name')}: {entity.username}\n"
                    f"{_t('Message')}\n"
                )

                for key, value in meta_data.data().items():
                    if key == "send_name":
                        msg += f"└─ {key}: {value or None}\n"
                    else:
                        msg += f"├─ {key}: {value or None}\n"

                msg += "`"
    await client.send_message(
        message.from_user.id,
        msg,
    )


async def add_filter(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Set the download filter of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /add_filter your filter"),
        )
        return

    filter_str = replace_date_time(args[1])
    res, err = _bot.filter.check_filter(filter_str)
    if res:
        _bot.app.down = args[1]
        await client.send_message(
            message.from_user.id, f"{_t('Add download filter')} : {args[1]}"
        )
    else:
        await client.send_message(
            message.from_user.id, f"{err}\n{_t('Check error, please add again!')}"
        )
    return


async def direct_download(
    download_bot: DownloadBot,
    chat_id: Union[str, int],
    message: pyrogram.types.Message,
    download_message: pyrogram.types.Message,
    client: pyrogram.Client = None,
):
    """Direct Download"""

    task_id = _bot.gen_task_id()
    replay_message = "⏳ **正在连接 Telegram 媒体流高速下载...**"
    cancel_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消下载", callback_data=f"cancel_tg_download_{task_id}")]
    ])
    last_reply_message = await download_bot.bot.send_message(
        message.from_user.id,
        replay_message,
        reply_to_message_id=message.id,
        reply_markup=cancel_keyboard,
    )

    node = TaskNode(
        chat_id=chat_id,
        from_user_id=message.from_user.id,
        reply_message_id=last_reply_message.id,
        replay_message=replay_message,
        limit=1,
        bot=download_bot.bot,
        task_id=task_id,
    )
    node.last_edit_msg = replay_message

    node.client = client

    _bot.add_task_node(node)

    await _bot.add_download_task(
        download_message,
        node,
    )

    node.is_running = True


async def download_forward_media(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Downloads the media from a forwarded message.

    Parameters:
        client (pyrogram.Client): The client instance.
        message (pyrogram.types.Message): The message object.

    Returns:
        None
    """

    if message.media and getattr(message, message.media.value):
        await direct_download(_bot, message.from_user.id, message, message, client)
        return

    await client.send_message(
        message.from_user.id,
        f"1. {_t('Direct download, directly forward the message to your robot')}\n\n",
        parse_mode=pyrogram.enums.ParseMode.HTML,
    )


async def download_from_link(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Downloads a single message from a Telegram link.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the Telegram link.

    Returns:
        None
    """

    if not message.text or not message.text.startswith("https://t.me"):
        return

    msg = (
        f"1. {_t('Directly download a single message')}\n"
        "<i>https://t.me/12000000/1</i>\n\n"
    )

    text = message.text.split()
    if len(text) != 1:
        await client.send_message(
            message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
        )

    chat_id, message_id, _ = await parse_link(_bot.client, text[0])

    entity = None
    if chat_id:
        entity = await _bot.client.get_chat(chat_id)
    if entity:
        if message_id:
            download_message = await retry(
                _bot.client.get_messages, args=(chat_id, message_id)
            )
            if download_message:
                await direct_download(_bot, entity.id, message, download_message)
            else:
                client.send_message(
                    message.from_user.id,
                    f"{_t('From')} {entity.title} {_t('download')} {message_id} {_t('error')}!",
                    reply_to_message_id=message.id,
                )
        return

    await client.send_message(
        message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
    )


# pylint: disable = R0912, R0915,R0914


async def download_from_bot(client: pyrogram.Client, message: pyrogram.types.Message):
    """Download from bot"""

    msg = (
        f"{_t('Parameter error, please enter according to the reference format')}:\n\n"
        f"1. {_t('Download all messages of common group')}\n"
        "<i>/download https://t.me/fkdhlg 1 0</i>\n\n"
        f"{_t('The private group (channel) link is a random group message link')}\n\n"
        f"2. {_t('The download starts from the N message to the end of the M message')}. "
        f"{_t('When M is 0, it means the last message. The filter is optional')}\n"
        f"<i>/download https://t.me/12000000 N M [filter]</i>\n\n"
    )

    args = message.text.split(maxsplit=4)
    if not message.text or len(args) < 4:
        await client.send_message(
            message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
        )
        return

    url = args[1]
    try:
        start_offset_id = int(args[2])
        end_offset_id = int(args[3])
    except Exception:
        await client.send_message(
            message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
        )
        return

    limit = 0
    if end_offset_id:
        if end_offset_id < start_offset_id:
            raise ValueError(
                f"end_offset_id < start_offset_id, {end_offset_id} < {start_offset_id}"
            )

        limit = end_offset_id - start_offset_id + 1

    download_filter = args[4] if len(args) > 4 else None

    if download_filter:
        download_filter = replace_date_time(download_filter)
        res, err = _bot.filter.check_filter(download_filter)
        if not res:
            await client.send_message(
                message.from_user.id, err, reply_to_message_id=message.id
            )
            return
    try:
        chat_id, _, _ = await parse_link(_bot.client, url)
        if chat_id:
            entity = await _bot.client.get_chat(chat_id)
        if entity:
            chat_title = entity.title
            reply_message = f"from {chat_title} "
            chat_download_config = ChatDownloadConfig()
            chat_download_config.last_read_message_id = start_offset_id
            chat_download_config.download_filter = download_filter
            reply_message += (
                f"download message id = {start_offset_id} - {end_offset_id} !"
            )
            task_id = _bot.gen_task_id()
            cancel_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ 取消下载", callback_data=f"cancel_tg_download_{task_id}")]
            ])
            last_reply_message = await client.send_message(
                message.from_user.id,
                reply_message,
                reply_to_message_id=message.id,
                reply_markup=cancel_keyboard,
            )
            node = TaskNode(
                chat_id=entity.id,
                from_user_id=message.from_user.id,
                reply_message_id=last_reply_message.id,
                replay_message=reply_message,
                limit=limit,
                start_offset_id=start_offset_id,
                end_offset_id=end_offset_id,
                bot=_bot.bot,
                task_id=task_id,
            )
            node.is_running = True
            _bot.add_task_node(node)
            _bot.app.loop.create_task(
                _bot.download_chat_task(_bot.client, chat_download_config, node)
            )
    except Exception as e:
        await client.send_message(
            message.from_user.id,
            f"{_t('chat input error, please enter the channel or group link')}\n\n"
            f"{_t('Error type')}: {e.__class__}"
            f"{_t('Exception message')}: {e}",
        )
        return


async def get_forward_task_node(
    client: pyrogram.Client,
    message: pyrogram.types.Message,
    task_type: TaskType,
    src_chat_link: str,
    dst_chat_link: str,
    offset_id: int = 0,
    end_offset_id: int = 0,
    download_filter: str = None,
    reply_comment: bool = False,
):
    """Get task node"""
    limit: int = 0

    if end_offset_id:
        if end_offset_id < offset_id:
            await client.send_message(
                message.from_user.id,
                f" end_offset_id({end_offset_id}) < start_offset_id({offset_id}),"
                f" end_offset_id{_t('must be greater than')} offset_id",
            )
            return None

        limit = end_offset_id - offset_id + 1

    src_chat_id, _, _ = await parse_link(_bot.client, src_chat_link)
    dst_chat_id, target_msg_id, topic_id = await parse_link(_bot.client, dst_chat_link)

    if not src_chat_id or not dst_chat_id:
        logger.info(f"{src_chat_id} {dst_chat_id}")
        await client.send_message(
            message.from_user.id,
            _t("Invalid chat link") + f"{src_chat_id} {dst_chat_id}",
            reply_to_message_id=message.id,
        )
        return None

    try:
        src_chat = await _bot.client.get_chat(src_chat_id)
        dst_chat = await _bot.client.get_chat(dst_chat_id)
    except Exception as e:
        await client.send_message(
            message.from_user.id,
            f"{_t('Invalid chat link')} {e}",
            reply_to_message_id=message.id,
        )
        logger.exception(f"get chat error: {e}")
        return None

    me = await client.get_me()
    if dst_chat.id == me.id:
        # TODO: when bot receive message judge if download
        await client.send_message(
            message.from_user.id,
            _t("Cannot be forwarded to this bot, will cause an infinite loop"),
            reply_to_message_id=message.id,
        )
        return None

    if download_filter:
        download_filter = replace_date_time(download_filter)
        res, err = _bot.filter.check_filter(download_filter)
        if not res:
            await client.send_message(
                message.from_user.id, err, reply_to_message_id=message.id
            )

    last_reply_message = await client.send_message(
        message.from_user.id,
        "Forwarding message, please wait...",
        reply_to_message_id=message.id,
    )

    node = TaskNode(
        chat_id=src_chat.id,
        from_user_id=message.from_user.id,
        upload_telegram_chat_id=dst_chat_id,
        reply_message_id=last_reply_message.id,
        replay_message=last_reply_message.text,
        has_protected_content=src_chat.has_protected_content,
        download_filter=download_filter,
        limit=limit,
        start_offset_id=offset_id,
        end_offset_id=end_offset_id,
        bot=_bot.bot,
        task_id=_bot.gen_task_id(),
        task_type=task_type,
        topic_id=topic_id,
    )

    if target_msg_id and reply_comment:
        node.reply_to_message = await _bot.client.get_discussion_message(
            dst_chat_id, target_msg_id
        )

    _bot.add_task_node(node)

    node.upload_user = _bot.client
    if not dst_chat.type is pyrogram.enums.ChatType.BOT:
        has_permission = await check_user_permission(_bot.client, me.id, dst_chat.id)
        if has_permission:
            node.upload_user = _bot.bot

    if node.upload_user is _bot.client:
        await client.edit_message_text(
            message.from_user.id,
            last_reply_message.id,
            "Note that the robot may not be in the target group,"
            " use the user account to forward",
        )

    return node


# pylint: disable = R0914
async def forward_message_impl(
    client: pyrogram.Client, message: pyrogram.types.Message, reply_comment: bool
):
    """
    Forward message
    """

    async def report_error(client: pyrogram.Client, message: pyrogram.types.Message):
        """Report error"""

        await client.send_message(
            message.from_user.id,
            f"{_t('Invalid command format')}."
            f"{_t('Please use')} "
            "/forward https://t.me/c/src_chat https://t.me/c/dst_chat "
            f"1 400 `[`{_t('Filter')}`]`\n",
        )

    args = message.text.split(maxsplit=5)
    if len(args) < 5:
        await report_error(client, message)
        return

    src_chat_link = args[1]
    dst_chat_link = args[2]

    try:
        offset_id = int(args[3])
        end_offset_id = int(args[4])
    except Exception:
        await report_error(client, message)
        return

    download_filter = args[5] if len(args) > 5 else None

    node = await get_forward_task_node(
        client,
        message,
        TaskType.Forward,
        src_chat_link,
        dst_chat_link,
        offset_id,
        end_offset_id,
        download_filter,
        reply_comment,
    )

    if not node:
        return

    if not node.has_protected_content:
        try:
            async for item in get_chat_history_v2(  # type: ignore
                _bot.client,
                node.chat_id,
                limit=node.limit,
                max_id=node.end_offset_id,
                offset_id=offset_id,
                reverse=True,
            ):
                await forward_normal_content(client, node, item)
                if node.is_stop_transmission:
                    await client.edit_message_text(
                        message.from_user.id,
                        node.reply_message_id,
                        f"{_t('Stop Forward')}",
                    )
                    break
        except Exception as e:
            await client.edit_message_text(
                message.from_user.id,
                node.reply_message_id,
                f"{_t('Error forwarding message')} {e}",
            )
        finally:
            await report_bot_status(client, node, immediate_reply=True)
            node.stop_transmission()
    else:
        await forward_msg(node, offset_id)


async def forward_messages(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Forwards messages from one chat to another.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """
    return await forward_message_impl(client, message, False)


async def forward_normal_content(
    client: pyrogram.Client, node: TaskNode, message: pyrogram.types.Message
):
    """Forward normal content"""
    forward_ret = ForwardStatus.FailedForward
    if node.download_filter:
        meta_data = MetaData()
        caption = message.caption
        if caption:
            caption = validate_title(caption)
            _bot.app.set_caption_name(node.chat_id, message.media_group_id, caption)
        else:
            caption = _bot.app.get_caption_name(node.chat_id, message.media_group_id)
        set_meta_data(meta_data, message, caption)
        _bot.filter.set_meta_data(meta_data)
        if not _bot.filter.exec(node.download_filter):
            forward_ret = ForwardStatus.SkipForward
            if message.media_group_id:
                node.upload_status[message.id] = UploadStatus.SkipUpload
                await proc_cache_forward(_bot.client, node, message, False)
            await report_bot_forward_status(client, node, forward_ret)
            return

    await upload_telegram_chat_message(
        _bot.client, node.upload_user, _bot.app, node, message
    )


async def forward_msg(node: TaskNode, message_id: int):
    """Forward normal message"""

    chat_download_config = ChatDownloadConfig()
    chat_download_config.last_read_message_id = message_id
    chat_download_config.download_filter = node.download_filter  # type: ignore

    await _bot.download_chat_task(_bot.client, chat_download_config, node)


async def set_listen_forward_msg(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Set the chat to listen for forwarded messages.

    Args:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message sent by the user.

    Returns:
        None
    """
    args = message.text.split(maxsplit=3)

    if len(args) < 3:
        await client.send_message(
            message.from_user.id,
            f"{_t('Invalid command format')}. {_t('Please use')} /listen_forward "
            f"https://t.me/c/src_chat https://t.me/c/dst_chat [{_t('Filter')}]\n",
        )
        return

    src_chat_link = args[1]
    dst_chat_link = args[2]

    download_filter = args[3] if len(args) > 3 else None

    node = await get_forward_task_node(
        client,
        message,
        TaskType.ListenForward,
        src_chat_link,
        dst_chat_link,
        download_filter=download_filter,
    )

    if not node:
        return

    if node.chat_id in _bot.listen_forward_chat:
        _bot.remove_task_node(_bot.listen_forward_chat[node.chat_id].task_id)

    node.is_running = True
    _bot.listen_forward_chat[node.chat_id] = node


async def listen_forward_msg(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Forwards messages from a chat to another chat if the message does not contain protected content.
    If the message contains protected content, it will be downloaded and forwarded to the other chat.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message to be forwarded.
    """

    if message.chat and message.chat.id in _bot.listen_forward_chat:
        node = _bot.listen_forward_chat[message.chat.id]

        # TODO(tangyoha):fix run time change protected content
        if not node.has_protected_content:
            await forward_normal_content(client, node, message)
            await report_bot_status(client, node, immediate_reply=True)
        else:
            await _bot.add_download_task(
                message,
                node,
            )


async def stop(client: pyrogram.Client, message: pyrogram.types.Message):
    """Stops listening for forwarded messages."""

    await client.send_message(
        message.chat.id,
        _t("Please select:"),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        _t("Stop Download"), callback_data="stop_download"
                    ),
                    InlineKeyboardButton(
                        _t("Stop Forward"), callback_data="stop_forward"
                    ),
                ],
                [  # Second row
                    InlineKeyboardButton(
                        _t("Stop Listen Forward"), callback_data="stop_listen_forward"
                    )
                ],
            ]
        ),
    )


async def stop_task(
    client: pyrogram.Client,
    query: pyrogram.types.CallbackQuery,
    queryHandler: str,
    task_type: TaskType,
):
    """Stop task"""
    if query.data == queryHandler:
        buttons: List[InlineKeyboardButton] = []
        temp_buttons: List[InlineKeyboardButton] = []
        for key, value in _bot.task_node.copy().items():
            if not value.is_finish() and value.task_type is task_type:
                if len(temp_buttons) == 3:
                    buttons.append(temp_buttons)
                    temp_buttons = []
                temp_buttons.append(
                    InlineKeyboardButton(
                        f"{key}", callback_data=f"{queryHandler} task {key}"
                    )
                )
        if temp_buttons:
            buttons.append(temp_buttons)

        if buttons:
            buttons.insert(
                0,
                [
                    InlineKeyboardButton(
                        _t("all"), callback_data=f"{queryHandler} task all"
                    )
                ],
            )
            await client.edit_message_text(
                query.message.from_user.id,
                query.message.id,
                f"{_t('Stop')} {_t(task_type.name)}...",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            await client.edit_message_text(
                query.message.from_user.id,
                query.message.id,
                f"{_t('No Task')}",
            )
    else:
        task_id = query.data.split(" ")[2]
        await client.edit_message_text(
            query.message.from_user.id,
            query.message.id,
            f"{_t('Stop')} {_t(task_type.name)}...",
        )
        _bot.stop_task(task_id)


async def cancel_telegram_download(
    client: pyrogram.Client, query: pyrogram.types.CallbackQuery, task_id_str: str
):
    """Cancels a running Telegram download task directly from the inline button."""
    try:
        task_id = int(task_id_str)
    except Exception:
        await query.answer("⚠️ 无效的任务标识", show_alert=True)
        return

    task = _bot.task_node.get(task_id)
    if task:
        task.is_stop_transmission = True
        task.is_cancelled = True
        try:
            task.stop_transmission()
        except Exception:
            pass
        _bot.stop_task(str(task_id))
        is_single = getattr(task, "limit", 1) == 1
        if is_single:
            cancel_text = (
                f"🛑 **【Telegram 媒体下载已取消】**\n\n"
                f"⚠️ 该下载任务已由用户手动取消。"
            )
        else:
            cancel_text = (
                f"🛑 **【Telegram 批量下载已取消】**\n\n"
                f"🆔 任务 ID: `{task.task_id}`\n"
                f"⚠️ 批量下载任务已被用户手动取消。"
            )
        task.last_edit_msg = cancel_text
        task.is_finished_reported = True
        try:
            await client.edit_message_text(
                query.message.chat.id,
                query.message.id,
                cancel_text,
                parse_mode=pyrogram.enums.ParseMode.MARKDOWN,
                reply_markup=None,
            )
        except Exception:
            try:
                await client.edit_message_text(
                    query.message.chat.id,
                    query.message.id,
                    cancel_text,
                    reply_markup=None,
                )
            except Exception:
                pass
        _bot.remove_task_node(task_id)
        await query.answer("🛑 已成功取消下载", show_alert=False)
    else:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer("⚠️ 该下载任务已结束或已被取消", show_alert=False)


async def on_query_handler(
    client: pyrogram.Client, query: pyrogram.types.CallbackQuery
):
    """
    Asynchronous function that handles query callbacks.

    Parameters:
        client (pyrogram.Client): The Pyrogram client object.
        query (pyrogram.types.CallbackQuery): The callback query object.

    Returns:
        None
    """

    if query.data and query.data.startswith("cancel_tg_download_"):
        task_id_str = query.data.split("cancel_tg_download_")[1]
        await cancel_telegram_download(client, query, task_id_str)
        return

    for it in QueryHandler:
        queryHandler = QueryHandlerStr.get_str(it.value)
        if queryHandler and queryHandler in (query.data or ""):
            await stop_task(client, query, queryHandler, TaskType(it.value))
            return
    raise pyrogram.ContinuePropagation


async def forward_to_comments(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Forwards specified media to a designated comment section.

    Usage: /forward_to_comments <source_chat_link> <destination_chat_link> <msg_start_id> <msg_end_id>

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.
    """
    return await forward_message_impl(client, message, True)


async def list_plugins_cmd(client: pyrogram.Client, message: pyrogram.types.Message):
    logger.info(f"User {message.from_user.id} requested /plugins")
    plugins = getattr(_bot, "plugins", {})
    if not plugins:
        await client.send_message(message.chat.id, "⚠️ 当前尚未加载任何插件。", reply_to_message_id=message.id)
        return
    text = f"🧩 **当前已加载插件列表 ({len(plugins)} 个)**:\n"
    for p_id, p in plugins.items():
        text += (
            f"\n📦 **{p['name']}** (v{p['version']})\n"
            f" ├─ 🆔 标识: `{p_id}`\n"
            f" └─ 📝 描述: {p['desc']}\n"
        )
    text += "\n💡 *提示: 在 plugins/ 增改插件后，发送 /reload 即可免重启热重载生效！*"
    await client.send_message(message.chat.id, text, reply_to_message_id=message.id)

async def reload_plugins_cmd(client: pyrogram.Client, message: pyrogram.types.Message):
    logger.info(f"User {message.from_user.id} requested /reload")
    count = _bot.reload_plugins()
    try:
        await _bot.update_bot_commands()
    except Exception as e:
        logger.debug(f"Failed to update bot commands on reload: {e}")
    await client.send_message(
        message.chat.id,
        f"✅ **插件热重载成功！**\n当前已重新加载生效 **{count}** 个插件，全部命令菜单与帮助已同步更新生效。",
        reply_to_message_id=message.id
    )


async def restart_bot_cmd(client: pyrogram.Client, message: pyrogram.types.Message):
    """Restart bot container"""
    logger.info(f"User {message.from_user.id} requested /restart")
    try:
        await client.send_message(
            message.chat.id,
            "🔄 **正在重启 Telegram Media Downloader 服务...**\nDocker 容器将自动拉起最新配置与代码，请稍候约 5-10 秒。",
            reply_to_message_id=message.id,
        )
    except Exception as e:
        logger.error(f"Failed to send restart message: {e}")
    await asyncio.sleep(1)
    os._exit(0)

