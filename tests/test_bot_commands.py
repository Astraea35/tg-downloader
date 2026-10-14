import ast
import re
from pathlib import Path
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock

import pytest
from pyrogram import types


tree = ast.parse((Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8-sig"))
source = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DownloadBot")
methods = [
    node for node in source.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    and node.name in {"get_bot_commands", "update_bot_commands"}
]
namespace = {"types": types, "List": List, "logger": SimpleNamespace(warning=lambda *args: None)}
exec(compile(ast.Module(body=methods, type_ignores=[]), "bot_command_methods", "exec"), namespace)


def test_menu_has_valid_unique_commands_covering_personal_features():
    commands = namespace["get_bot_commands"](None)
    names = [item.command for item in commands]
    assert len(names) == len(set(names)) <= 100
    assert all(re.fullmatch("[a-z0-9_]{1,32}", name) for name in names)
    assert all(1 <= len(item.description) <= 256 for item in commands)
    assert {
        "status", "aqt", "invert", "flip", "gif", "mc", "mc_challenge",
        "music", "qqmusic", "lyrics",
    } <= set(names)
    assert not {
        "ocr", "convert", "pig", "eat", "reminder_add", "task_add",
        "reminder_run", "task_run", "reminder_delete", "task_delete",
    } & set(names)


@pytest.mark.asyncio
async def test_menu_refresh_covers_private_language_overrides():
    bot = SimpleNamespace(
        is_connected=True, set_bot_commands=AsyncMock(), set_chat_menu_button=AsyncMock(),
    )
    instance = SimpleNamespace(
        bot=bot, allowed_user_ids=[123, 123],
        get_bot_commands=lambda: namespace["get_bot_commands"](None),
    )
    await namespace["update_bot_commands"](instance)
    assert bot.set_bot_commands.await_count == 9
    scopes = {}
    for call in bot.set_bot_commands.await_args_list:
        scopes.setdefault(type(call.kwargs["scope"]).__name__, set()).add(call.kwargs["language_code"])
    assert scopes == {
        "BotCommandScopeDefault": {"", "zh", "en"},
        "BotCommandScopeAllPrivateChats": {"", "zh", "en"},
        "BotCommandScopeChat": {"", "zh", "en"},
    }
    assert isinstance(bot.set_chat_menu_button.call_args.kwargs["menu_button"], types.MenuButtonCommands)
