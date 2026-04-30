import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

# Stub heavy deps before importing core.chat
for _mod in ("ddgs", "ollama"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core.identity import IDENTITY_CONTRACT  # noqa: E402
from core.chat import build_messages         # noqa: E402


class TestIdentityContract:
    def test_contract_is_non_empty(self):
        assert IDENTITY_CONTRACT.strip()

    def test_contract_names_nova(self):
        assert "Nova" in IDENTITY_CONTRACT

    def test_contract_hides_model_name(self):
        # gemma4 should appear only as an example of what NOT to reveal
        assert "gemma4" in IDENTITY_CONTRACT

    def test_contract_covers_identity_question(self):
        assert "Nova c'est qui" in IDENTITY_CONTRACT


class TestBuildMessagesInjectsIdentity:
    def test_normal_context_starts_with_contract(self):
        messages = build_messages([], "bonjour", [], None, None, None)
        assert messages[0]["content"].startswith(IDENTITY_CONTRACT)

    def test_weather_context_contains_contract(self):
        messages = build_messages([], "météo ?", [], "temp: 20°C", "weather", None)
        assert IDENTITY_CONTRACT in messages[0]["content"]

    def test_search_context_contains_contract(self):
        messages = build_messages([], "qui est Einstein ?", [], "résultats…", "search", None)
        assert IDENTITY_CONTRACT in messages[0]["content"]
