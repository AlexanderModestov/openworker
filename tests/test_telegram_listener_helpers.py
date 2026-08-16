"""Tests for the pure-Python helpers in telegram_listener.py that don't need Telethon
(the module lazy-imports Telethon inside functions, so it's importable without it)."""

from __future__ import annotations

import pytest

from coworker.connectors.telegram_listener import _match_keywords, _prompt, _require_config


def test_match_keywords_case_insensitive():
    assert _match_keywords("this is URGENT", ["urgent"]) == ["urgent"]


def test_match_keywords_multiple_hits():
    hits = _match_keywords("urgent, please @alex asap", ["urgent", "@alex", "asap"])
    assert set(hits) == {"urgent", "@alex", "asap"}


def test_match_keywords_no_hit_returns_none():
    assert _match_keywords("just chatting", ["urgent"]) is None


def test_match_keywords_empty_text():
    assert _match_keywords("", ["urgent"]) is None


def test_require_config_raises_on_missing_fields():
    with pytest.raises(SystemExit):
        _require_config({"api_id": "1"})


def test_require_config_passes_when_complete():
    _require_config(
        {"api_id": "1", "api_hash": "h", "session_string": "s", "chat_ids": ["-100"]}
    )


def test_require_config_passes_with_multiple_chat_ids():
    _require_config(
        {"api_id": "1", "api_hash": "h", "session_string": "s", "chat_ids": ["-100", "-200"]}
    )


def test_require_config_raises_on_empty_chat_ids():
    with pytest.raises(SystemExit):
        _require_config(
            {"api_id": "1", "api_hash": "h", "session_string": "s", "chat_ids": []}
        )


def test_prompt_returns_first_valid_answer(monkeypatch):
    answers = iter(["12345"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert _prompt("api_id: ", validate=str.isdigit, error="nope") == "12345"


def test_prompt_reasks_on_invalid_then_blank_then_valid(monkeypatch, capsys):
    answers = iter([".venv/bin/openworker-telegram-listener --login", "", "12345"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    result = _prompt("api_id: ", validate=str.isdigit, error="api_id must be numeric")
    assert result == "12345"
    out = capsys.readouterr().out
    assert "api_id must be numeric" in out
    assert "empty" in out


def test_prompt_raises_systemexit_on_closed_stdin(monkeypatch):
    def _raise(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise)
    with pytest.raises(SystemExit):
        _prompt("api_id: ")
