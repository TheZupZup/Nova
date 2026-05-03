import sqlite3

import bcrypt
import pytest

from core import users


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "nova.db")


def _connect(path):
    return sqlite3.connect(path)


def test_create_users_table_is_idempotent(db_path):
    with _connect(db_path) as conn:
        users.create_users_table(conn)
        users.create_users_table(conn)  # second call must not raise

        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    assert {
        "id",
        "username",
        "password_hash",
        "role",
        "is_restricted",
        "token_version",
        "created_at",
        "disabled_at",
    } <= cols


def test_create_user_hashes_password_and_returns_id(db_path):
    with _connect(db_path) as conn:
        users.create_users_table(conn)
        uid = users.create_user(conn, "alice", "s3cret", role=users.ROLE_USER)

        row = users.get_user_by_username(conn, "alice")
    assert uid > 0
    assert row is not None
    assert row["username"] == "alice"
    assert row["role"] == "user"
    assert row["is_restricted"] == 0
    assert row["token_version"] == 1
    # password_hash is a bcrypt hash, not the plaintext
    assert row["password_hash"] != "s3cret"
    assert bcrypt.checkpw(b"s3cret", row["password_hash"].encode())


def test_create_user_rejects_invalid_role(db_path):
    with _connect(db_path) as conn:
        users.create_users_table(conn)
        with pytest.raises(ValueError):
            users.create_user(conn, "x", "y", role="superuser")


def test_create_user_rejects_empty_credentials(db_path):
    with _connect(db_path) as conn:
        users.create_users_table(conn)
        with pytest.raises(ValueError):
            users.create_user(conn, "", "p")
        with pytest.raises(ValueError):
            users.create_user(conn, "u", "")


def test_username_uniqueness_enforced(db_path):
    with _connect(db_path) as conn:
        users.create_users_table(conn)
        users.create_user(conn, "dup", "a")
        with pytest.raises(sqlite3.IntegrityError):
            users.create_user(conn, "dup", "b")


def test_seed_default_admin_only_when_empty(db_path):
    with _connect(db_path) as conn:
        users.create_users_table(conn)
        first = users.seed_default_admin(conn, "admin", "pw")
        second = users.seed_default_admin(conn, "admin", "pw")
        third = users.seed_default_admin(conn, "other", "pw2")

        rows = conn.execute("SELECT username, role FROM users").fetchall()

    assert first is not None
    assert second is None
    assert third is None
    assert rows == [("admin", "admin")]


def test_get_user_by_username_returns_none_for_missing(db_path):
    with _connect(db_path) as conn:
        users.create_users_table(conn)
        assert users.get_user_by_username(conn, "ghost") is None
