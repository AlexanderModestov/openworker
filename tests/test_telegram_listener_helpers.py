"""Tests for the pure-Python helpers in telegram_listener.py that don't need Telethon
(the module lazy-imports Telethon inside functions, so it's importable without it)."""

from __future__ import annotations

import pytest

from types import SimpleNamespace

from coworker.connectors.telegram_listener import (
    _match_keywords,
    _prompt,
    _require_config,
    _resolve_chats,
)


def _dialog(entity_id: int, *, channel: bool) -> SimpleNamespace:
    return SimpleNamespace(entity=SimpleNamespace(id=entity_id, channel=channel))


def _fake_peer_id(entity) -> int:
    # Stand-in for telethon.utils.get_peer_id: channels get the -100 prefix, users don't.
    return -1000000000000 - entity.id if entity.channel else entity.id


def test_resolve_chats_accepts_marked_channel_id():
    dialogs = [_dialog(361072099, channel=True)]
    resolved = _resolve_chats(dialogs, ["-1000361072099"], peer_id=_fake_peer_id)
    assert resolved == [("-1000361072099", dialogs[0].entity)]


def test_resolve_chats_accepts_legacy_bare_id_and_canonicalizes_it():
    # An older --login saved d.entity.id (unmarked). It must still resolve, and the chat_id
    # written to the store must be the marked form so it matches event.chat_id later.
    dialogs = [_dialog(361072099, channel=True)]
    resolved = _resolve_chats(dialogs, ["361072099"], peer_id=_fake_peer_id)
    assert resolved == [("-1000361072099", dialogs[0].entity)]


def test_resolve_chats_user_id_is_unchanged():
    dialogs = [_dialog(42, channel=False)]
    assert _resolve_chats(dialogs, ["42"], peer_id=_fake_peer_id) == [("42", dialogs[0].entity)]


def test_resolve_chats_raises_on_unknown_chat():
    dialogs = [_dialog(1, channel=False)]
    with pytest.raises(SystemExit):
        _resolve_chats(dialogs, ["1", "999"], peer_id=_fake_peer_id)


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
