import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs

import pytest

spec = importlib.util.spec_from_file_location(
    "steam_helper_auth_test", Path(__file__).resolve().parents[1] / "steam_service.py",
)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


@pytest.mark.parametrize("body,status,expected", [(b"Ok.", 200, "SID=test"), (b"Fails.", 200, ""), (b"Ok.", 403, "")])
def test_qbt_cookie_requires_successful_login(monkeypatch, body, status, expected):
    monkeypatch.delenv("QBT_USERNAME", raising=False)
    monkeypatch.delenv("QBT_PASSWORD", raising=False)
    monkeypatch.setattr(helper, "load_config", lambda: {"qbt": {"username": "owner", "password": "a&b= c"}})
    monkeypatch.setattr(helper, "get_fnos_qbt_credentials", lambda path: {})
    response = SimpleNamespace(status=status, read=lambda: body, getheader=lambda name: "SID=test; HttpOnly")
    connection = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(helper, "UnixHTTPConnection", lambda path: connection)
    assert helper.get_qbt_cookie("/test.sock") == expected
    assert parse_qs(connection.request.call_args.args[2]) == {"username": ["owner"], "password": ["a&b= c"]}
    connection.close.assert_called_once()


def test_dynamic_credentials_match_only_the_requested_fnos_socket(tmp_path):
    process = tmp_path / "123"
    process.mkdir()
    command = process / "cmdline"
    command.write_bytes(
        b"/usr/trim/bin/trim-qbittorrent-nox\0"
        b"--webui-password=current&= secret\0--webui-sock-path=/home/owner/qbt.sock\0"
    )
    assert helper.get_fnos_qbt_credentials("/home/owner/qbt.sock", str(tmp_path)) == {
        "username": "admin", "password": "current&= secret",
    }
    assert helper.get_fnos_qbt_credentials("/home/another/qbt.sock", str(tmp_path)) == {}
    command.write_bytes(b"/usr/bin/unrelated\0--webui-password=secret\0--webui-sock-path=/home/owner/qbt.sock\0")
    assert helper.get_fnos_qbt_credentials("/home/owner/qbt.sock", str(tmp_path)) == {}


def test_login_uses_rotated_credentials_and_encodes_them(monkeypatch):
    for name in ("QBT_USERNAME", "QBT_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    credentials = {"username": "admin", "password": "new&= password"}
    monkeypatch.setattr(helper, "get_fnos_qbt_credentials", lambda path: credentials)
    monkeypatch.setattr(helper, "load_config", lambda: {"qbt": {"username": "old", "password": "old"}})
    response = SimpleNamespace(
        status=200, read=lambda: b"Ok.", getheader=lambda name: "SID=test; HttpOnly",
    )
    connection = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(helper, "UnixHTTPConnection", lambda path: connection)
    assert helper.get_qbt_cookie("/test.sock") == "SID=test"
    assert parse_qs(connection.request.call_args.args[2]) == {
        "username": ["admin"], "password": ["new&= password"],
    }


def test_steamcmd_container_uses_configured_nas_http_proxy(monkeypatch):
    monkeypatch.delenv("STEAM_PROXY", raising=False)
    monkeypatch.setattr(helper, "load_config", lambda: {"steam_proxy": "http://nas:7890"})
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="ok"))
    monkeypatch.setattr(helper.subprocess, "run", run)
    assert helper.run_steamcmd(["+quit"]) == (0, "ok")
    command = run.call_args.args[0]
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert f"{name}=http://nas:7890" in command
    assert command[-2:] == ["steamcmd/steamcmd:latest", "+quit"]
