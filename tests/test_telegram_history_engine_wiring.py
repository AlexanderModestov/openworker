"""build_engine() registers the read-only work-chat tools iff a TelegramHistoryStore is
passed in — mirrors test_web_search.py's minimal engine-construction pattern."""

from __future__ import annotations

from coworker.agent import build_engine
from coworker.agents import chat_agent
from coworker.connectors.telegram_history_store import TelegramHistoryStore
from coworker.secrets import SecretStore

_WORK_CHAT_TOOLS = {
    "get_recent_work_chat_messages",
    "search_work_chat",
    "get_messages_by_sender",
    "get_my_own_messages",
    "check_work_chat_health",
}


class _StubProvider:
    def complete(self, **_kw):
        from coworker.providers import AssistantTurn

        return AssistantTurn()

    def capabilities(self, _model):
        from coworker.providers.base import ModelCapabilities

        return ModelCapabilities()


def test_engine_registers_work_chat_tools_when_store_present(tmp_path):
    store = TelegramHistoryStore(tmp_path / "telegram_history.db")
    eng = build_engine(
        agent=chat_agent(),
        provider=_StubProvider(),
        secrets=SecretStore(tmp_path / "s.json"),
        telegram_history=store,
    )
    assert _WORK_CHAT_TOOLS <= set(eng.registry.names())


def test_engine_omits_work_chat_tools_when_store_absent(tmp_path):
    eng = build_engine(
        agent=chat_agent(),
        provider=_StubProvider(),
        secrets=SecretStore(tmp_path / "s.json"),
    )
    assert not (_WORK_CHAT_TOOLS & set(eng.registry.names()))
