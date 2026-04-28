"""
Tests for Natural Memory v1 + v2:
  - DB initialisation and schema migration
  - save / list / search memories
  - policy: reject sensitive content
  - extractor: detect preferences, projects, hardware
  - retriever: keyword (v1) and cosine (v2) retrieval
  - forget command: delete matching memories
  - embedding generation (mocked)
  - deduplication on write
  - preference overwrite
"""
import pytest
from unittest.mock import patch

from memory.embeddings import generate_embedding, cosine_similarity
from memory.schema import Memory
from memory.store import (
    initialize_memory_database,
    save_memory,
    update_memory,
    delete_memory,
    list_memories,
    search_memories,
    delete_memories_matching,
)
from memory.policy import is_memory_allowed
from memory.extractor import extract_memories
from memory.retriever import get_relevant_memories, format_for_prompt


# ── helpers ────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    """Provides a fresh, isolated SQLite database for each test."""
    db = str(tmp_path / "test_memory.db")
    initialize_memory_database(db)
    return db


def _mem(**kwargs) -> Memory:
    defaults = dict(kind="general", topic="test", content="test content", confidence=0.9)
    defaults.update(kwargs)
    return Memory(**defaults)


# ── DB initialisation ──────────────────────────────────────────────────────────

class TestDatabaseInit:
    def test_initialise_creates_table(self, tmp_db):
        mems = list_memories(db_path=tmp_db)
        assert isinstance(mems, list)
        assert mems == []

    def test_initialise_is_idempotent(self, tmp_db):
        # Calling twice must not raise or duplicate the table
        initialize_memory_database(tmp_db)
        mems = list_memories(db_path=tmp_db)
        assert mems == []


# ── save / list / search ───────────────────────────────────────────────────────

class TestSaveAndList:
    def test_save_and_list_single(self, tmp_db):
        m = _mem(kind="preference", topic="editor", content="User prefers neovim.")
        save_memory(m, db_path=tmp_db)
        result = list_memories(db_path=tmp_db)
        assert len(result) == 1
        assert result[0].content == "User prefers neovim."
        assert result[0].kind == "preference"

    def test_save_multiple_returns_all(self, tmp_db):
        # Use distinct kind+topic combinations so dedup doesn't consolidate them.
        save_memory(_mem(kind="preference", topic="editor", content="User prefers neovim."), db_path=tmp_db)
        save_memory(_mem(kind="software", topic="terminal", content="User uses alacritty."), db_path=tmp_db)
        save_memory(_mem(kind="hardware", topic="hardware_setup", content="User has 32GB RAM."), db_path=tmp_db)
        assert len(list_memories(db_path=tmp_db)) == 3

    def test_list_returns_newest_first(self, tmp_db):
        m1 = _mem(content="first", created_at="2024-01-01T00:00:00")
        m2 = _mem(content="second", created_at="2024-06-01T00:00:00")
        save_memory(m1, db_path=tmp_db)
        save_memory(m2, db_path=tmp_db)
        result = list_memories(db_path=tmp_db)
        assert result[0].content == "second"

    def test_delete_removes_memory(self, tmp_db):
        m = _mem()
        save_memory(m, db_path=tmp_db)
        delete_memory(m.id, db_path=tmp_db)
        assert list_memories(db_path=tmp_db) == []

    def test_update_changes_content(self, tmp_db):
        m = _mem(content="original")
        save_memory(m, db_path=tmp_db)
        m.content = "updated"
        update_memory(m, db_path=tmp_db)
        result = list_memories(db_path=tmp_db)
        assert result[0].content == "updated"


class TestSearchMemories:
    def test_search_returns_matching(self, tmp_db):
        save_memory(_mem(topic="linux_distribution", content="User prefers Fedora KDE."), db_path=tmp_db)
        save_memory(_mem(topic="editor", content="User uses neovim."), db_path=tmp_db)
        results = search_memories("fedora linux", db_path=tmp_db)
        assert len(results) == 1
        assert "Fedora" in results[0].content

    def test_search_empty_query_returns_nothing(self, tmp_db):
        save_memory(_mem(), db_path=tmp_db)
        assert search_memories("", db_path=tmp_db) == []

    def test_search_no_match_returns_empty(self, tmp_db):
        save_memory(_mem(content="User prefers dark mode."), db_path=tmp_db)
        assert search_memories("kubernetes docker swarm", db_path=tmp_db) == []

    def test_search_respects_limit(self, tmp_db):
        for i in range(10):
            save_memory(_mem(topic="python", content=f"User writes python {i}"), db_path=tmp_db)
        results = search_memories("python", limit=3, db_path=tmp_db)
        assert len(results) <= 3

    def test_search_ranks_by_keyword_overlap(self, tmp_db):
        save_memory(_mem(topic="linux", content="User uses linux fedora kde"), db_path=tmp_db)
        save_memory(_mem(topic="linux2", content="User uses linux"), db_path=tmp_db)
        results = search_memories("linux fedora kde", db_path=tmp_db)
        assert "fedora" in results[0].content.lower()


# ── policy ─────────────────────────────────────────────────────────────────────

class TestPolicy:
    def test_allows_normal_preference(self):
        m = _mem(kind="preference", topic="editor", content="User prefers neovim.")
        assert is_memory_allowed(m) is True

    def test_allows_project_info(self):
        m = _mem(kind="project", topic="nova", content="Nova uses FastAPI and Ollama.")
        assert is_memory_allowed(m) is True

    def test_allows_hardware_info(self):
        m = _mem(kind="hardware", topic="hardware", content="User has 32GB RAM and an Nvidia GPU.")
        assert is_memory_allowed(m) is True

    def test_allows_linux_distro_preference(self):
        m = _mem(kind="preference", topic="linux_distribution", content="User prefers Fedora KDE.")
        assert is_memory_allowed(m) is True

    def test_rejects_password(self):
        m = _mem(content="User's password is hunter2.")
        assert is_memory_allowed(m) is False

    def test_rejects_api_key(self):
        m = _mem(content="The api_key is sk-abc123.")
        assert is_memory_allowed(m) is False

    def test_rejects_token(self):
        m = _mem(content="token=abcdef1234567890abcdef1234567890")
        assert is_memory_allowed(m) is False

    def test_rejects_credit_card(self):
        m = _mem(content="credit card 4111111111111111")
        assert is_memory_allowed(m) is False

    def test_rejects_medical_info(self):
        m = _mem(content="User has diabetes and takes medication.")
        assert is_memory_allowed(m) is False

    def test_rejects_political_identity(self):
        m = _mem(content="User voted for Trump in the last election.")
        assert is_memory_allowed(m) is False

    def test_rejects_transient_emotion(self):
        m = _mem(content="User is feeling sad right now.")
        assert is_memory_allowed(m) is False

    def test_rejects_ex_relationship_drama(self):
        m = _mem(content="User's ex cheated on them.")
        assert is_memory_allowed(m) is False


# ── extractor ──────────────────────────────────────────────────────────────────

class TestExtractor:
    def test_extracts_preference(self):
        mems = extract_memories("I prefer Fedora KDE.")
        assert len(mems) >= 1
        kinds = [m.kind for m in mems]
        assert "preference" in kinds

    def test_extracts_avoid(self):
        mems = extract_memories("I hate Ubuntu.")
        assert any(m.kind == "avoid" for m in mems)

    def test_extracts_project(self):
        mems = extract_memories("My project Nova uses FastAPI and Ollama.")
        assert any(m.kind == "project" for m in mems)
        project_mems = [m for m in mems if m.kind == "project"]
        assert any("Nova" in m.content or "nova" in m.topic for m in project_mems)

    def test_project_content_includes_stack(self):
        mems = extract_memories("My project Nova uses FastAPI and Ollama.")
        project_mems = [m for m in mems if m.kind == "project"]
        assert any("FastAPI" in m.content for m in project_mems)

    def test_extracts_hardware(self):
        mems = extract_memories("My PC has 32GB RAM and an Nvidia GPU.")
        assert any(m.kind == "hardware" for m in mems)

    def test_extracts_software_usage(self):
        mems = extract_memories("I use neovim for coding.")
        assert any(m.kind == "software" for m in mems)

    def test_extracts_workflow(self):
        mems = extract_memories("From now on, always use type hints in Python.")
        assert any(m.kind == "workflow" for m in mems)

    def test_returns_list_for_empty_input(self):
        assert extract_memories("") == []

    def test_returns_list_for_no_trigger(self):
        mems = extract_memories("What is the weather today?")
        assert isinstance(mems, list)

    def test_infers_linux_topic(self):
        mems = extract_memories("I prefer Fedora KDE.")
        pref_mems = [m for m in mems if m.kind == "preference"]
        assert any(m.topic == "linux_distribution" for m in pref_mems)

    def test_confidence_is_positive(self):
        mems = extract_memories("I prefer dark themes.")
        assert all(m.confidence > 0 for m in mems)

    def test_extracts_french_preference(self):
        mems = extract_memories("Je préfère utiliser neovim.")
        assert any(m.kind == "preference" for m in mems)

    def test_extracts_multiple_triggers_in_one_message(self):
        mems = extract_memories("I prefer Fedora KDE and I hate Ubuntu.")
        kinds = [m.kind for m in mems]
        assert "preference" in kinds
        assert "avoid" in kinds


# ── retriever ──────────────────────────────────────────────────────────────────

class TestRetriever:
    def test_returns_relevant_memories(self, tmp_db):
        save_memory(_mem(topic="linux_distribution", content="User prefers Fedora."), db_path=tmp_db)
        save_memory(_mem(topic="editor", content="User uses neovim."), db_path=tmp_db)
        results = get_relevant_memories("which linux distro", db_path=tmp_db)
        assert any("Fedora" in m.content for m in results)

    def test_empty_message_returns_empty(self, tmp_db):
        save_memory(_mem(), db_path=tmp_db)
        assert get_relevant_memories("", db_path=tmp_db) == []

    def test_respects_limit(self, tmp_db):
        for i in range(20):
            save_memory(_mem(topic="python", content=f"User likes python {i}"), db_path=tmp_db)
        results = get_relevant_memories("python", limit=5, db_path=tmp_db)
        assert len(results) <= 5


class TestFormatForPrompt:
    def test_empty_list_returns_empty_string(self):
        assert format_for_prompt([]) == ""

    def test_formats_single_memory(self):
        m = _mem(kind="preference", topic="editor", content="User prefers neovim.")
        text = format_for_prompt([m])
        assert "Relevant user memory:" in text
        assert "preference/editor" in text
        assert "User prefers neovim." in text

    def test_formats_multiple_memories(self):
        mems = [
            _mem(kind="preference", topic="editor", content="User prefers neovim."),
            _mem(kind="hardware", topic="hardware", content="User has 32GB RAM."),
        ]
        text = format_for_prompt(mems)
        assert text.count("-") >= 2


# ── forget command ─────────────────────────────────────────────────────────────

class TestForgetCommand:
    def test_forget_deletes_matching(self, tmp_db):
        save_memory(_mem(topic="fedora", content="User prefers Fedora KDE."), db_path=tmp_db)
        save_memory(_mem(topic="neovim", content="User uses neovim."), db_path=tmp_db)
        count = delete_memories_matching("fedora", db_path=tmp_db)
        assert count == 1
        remaining = list_memories(db_path=tmp_db)
        assert all("Fedora" not in m.content for m in remaining)

    def test_forget_all_about_topic(self, tmp_db):
        save_memory(_mem(topic="ubuntu", content="User tried Ubuntu once."), db_path=tmp_db)
        save_memory(_mem(topic="ubuntu2", content="User disliked Ubuntu."), db_path=tmp_db)
        save_memory(_mem(topic="fedora", content="User prefers Fedora."), db_path=tmp_db)
        count = delete_memories_matching("ubuntu", db_path=tmp_db)
        assert count == 2
        remaining = list_memories(db_path=tmp_db)
        assert len(remaining) == 1
        assert "Fedora" in remaining[0].content

    def test_forget_nonexistent_returns_zero(self, tmp_db):
        count = delete_memories_matching("kubernetes", db_path=tmp_db)
        assert count == 0


# ── v2: embeddings ─────────────────────────────────────────────────────────────

class TestEmbeddings:
    def test_cosine_similarity_identical(self):
        v = [1.0, 0.0, 0.5]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_cosine_similarity_orthogonal(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_cosine_similarity_opposite(self):
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_cosine_similarity_zero_vector(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_generate_embedding_returns_list_on_success(self):
        with patch("memory.embeddings.client.embeddings", return_value={"embedding": [0.1, 0.2, 0.3]}):
            result = generate_embedding("test text")
        assert result == [0.1, 0.2, 0.3]

    def test_generate_embedding_returns_none_on_failure(self):
        with patch("memory.embeddings.client.embeddings", side_effect=Exception("ollama down")):
            result = generate_embedding("test text")
        assert result is None


# ── v2: schema migration ───────────────────────────────────────────────────────

class TestSchemaMigration:
    def test_embedding_column_created_on_init(self, tmp_db):
        mems = list_memories(db_path=tmp_db)
        assert isinstance(mems, list)

    def test_init_is_idempotent_with_embedding_column(self, tmp_db):
        # Calling initialize twice must not raise even if column already exists.
        initialize_memory_database(tmp_db)
        initialize_memory_database(tmp_db)
        assert list_memories(db_path=tmp_db) == []


# ── v2: embedding storage and retrieval ───────────────────────────────────────

class TestEmbeddingStorage:
    @pytest.fixture(autouse=True)
    def no_ollama(self, monkeypatch):
        monkeypatch.setattr("memory.store.generate_embedding", lambda _: None)

    def test_embedding_persisted_and_loaded(self, tmp_db):
        m = _mem()
        m.embedding = [0.1, 0.2, 0.3]
        save_memory(m, db_path=tmp_db)
        loaded = list_memories(db_path=tmp_db)
        assert loaded[0].embedding == pytest.approx([0.1, 0.2, 0.3])

    def test_null_embedding_round_trips_as_none(self, tmp_db):
        save_memory(_mem(), db_path=tmp_db)
        loaded = list_memories(db_path=tmp_db)
        assert loaded[0].embedding is None


# ── v2: deduplication on write ─────────────────────────────────────────────────

class TestDeduplication:
    @pytest.fixture(autouse=True)
    def no_ollama(self, monkeypatch):
        # Tests set embeddings directly; prevent accidental Ollama calls.
        monkeypatch.setattr("memory.store.generate_embedding", lambda _: None)

    def test_high_cosine_similarity_updates_existing(self, tmp_db):
        m1 = _mem(kind="preference", topic="editor", content="User prefers neovim.")
        m1.embedding = [1.0, 0.0, 0.0]
        save_memory(m1, db_path=tmp_db)

        m2 = _mem(kind="preference", topic="editor", content="User prefers VS Code.")
        m2.embedding = [0.98, 0.1, 0.0]  # cosine similarity ≈ 0.995 → above threshold
        save_memory(m2, db_path=tmp_db)

        mems = list_memories(db_path=tmp_db)
        assert len(mems) == 1
        assert "VS Code" in mems[0].content

    def test_low_cosine_similarity_inserts_new(self, tmp_db):
        m1 = _mem(kind="hardware", topic="hardware", content="User has 32GB RAM.")
        m1.embedding = [1.0, 0.0, 0.0]
        save_memory(m1, db_path=tmp_db)

        m2 = _mem(kind="hardware", topic="hardware", content="User has an Nvidia RTX 3090.")
        m2.embedding = [0.0, 1.0, 0.0]  # orthogonal → below threshold
        save_memory(m2, db_path=tmp_db)

        assert len(list_memories(db_path=tmp_db)) == 2

    def test_dedup_preserves_original_created_at(self, tmp_db):
        m1 = _mem(kind="preference", topic="editor", content="User prefers neovim.",
                  created_at="2024-01-01T00:00:00")
        m1.embedding = [1.0, 0.0]
        save_memory(m1, db_path=tmp_db)

        m2 = _mem(kind="preference", topic="editor", content="User prefers VS Code.")
        m2.embedding = [0.97, 0.1]
        save_memory(m2, db_path=tmp_db)

        mems = list_memories(db_path=tmp_db)
        assert mems[0].created_at == "2024-01-01T00:00:00"

    def test_different_topic_always_inserts_new(self, tmp_db):
        m1 = _mem(kind="preference", topic="editor", content="User prefers neovim.")
        m1.embedding = [1.0, 0.0]
        save_memory(m1, db_path=tmp_db)

        m2 = _mem(kind="preference", topic="terminal", content="User prefers alacritty.")
        m2.embedding = [1.0, 0.0]  # same embedding but different topic
        save_memory(m2, db_path=tmp_db)

        assert len(list_memories(db_path=tmp_db)) == 2

    def test_keyword_dedup_identical_content(self, tmp_db):
        # No embeddings — Jaccard similarity of identical content = 1.0 → update.
        m1 = _mem(kind="preference", topic="editor", content="User prefers neovim for coding.")
        save_memory(m1, db_path=tmp_db)

        m2 = _mem(kind="preference", topic="editor", content="User prefers neovim for coding.")
        save_memory(m2, db_path=tmp_db)

        assert len(list_memories(db_path=tmp_db)) == 1

    def test_keyword_dedup_similar_preference(self, tmp_db):
        # "User prefers neovim" vs "User prefers VS Code" share enough tokens
        # (user, prefers) for Jaccard ≥ 0.50 → update.
        m1 = _mem(kind="preference", topic="editor", content="User prefers neovim.")
        save_memory(m1, db_path=tmp_db)

        m2 = _mem(kind="preference", topic="editor", content="User prefers VS Code.")
        save_memory(m2, db_path=tmp_db)

        mems = list_memories(db_path=tmp_db)
        assert len(mems) == 1
        assert "VS Code" in mems[0].content


# ── v2: cosine retrieval ───────────────────────────────────────────────────────

class TestRetrieverV2:
    def test_cosine_search_returns_relevant(self, tmp_db):
        m = _mem(kind="preference", topic="editor", content="User prefers neovim.")
        m.embedding = [1.0, 0.0, 0.0]
        with patch("memory.store.generate_embedding", return_value=None):
            save_memory(m, db_path=tmp_db)

        with patch("memory.retriever.generate_embedding", return_value=[0.99, 0.1, 0.0]):
            results = get_relevant_memories("which editor", db_path=tmp_db)

        assert len(results) == 1
        assert results[0].topic == "editor"

    def test_cosine_search_filters_irrelevant(self, tmp_db):
        m = _mem(kind="preference", topic="editor", content="User prefers neovim.")
        m.embedding = [1.0, 0.0, 0.0]
        with patch("memory.store.generate_embedding", return_value=None):
            save_memory(m, db_path=tmp_db)

        # Query embedding is orthogonal to memory embedding → score = 0 < threshold.
        with patch("memory.retriever.generate_embedding", return_value=[0.0, 0.0, 1.0]):
            results = get_relevant_memories("something unrelated", db_path=tmp_db)

        assert results == []

    def test_falls_back_to_keyword_when_ollama_unavailable(self, tmp_db):
        with patch("memory.store.generate_embedding", return_value=None):
            save_memory(_mem(topic="fedora", content="User prefers Fedora."), db_path=tmp_db)

        with patch("memory.retriever.generate_embedding", return_value=None):
            results = get_relevant_memories("fedora linux", db_path=tmp_db)

        assert any("Fedora" in m.content for m in results)

    def test_legacy_memories_included_via_keyword_fallback(self, tmp_db):
        # A memory with no embedding (legacy v1 row) is served via keyword
        # search even when a query embedding is available.
        with patch("memory.store.generate_embedding", return_value=None):
            save_memory(_mem(topic="fedora", content="User prefers Fedora."), db_path=tmp_db)

        with patch("memory.retriever.generate_embedding", return_value=[0.5, 0.5]):
            results = get_relevant_memories("fedora linux", db_path=tmp_db)

        assert any("Fedora" in m.content for m in results)
