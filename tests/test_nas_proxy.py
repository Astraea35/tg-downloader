import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
PROXY = {"http": "http://192.168.1.34:7890", "https": "http://192.168.1.34:7890"}


def load_plugin(name):
    spec = importlib.util.spec_from_file_location(name + "_proxy_test", ROOT / "plugins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("url", ["https://video.twimg.com/test.mp4", "https://mirror.example/test.mp4"])
def test_twitter_retries_never_bypass_configured_nas_proxy(monkeypatch, tmp_path, url):
    module = load_plugin("twitter_downloader")
    session = Mock()
    session.get.side_effect = requests.ConnectionError("proxy unavailable")
    monkeypatch.setattr(module.requests, "Session", lambda: session)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    result = module.download_with_fallback([{"url": url}], str(tmp_path / "test.mp4"), PROXY)
    assert not result["success"]
    assert session.get.call_count == 3
    assert all(call.kwargs["proxies"] == PROXY for call in session.get.call_args_list)
    assert session.trust_env is False


def test_snapvid_first_request_uses_configured_proxy(monkeypatch):
    module = load_plugin("twitter_downloader")
    post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"status": "error"}))
    monkeypatch.setattr(module.requests, "post", post)
    assert module.extract_from_snapvid("https://x.com/i/status/1", PROXY) is None
    assert post.call_count == 1
    assert post.call_args.kwargs["proxies"] == PROXY


def test_flaresolverr_session_and_fresh_retry_keep_nas_proxy(monkeypatch):
    module = load_plugin("twitter_downloader")
    calls = []

    def post(url, **kwargs):
        payload = kwargs["json"]
        calls.append(payload.copy())
        if payload["cmd"] == "sessions.list":
            data = {"sessions": []}
        elif payload["cmd"] == "sessions.create":
            data = {"status": "ok"}
        else:
            data = {"status": "ok", "solution": {"response": "not json"}}
        return SimpleNamespace(status_code=200, json=lambda: data)

    monkeypatch.setattr(module.requests, "post", post)
    module.extract_from_snapvid_flaresolverr("https://x.com/i/status/1", proxies=PROXY)
    routed = [call for call in calls if call["cmd"] in {"sessions.create", "request.post"}]
    assert len(routed) == 3
    assert all(call["proxy"] == {"url": PROXY["https"]} for call in routed)


@pytest.mark.parametrize(
    ("plugin", "function", "arguments", "raises"),
    [
        ("wallpaper_downloader", "get_steam_workshop_details", ("1", PROXY), False),
        ("wallpaper_downloader", "fetch_preview_image_bytes", ("https://example.com/image.jpg", PROXY), False),
        ("pan_search", "sync_fetch_url", ("https://example.com", None, None, PROXY), True),
    ],
)
def test_proxy_failure_does_not_trigger_direct_request(monkeypatch, plugin, function, arguments, raises):
    module = load_plugin(plugin)
    opener = Mock()
    opener.open.side_effect = OSError("proxy unavailable")
    build_opener = Mock(return_value=opener)
    monkeypatch.setattr(module.urllib.request, "build_opener", build_opener)
    if raises:
        with pytest.raises(OSError):
            getattr(module, function)(*arguments)
    else:
        getattr(module, function)(*arguments)
    assert build_opener.call_count == 1
    assert build_opener.call_args.args[0].proxies == PROXY
