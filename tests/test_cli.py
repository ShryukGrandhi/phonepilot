import json

import httpx

from phonepilot import cli
from phonepilot.cloud import PhoneHarnessClient


def test_parser_defaults_and_subcommands():
    p = cli._parser()
    a = p.parse_args(["run", "do it", "--max-steps", "7", "--keep"])
    assert a.cmd == "run" and a.task == "do it" and a.max_steps == 7 and a.keep and a.timeout == 900
    a = p.parse_args(["op", "sid1", "input.tap", "x=1", "y=2"])
    assert a.kw == ["x=1", "y=2"]
    a = p.parse_args(["video", "runs/x", "--seconds", "1.5"])
    assert a.seconds == 1.5


def test_literal_parses_json_or_keeps_string():
    assert cli._literal("3") == 3 and cli._literal("true") is True and cli._literal("abc") == "abc"


def test_main_without_command_prints_help_and_returns_2(capsys):
    assert cli.main([]) == 2
    assert "usage" in capsys.readouterr().out


def test_main_reports_api_errors(monkeypatch, capsys):
    monkeypatch.setenv("PHONE_HARNESS_API_KEY", "k")
    transport = httpx.MockTransport(lambda r: httpx.Response(403, json={"error": "no rentals", "code": "rent_blocked"}))
    monkeypatch.setattr(cli, "PhoneHarnessClient", lambda **kw: PhoneHarnessClient(api_key="k", transport=transport))
    assert cli.main(["account"]) == 1
    out = capsys.readouterr().out
    assert "API error" in out and "rent_blocked" in out


def test_sessions_and_history_commands(monkeypatch, capsys, phone):
    monkeypatch.setenv("PHONE_HARNESS_API_KEY", "test-key")
    phone.state = "ready"

    def handler(request):
        if request.url.path == "/sessions" and request.method == "GET":
            return httpx.Response(200, json=[phone._session()])
        if request.url.path == "/history":
            return httpx.Response(200, json=[{"sid": "abc", "usage_minutes": 3, "cost_cents": 105, "end_reason": "session duration limit"}])
        return phone.handle(request)

    monkeypatch.setattr(cli, "PhoneHarnessClient", lambda **kw: PhoneHarnessClient(api_key="test-key", transport=httpx.MockTransport(handler)))
    assert cli.main(["sessions"]) == 0
    assert "fake123" in capsys.readouterr().out
    assert cli.main(["history"]) == 0
    assert "1.05 USD" in capsys.readouterr().out
    assert cli.main(["end", "fake123"]) == 0
    assert phone.state == "closing"


def test_op_command_prints_result(monkeypatch, capsys, phone):
    monkeypatch.setenv("PHONE_HARNESS_API_KEY", "test-key")
    phone.state = "ready"
    monkeypatch.setattr(cli, "PhoneHarnessClient", lambda **kw: PhoneHarnessClient(api_key="test-key", transport=httpx.MockTransport(phone.handle)))
    assert cli.main(["op", "fake123", "apps.current"]) == 0
    assert json.loads(capsys.readouterr().out) == "app.lawnchair"
    assert cli.main(["op", "fake123", "screen.capture"]) == 0
    assert "chars>" in capsys.readouterr().out
