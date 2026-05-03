from unittest.mock import patch
from core.memory import parse_and_save

# Arbitrary stand-in user_id; these tests only check parsing logic.
_UID = 42


class TestParseAndSave:
    def test_valid_save_format_saves_memory(self):
        with patch("core.memory.save_memory") as mock_save:
            result = parse_and_save("SAVE:preferences:likes coffee", _UID)
        assert result is True
        mock_save.assert_called_once_with("preferences", "likes coffee", _UID)

    def test_nothing_response_returns_false(self):
        with patch("core.memory.save_memory") as mock_save:
            result = parse_and_save("NOTHING", _UID)
        assert result is False
        mock_save.assert_not_called()

    def test_invalid_text_returns_false(self):
        with patch("core.memory.save_memory") as mock_save:
            result = parse_and_save("random text", _UID)
        assert result is False
        mock_save.assert_not_called()

    def test_extra_colons_in_content_preserved(self):
        with patch("core.memory.save_memory") as mock_save:
            result = parse_and_save("SAVE:preferences:likes coffee:with milk", _UID)
        assert result is True
        mock_save.assert_called_once_with("preferences", "likes coffee:with milk", _UID)

    def test_missing_content_returns_false(self):
        with patch("core.memory.save_memory") as mock_save:
            result = parse_and_save("SAVE:preferences", _UID)
        assert result is False
        mock_save.assert_not_called()

    def test_empty_input_returns_false(self):
        with patch("core.memory.save_memory") as mock_save:
            result = parse_and_save("", _UID)
        assert result is False
        mock_save.assert_not_called()

    def test_whitespace_input_returns_false(self):
        with patch("core.memory.save_memory") as mock_save:
            result = parse_and_save("   ", _UID)
        assert result is False
        mock_save.assert_not_called()
