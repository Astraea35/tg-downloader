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
    response = SimpleNamespace(status=status, read=lambda: body, getheader=lambda name: "SID=test; HttpOnly")
    connection = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(helper, "UnixHTTPConnection", lambda path: connection)
    assert helper.get_qbt_cookie("/test.sock") == expected
    assert parse_qs(connection.request.call_args.args[2]) == {"username": ["owner"], "password": ["a&b= c"]}
    connection.close.assert_called_once()
