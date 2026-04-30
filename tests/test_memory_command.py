from unittest.mock import patch
from core.memory_command import handle_manual_memory_command


def test_retiens_ca_saves():
    with patch("core.memory_command.save_memory") as mock_save:
        result = handle_manual_memory_command("Retiens ça: mon âge est 25")
        mock_save.assert_called_once_with("manual", "mon âge est 25")
        assert result is not None
        assert "mon âge est 25" in result


def test_souviens_toi_de_ca_saves():
    with patch("core.memory_command.save_memory") as mock_save:
        result = handle_manual_memory_command("Souviens-toi de ça: je préfère le dark mode")
        mock_save.assert_called_once_with("manual", "je préfère le dark mode")
        assert result is not None
        assert "je préfère le dark mode" in result


def test_souviens_toi_legacy_saves():
    """Legacy "souviens-toi:" still works; content is preserved as-is (no category split)."""
    with patch("core.memory_command.save_memory") as mock_save:
        result = handle_manual_memory_command("souviens-toi: préférence:vim")
        mock_save.assert_called_once_with("manual", "préférence:vim")
        assert result is not None


def test_case_insensitive():
    with patch("core.memory_command.save_memory") as mock_save:
        result = handle_manual_memory_command("RETIENS ÇA: test majuscules")
        mock_save.assert_called_once_with("manual", "test majuscules")
        assert result is not None


def test_leading_whitespace_ignored():
    with patch("core.memory_command.save_memory") as mock_save:
        result = handle_manual_memory_command("  Retiens ça: avec espace avant")
        mock_save.assert_called_once_with("manual", "avec espace avant")
        assert result is not None


def test_empty_content_no_save():
    with patch("core.memory_command.save_memory") as mock_save:
        result = handle_manual_memory_command("Retiens ça:")
        mock_save.assert_not_called()
        assert result is not None
        assert "vide" in result


def test_empty_content_with_spaces_no_save():
    with patch("core.memory_command.save_memory") as mock_save:
        result = handle_manual_memory_command("Retiens ça:   ")
        mock_save.assert_not_called()
        assert result is not None


def test_normal_message_returns_none():
    with patch("core.memory_command.save_memory") as mock_save:
        result = handle_manual_memory_command("Comment tu vas ?")
        mock_save.assert_not_called()
        assert result is None


def test_partial_prefix_returns_none():
    with patch("core.memory_command.save_memory") as mock_save:
        result = handle_manual_memory_command("souviens")
        mock_save.assert_not_called()
        assert result is None


def test_confirmation_contains_full_content():
    with patch("core.memory_command.save_memory"):
        result = handle_manual_memory_command("Retiens ça: foo bar baz")
        assert "foo bar baz" in result


def test_content_with_colon_preserved():
    """Colons inside the content must not be split further."""
    with patch("core.memory_command.save_memory") as mock_save:
        result = handle_manual_memory_command("Retiens ça: Python 3.12: plus rapide")
        mock_save.assert_called_once_with("manual", "Python 3.12: plus rapide")
        assert result is not None
