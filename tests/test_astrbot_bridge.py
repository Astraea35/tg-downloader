import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "nas_bridge_test", ROOT / "plugins/astrbot_bridge.py"
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


@pytest.fixture(autouse=True)
def gateway():
    import asyncio

    client = SimpleNamespace(
        edit_message_text=AsyncMock(),
        send_message=AsyncMock(return_value=SimpleNamespace(id=99)),
    )
    bridge._bot_instance = SimpleNamespace(
        bot=client,
        allowed_user_ids=[123],
        config={"plugins": {}},
    )
    bridge._states.clear()
    bridge._delivered.clear()
    bridge._states["123-1"] = {
        "chat_id": "123",
        "status_id": 77,
        "pages": [],
        "result_text": "",
        "done": False,
        "lock": asyncio.Lock(),
    }
    return client


@pytest.mark.parametrize(
    "text",
    [
        "/download https://t.me/c/123/4",
        "/nyaa Frieren",
        "/wp 123",
        "https://x.com/example/status/123",
        "https://steamcommunity.com/sharedfiles/filedetails/?id=1",
        "magnet:?xt=urn:btih:123",
        "/pan 测试",
        "/restart",
    ],
)
def test_downloads_stay_on_nas(text):
    assert not bridge.should_forward(text)


@pytest.mark.parametrize(
    "text",
    [
        "/bili BV123",
        "/今日运势",
        "/订阅列表",
        "/生图 cat",
        "/ainews",
        "https://www.bilibili.com/video/BV123",
        "你好",
    ],
)
def test_personal_features_go_to_astrbot(text):
    assert bridge.should_forward(text)


def test_bare_media_stays_on_nas_but_caption_commands_can_be_forwarded():
    assert not bridge.should_forward("", True)
    assert bridge.should_forward("/升图", True)
    assert bridge.normalize_command("/bili@my_bot BV123") == "/总结 BV123"
    assert bridge.normalize_command("/news") == "/日报"


@pytest.mark.parametrize("alias,command", [
    ("invert", "反色"), ("flip", "左右翻转"), ("gif", "转GIF"),
    ("mc", "mc成就"), ("mc_challenge", "mc挑战"),
    ("music", "点歌"), ("qqmusic", "QQ点歌"), ("lyrics", "查歌词"),
])
def test_menu_alias_preserves_arguments_and_routes_to_astrbot(alias, command):
    text = f"/{alias}@private_bot 名称 0 0 1 1 * 参数"
    assert bridge.should_forward(text, has_media=True)
    assert bridge.normalize_command(text) == f"/{command} 名称 0 0 1 1 * 参数"


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["ocr", "表情转换", "今日小猪", "添加提醒", "吃什么"])
async def test_removed_features_do_not_reach_astrbot(command):
    message = SimpleNamespace(
        text=f"/{command}", caption=None, reply_text=AsyncMock(),
        stop_propagation=lambda: None,
    )
    await bridge.forward_message(SimpleNamespace(), message)
    message.reply_text.assert_awaited_once_with("该功能未在 TG 启用。", quote=True)


def test_telegram_layout_separates_sections_and_items_without_changing_code():
    text = "# 标题\n1. 第一条\n2. 第二条\n## 详细信息\n- 要点\n```\n- code\n```"
    result = bridge.telegram_markdown(text)
    assert "**标题**\n\n1. 第一条\n\n2. 第二条" in result
    assert "**详细信息**\n\n• 要点" in result
    assert "```\n- code\n```" in result
    assert bridge.telegram_markdown(result) == result


def test_long_emoji_text_is_lossless_and_below_telegram_limit():
    text = ("中文 😀 **段落**\n" * 1000) + "END"
    chunks = bridge.split_text(text)
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 3500 for chunk in chunks)


@pytest.mark.asyncio
async def test_progress_and_final_text_edit_the_same_message(gateway):
    client = gateway
    for text in ("正在搜索", "正在总结", "**最终结果**"):
        result = await bridge.receive_push(
            {
                "chat_id": "123",
                "request_id": "123-1",
                "parts": [{"type": "text", "text": text}],
            }
        )
        assert result["ok"]
    assert client.edit_message_text.await_count == 3
    assert {call.args[:2] for call in client.edit_message_text.await_args_list} == {
        (123, 77)
    }
    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_result_keeps_the_remainder_when_markdown_is_rejected(gateway):
    import pyrogram

    client = gateway
    client.send_message.side_effect = [
        pyrogram.errors.EntityBoundsInvalid(),
        SimpleNamespace(id=99),
    ]
    text = "a" * 3500 + "**remainder**"
    await bridge.receive_push({
        "chat_id": "123",
        "request_id": "123-1",
        "parts": [{"type": "text", "text": text}],
    })
    assert client.edit_message_text.await_args.args[2] == "a" * 3500
    assert client.send_message.await_args.args[1] == "**remainder**"
    assert client.send_message.await_args.kwargs["parse_mode"] == pyrogram.enums.ParseMode.DISABLED
    assert bridge._states["123-1"]["pages"] == [99]


@pytest.mark.asyncio
async def test_plain_text_fallback_works_with_older_pyrogram(gateway, monkeypatch):
    import pyrogram

    monkeypatch.delattr(pyrogram.errors, "EntityBoundsInvalid", raising=False)
    gateway.edit_message_text.side_effect = [pyrogram.errors.BadRequest(), None]
    await bridge._edit_text(gateway, 123, 77, "status text")
    assert gateway.edit_message_text.await_count == 2
    assert gateway.edit_message_text.await_args.kwargs["parse_mode"] == pyrogram.enums.ParseMode.DISABLED


@pytest.mark.asyncio
async def test_missing_progress_message_is_replaced_with_the_result(gateway):
    import pyrogram

    gateway.edit_message_text.side_effect = pyrogram.errors.MessageIdInvalid()
    await bridge._update_text(gateway, 123, bridge._states["123-1"], "saved path")
    assert gateway.send_message.await_args.args == (123, "saved path")
    assert bridge._states["123-1"]["status_id"] == 99


@pytest.mark.asyncio
async def test_unknown_request_and_unlisted_user_cannot_send_messages():
    with pytest.raises(ValueError):
        await bridge.receive_push({"chat_id": 456, "parts": []})
    with pytest.raises(ValueError):
        await bridge.receive_push(
            {"chat_id": 123, "request_id": "unknown", "parts": []}
        )


@pytest.mark.asyncio
async def test_duplicate_delivery_does_not_send_again():
    client = bridge._bot_instance.bot
    before = client.edit_message_text.await_count
    payload = {
        "chat_id": 123,
        "request_id": "123-1",
        "delivery_id": "delivery-123",
        "parts": [{"type": "text", "text": "完成"}],
    }
    await bridge.receive_push(payload)
    assert (await bridge.receive_push(payload))["duplicate"]
    assert client.edit_message_text.await_count == before + 1


def test_web_callback_requires_matching_secret():
    bridge._bot_instance.config["plugins"]["astrbot_bridge"] = {
        "shared_secret": "a" * 40
    }
    with pytest.raises(PermissionError):
        bridge.handle_web_push({}, "b" * 40)


@pytest.mark.asyncio
async def test_followup_lyrics_keep_previously_saved_media_paths(gateway):
    bridge._states["123-1"]["saved_paths"] = ["/nas/music/song.mp3"]
    await bridge.receive_push({
        "chat_id": 123, "request_id": "123-1",
        "parts": [{"type": "text", "text": "歌词第一行\n歌词第二行"}],
    })
    text = gateway.edit_message_text.await_args.args[2]
    assert "歌词第一行\n歌词第二行" in text
    assert "/nas/music/song.mp3" in text
    assert text.count("/nas/music/song.mp3") == 1


@pytest.mark.asyncio
async def test_generated_media_replaces_started_notice_with_saved_path(
    gateway, monkeypatch, tmp_path,
):
    bridge._states["123-1"]["result_text"] = "已开始生图任务 [任务ID: test]"
    bridge._bot_instance.app = SimpleNamespace(save_path=str(tmp_path))
    gateway.send_photo = AsyncMock()
    monkeypatch.setattr(bridge, "_download_asset", lambda asset, target: None)
    await bridge.receive_push({
        "chat_id": 123, "request_id": "123-1",
        "parts": [{"type": "image", "name": "result.png"}],
    })
    text = gateway.edit_message_text.await_args.args[2]
    assert "已开始生图任务" not in text
    assert "已保存到 NAS" in text and "result.png" in text
