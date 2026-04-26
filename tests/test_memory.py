from unittest.mock import patch
from core.memory import parse_and_save


def test_valid_input_returns_true():
    with patch("core.memory.save_memory") as mock_save:
        result = parse_and_save("SAVE:knowledge:GPT-4o now supports video")
        assert result is True
        mock_save.assert_called_once_with("knowledge", "GPT-4o now supports video")


def test_nothing_returns_false():
    with patch("core.memory.save_memory") as mock_save:
        result = parse_and_save("NOTHING")
        assert result is False
        mock_save.assert_not_called()


def test_missing_second_colon_returns_false():
    with patch("core.memory.save_memory") as mock_save:
        result = parse_and_save("SAVE:onlyone")
        assert result is False
        mock_save.assert_not_called()


def test_empty_category_returns_false():
    with patch("core.memory.save_memory") as mock_save:
        result = parse_and_save("SAVE::some content here")
        assert result is False
        mock_save.assert_not_called()


def test_empty_content_returns_false():
    with patch("core.memory.save_memory") as mock_save:
        result = parse_and_save("SAVE:knowledge:")
        assert result is False
        mock_save.assert_not_called()


def test_whitespace_only_content_returns_false():
    with patch("core.memory.save_memory") as mock_save:
        result = parse_and_save("SAVE:knowledge:   ")
        assert result is False
        mock_save.assert_not_called()


def test_content_with_colons_preserved():
    """Colons inside content must not be split further."""
    with patch("core.memory.save_memory") as mock_save:
        result = parse_and_save("SAVE:knowledge:Python 3.12: faster and better")
        assert result is True
        mock_save.assert_called_once_with("knowledge", "Python 3.12: faster and better")


def test_empty_string_returns_false():
    with patch("core.memory.save_memory") as mock_save:
        result = parse_and_save("")
        assert result is False
        mock_save.assert_not_called()


def test_whitespace_input_returns_false():
    with patch("core.memory.save_memory") as mock_save:
        result = parse_and_save("   ")
        assert result is False
        mock_save.assert_not_called()
