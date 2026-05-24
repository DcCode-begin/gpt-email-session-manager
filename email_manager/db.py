from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .security import PlainTextProtector


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_group_name(name: str) -> str:
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("分组名称不能为空。")
    if len(normalized) > 60:
        raise ValueError("分组名称不能超过 60 个字符。")
    return normalized


def _infer_provider_from_email(email: str) -> str:
    domain = str(email or "").split("@")[-1].lower()
    if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        return "Outlook"
    if domain == "gmail.com":
        return "Gmail"
    if domain == "qq.com":
        return "QQ"
    if domain == "163.com":
        return "163"
    if domain == "126.com":
        return "126"
    if domain in {"icloud.com", "me.com"}:
        return "iCloud"
    if domain == "yahoo.com":
        return "Yahoo"
    return ""


INVALID_GROUP = "失效账号"
LEGACY_INVALID_GROUPS = ("失效邮箱",)
ISOLATED_GROUPS = (INVALID_GROUP,)


class EmailDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.protector = PlainTextProtector(path.parent)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password_encrypted TEXT NOT NULL,
                    oauth_client_id TEXT DEFAULT '',
                    oauth_refresh_token_encrypted TEXT DEFAULT '',
                    provider TEXT DEFAULT '',
                    imap_host TEXT DEFAULT '',
                    imap_port INTEGER DEFAULT 993,
                    group_name TEXT DEFAULT '',
                    previous_group_name TEXT DEFAULT '',
                    tags TEXT DEFAULT '',
                    remark TEXT DEFAULT '',
                    chatgpt_session_encrypted TEXT DEFAULT '',
                    chatgpt_session_updated_at TEXT DEFAULT '',
                    status TEXT DEFAULT 'unknown',
                    last_error TEXT DEFAULT '',
                    last_check_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS verification_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email_id INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    source_subject TEXT DEFAULT '',
                    source_from TEXT DEFAULT '',
                    received_at TEXT DEFAULT '',
                    copied_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS groups (
                    name TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    email_id INTEGER,
                    detail TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE SET NULL
                );
                """
            )
            self._ensure_column(conn, "emails", "oauth_client_id", "TEXT DEFAULT ''")
            self._ensure_column(
                conn,
                "emails",
                "oauth_refresh_token_encrypted",
                "TEXT DEFAULT ''",
            )
            self._ensure_column(conn, "emails", "previous_group_name", "TEXT DEFAULT ''")
            self._ensure_column(
                conn,
                "emails",
                "chatgpt_session_encrypted",
                "TEXT DEFAULT ''",
            )
            self._ensure_column(
                conn,
                "emails",
                "chatgpt_session_updated_at",
                "TEXT DEFAULT ''",
            )
            self._set_default(conn, "auto_lock_seconds", "300")
            self._set_default(conn, "clipboard_clear_seconds", "30")
            self._set_default(conn, "chatgpt_proxy", "")
            conn.execute("DELETE FROM settings WHERE key LIKE ?", ("master" + "_password_%",))
            self._migrate_legacy_invalid_groups(conn)
            for group_name in ISOLATED_GROUPS:
                self._ensure_group(conn, group_name)
            self._backfill_previous_groups(conn)
            self._migrate_secrets_to_plaintext(conn)

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _set_default(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )

    def _ensure_group(self, conn: sqlite3.Connection, group_name: str) -> None:
        group_name = (group_name or "").strip()
        if not group_name:
            return
        now = now_iso()
        conn.execute(
            "INSERT OR IGNORE INTO groups (name, created_at, updated_at) VALUES (?, ?, ?)",
            (group_name, now, now),
        )

    def _backfill_previous_groups(self, conn: sqlite3.Connection) -> None:
        placeholders = ",".join("?" for _ in ISOLATED_GROUPS)
        conn.execute(
            f"""
            UPDATE emails
               SET previous_group_name = COALESCE(NULLIF(provider, ''), '')
             WHERE group_name IN ({placeholders})
               AND COALESCE(previous_group_name, '') = ''
            """,
            ISOLATED_GROUPS,
        )

    def _migrate_legacy_invalid_groups(self, conn: sqlite3.Connection) -> None:
        self._ensure_group(conn, INVALID_GROUP)
        now = now_iso()
        for group_name in LEGACY_INVALID_GROUPS:
            conn.execute(
                """
                UPDATE emails
                   SET group_name = ?,
                       previous_group_name = COALESCE(NULLIF(previous_group_name, ''), NULLIF(provider, ''), ''),
                       updated_at = ?
                 WHERE group_name = ?
                """,
                (INVALID_GROUP, now, group_name),
            )
            conn.execute("DELETE FROM groups WHERE name = ?", (group_name,))

    def _migrate_secrets_to_plaintext(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT id, password_encrypted,
                   oauth_refresh_token_encrypted,
                   chatgpt_session_encrypted
              FROM emails
            """
        ).fetchall()
        updates = []
        for row in rows:
            password = self.protector.unprotect(row["password_encrypted"] or "")
            oauth_refresh_token = self.protector.unprotect(
                row["oauth_refresh_token_encrypted"] or ""
            )
            chatgpt_session = self.protector.unprotect(
                row["chatgpt_session_encrypted"] or ""
            )
            if (
                password != (row["password_encrypted"] or "")
                or oauth_refresh_token != (row["oauth_refresh_token_encrypted"] or "")
                or chatgpt_session != (row["chatgpt_session_encrypted"] or "")
            ):
                updates.append(
                    (
                        password,
                        oauth_refresh_token,
                        chatgpt_session,
                        now_iso(),
                        row["id"],
                    )
                )
        if updates:
            conn.executemany(
                """
                UPDATE emails
                   SET password_encrypted = ?,
                       oauth_refresh_token_encrypted = ?,
                       chatgpt_session_encrypted = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                updates,
            )

    def get_settings(self) -> dict:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def update_settings(self, settings: dict) -> dict:
        allowed = {"auto_lock_seconds", "clipboard_clear_seconds", "chatgpt_proxy"}
        with self.connect() as conn:
            for key, value in settings.items():
                if key in allowed:
                    conn.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)),
                    )
        return self.get_settings()

    def has_password_gate(self) -> bool:
        return False

    def disable_password_gate(self, password: str = "") -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM settings WHERE key LIKE ?", ("master" + "_password_%",))
            self._log(conn, "disable_password_gate", None, "")
        return
        if len(password) < 6:
            raise ValueError("主密码至少需要 6 位。")
        password_hash = {"salt": "", "hash": "", "rounds": "0"}
        with self.connect() as conn:
            for key, value in (
                ("disabled_password_salt", password_hash["salt"]),
                ("disabled_password_hash", password_hash["hash"]),
                ("disabled_password_rounds", password_hash["rounds"]),
            ):
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            self._log(conn, "setup_password_disabled", None, "")

    def password_gate_allows_access(self, password: str = "") -> bool:
        return True

    def list_groups(self) -> list[dict]:
        with self.connect() as conn:
            group_rows = conn.execute(
                "SELECT name, created_at, updated_at FROM groups"
            ).fetchall()
            count_rows = conn.execute(
                """
                SELECT group_name AS name, COUNT(*) AS email_count
                  FROM emails
                 WHERE COALESCE(group_name, '') <> ''
                 GROUP BY group_name
                """
            ).fetchall()

        groups: dict[str, dict] = {}
        for row in group_rows:
            name = row["name"] or ""
            if not name:
                continue
            groups[name] = {
                "name": name,
                "email_count": 0,
                "protected": name in ISOLATED_GROUPS,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        for row in count_rows:
            name = row["name"] or ""
            if not name:
                continue
            item = groups.setdefault(
                name,
                {
                    "name": name,
                    "email_count": 0,
                    "protected": name in ISOLATED_GROUPS,
                    "created_at": "",
                    "updated_at": "",
                },
            )
            item["email_count"] = int(row["email_count"] or 0)
        for name in ISOLATED_GROUPS:
            groups.setdefault(
                name,
                {
                    "name": name,
                    "email_count": 0,
                    "protected": True,
                    "created_at": "",
                    "updated_at": "",
                },
            )
        return sorted(groups.values(), key=lambda item: (item["protected"], item["name"]))

    def create_group(self, name: str) -> dict:
        name = _normalize_group_name(name)
        with self.connect() as conn:
            if self._group_exists(conn, name):
                raise ValueError("分组已存在。")
            self._ensure_group(conn, name)
            self._log(conn, "create_group", None, json.dumps({"name": name}, ensure_ascii=False))
        return self._group_by_name(name)

    def rename_group(self, old_name: str, new_name: str) -> dict:
        old_name = _normalize_group_name(old_name)
        new_name = _normalize_group_name(new_name)
        if old_name in ISOLATED_GROUPS:
            raise ValueError("内置失效分组不能重命名。")
        if new_name in ISOLATED_GROUPS:
            raise ValueError("不能重命名为内置失效分组。")
        if old_name == new_name:
            return self._group_by_name(new_name)
        updated_at = now_iso()
        with self.connect() as conn:
            if self._group_exists(conn, new_name):
                raise ValueError("目标分组已存在。")
            old_count = conn.execute(
                "SELECT COUNT(*) AS count FROM emails WHERE group_name = ?",
                (old_name,),
            ).fetchone()["count"]
            if not self._group_exists(conn, old_name) and int(old_count or 0) == 0:
                raise KeyError("分组不存在。")
            conn.execute(
                "INSERT INTO groups (name, created_at, updated_at) VALUES (?, ?, ?)",
                (new_name, updated_at, updated_at),
            )
            conn.execute("DELETE FROM groups WHERE name = ?", (old_name,))
            conn.execute(
                "UPDATE emails SET group_name = ?, updated_at = ? WHERE group_name = ?",
                (new_name, updated_at, old_name),
            )
            self._log(
                conn,
                "rename_group",
                None,
                json.dumps({"old": old_name, "new": new_name}, ensure_ascii=False),
            )
        return self._group_by_name(new_name)

    def delete_group(self, name: str) -> dict:
        name = _normalize_group_name(name)
        if name in ISOLATED_GROUPS:
            raise ValueError("内置失效分组不能删除。")
        updated_at = now_iso()
        with self.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM emails WHERE group_name = ?",
                (name,),
            ).fetchone()["count"]
            existed = self._group_exists(conn, name)
            if not existed and int(count or 0) == 0:
                raise KeyError("分组不存在。")
            conn.execute("DELETE FROM groups WHERE name = ?", (name,))
            conn.execute(
                "UPDATE emails SET group_name = '', updated_at = ? WHERE group_name = ?",
                (updated_at, name),
            )
            self._log(
                conn,
                "delete_group",
                None,
                json.dumps({"name": name, "cleared": int(count or 0)}, ensure_ascii=False),
            )
        return {"name": name, "cleared": int(count or 0)}

    def _group_by_name(self, name: str) -> dict:
        for group in self.list_groups():
            if group["name"] == name:
                return group
        raise KeyError("分组不存在。")

    def _group_exists(self, conn: sqlite3.Connection, name: str) -> bool:
        if conn.execute("SELECT 1 FROM groups WHERE name = ?", (name,)).fetchone():
            return True
        if conn.execute(
            "SELECT 1 FROM emails WHERE group_name = ? LIMIT 1",
            (name,),
        ).fetchone():
            return True
        return name in ISOLATED_GROUPS

    def list_emails(self, filters: dict | None = None) -> list[dict]:
        filters = filters or {}
        sql = """
            SELECT e.*,
                   vc.code AS latest_code,
                   vc.source_subject AS latest_code_subject,
                   vc.source_from AS latest_code_from,
                   vc.received_at AS latest_code_received_at
            FROM emails e
            LEFT JOIN verification_codes vc
              ON vc.id = (
                  SELECT id FROM verification_codes
                  WHERE email_id = e.id
                  ORDER BY received_at DESC, id DESC
                  LIMIT 1
              )
        """
        clauses: list[str] = []
        params: list[str] = []

        ids = filters.get("ids")
        requested_group = (filters.get("group_name") or "").strip()
        if not ids and requested_group not in ISOLATED_GROUPS:
            placeholders = ",".join("?" for _ in ISOLATED_GROUPS)
            clauses.append(f"COALESCE(e.group_name, '') NOT IN ({placeholders})")
            params.extend(ISOLATED_GROUPS)

        search = (filters.get("search") or "").strip()
        if search:
            clauses.append(
                "(e.email LIKE ? OR e.provider LIKE ? OR e.group_name LIKE ? "
                "OR e.tags LIKE ? OR e.remark LIKE ?)"
            )
            like = f"%{search}%"
            params.extend([like, like, like, like, like])

        for key in ("provider", "group_name", "status"):
            value = (filters.get(key) or "").strip()
            if value:
                clauses.append(f"e.{key} = ?")
                params.append(value)

        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"e.id IN ({placeholders})")
            params.extend([str(item) for item in ids])

        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY e.id ASC"

        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._public_email(row) for row in rows]

    def get_email(self, email_id: int) -> sqlite3.Row:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
        if not row:
            raise KeyError("邮箱不存在。")
        return row

    def get_email_by_address(self, email_address: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM emails WHERE lower(email) = lower(?)",
                (email_address.strip(),),
            ).fetchone()

    def get_decrypted_password(self, email_id: int, action: str = "view_password") -> str:
        row = self.get_email(email_id)
        password = self.protector.unprotect(row["password_encrypted"])
        with self.connect() as conn:
            self._log(conn, action, email_id, "")
        return password

    def get_decrypted_oauth_refresh_token(self, email_id: int) -> str:
        row = self.get_email(email_id)
        encrypted = row["oauth_refresh_token_encrypted"] or ""
        if not encrypted:
            return ""
        return self.protector.unprotect(encrypted)

    def create_email(self, data: dict) -> dict:
        created_at = now_iso()
        email = data.get("email", "").strip()
        provider = data.get("provider", "").strip() or _infer_provider_from_email(email)
        encrypted = self.protector.protect(data.get("password", ""))
        oauth_refresh_token = data.get("oauth_refresh_token", "")
        oauth_refresh_token_encrypted = (
            self.protector.protect(oauth_refresh_token) if oauth_refresh_token else ""
        )
        group_name = data.get("group_name", "").strip() or provider or "其他"
        if group_name:
            group_name = _normalize_group_name(group_name)
        with self.connect() as conn:
            self._ensure_group(conn, group_name)
            cursor = conn.execute(
                """
                INSERT INTO emails (
                    email, password_encrypted, oauth_client_id,
                    oauth_refresh_token_encrypted, provider, imap_host, imap_port,
                    group_name, tags, remark, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email,
                    encrypted,
                    data.get("oauth_client_id", "").strip(),
                    oauth_refresh_token_encrypted,
                    provider,
                    data.get("imap_host", "").strip(),
                    int(data.get("imap_port") or 993),
                    group_name,
                    data.get("tags", "").strip(),
                    data.get("remark", "").strip(),
                    created_at,
                    created_at,
                ),
            )
            email_id = cursor.lastrowid
            self._log(conn, "create_email", email_id, "")
        return self.list_emails({"ids": [email_id]})[0]

    def update_email(self, email_id: int, data: dict) -> dict:
        current = self.get_email(email_id)
        email = data.get("email", current["email"]).strip()
        provider = data.get("provider", "").strip() or _infer_provider_from_email(email) or (current["provider"] or "")
        encrypted = current["password_encrypted"]
        if data.get("password"):
            encrypted = self.protector.protect(data["password"])
        oauth_client_id = current["oauth_client_id"] or ""
        oauth_refresh_token_encrypted = current["oauth_refresh_token_encrypted"] or ""
        if "oauth_client_id" in data:
            oauth_client_id = data.get("oauth_client_id", "").strip()
        if data.get("oauth_refresh_token"):
            oauth_refresh_token_encrypted = self.protector.protect(
                data["oauth_refresh_token"]
            )
        group_name = data.get("group_name", current["group_name"]).strip()
        if group_name:
            group_name = _normalize_group_name(group_name)

        with self.connect() as conn:
            self._ensure_group(conn, group_name)
            conn.execute(
                """
                UPDATE emails
                   SET email = ?,
                       password_encrypted = ?,
                       oauth_client_id = ?,
                       oauth_refresh_token_encrypted = ?,
                       provider = ?,
                       imap_host = ?,
                       imap_port = ?,
                       group_name = ?,
                       tags = ?,
                       remark = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    email,
                    encrypted,
                    oauth_client_id,
                    oauth_refresh_token_encrypted,
                    provider,
                    data.get("imap_host", current["imap_host"]).strip(),
                    int(data.get("imap_port") or current["imap_port"] or 993),
                    group_name,
                    data.get("tags", current["tags"]).strip(),
                    data.get("remark", current["remark"]).strip(),
                    now_iso(),
                    email_id,
                ),
            )
            self._log(conn, "update_email", email_id, "")
        return self.list_emails({"ids": [email_id]})[0]

    def delete_email(self, email_id: int) -> None:
        with self.connect() as conn:
            self._log(conn, "delete_email", email_id, "")
            conn.execute("UPDATE operation_logs SET email_id = NULL WHERE email_id = ?", (email_id,))
            conn.execute("DELETE FROM emails WHERE id = ?", (email_id,))

    def bulk_delete(self, ids: Iterable[int]) -> int:
        ids = [int(item) for item in ids]
        if not ids:
            return 0
        with self.connect() as conn:
            conn.executemany(
                "UPDATE operation_logs SET email_id = NULL WHERE email_id = ?",
                [(item,) for item in ids],
            )
            conn.executemany("DELETE FROM emails WHERE id = ?", [(item,) for item in ids])
            self._log(conn, "bulk_delete", None, json.dumps({"count": len(ids)}))
        return len(ids)

    def bulk_group(self, ids: Iterable[int], group_name: str) -> int:
        ids = [int(item) for item in ids]
        if not ids:
            return 0
        group_name = group_name.strip()
        if group_name:
            group_name = _normalize_group_name(group_name)
        updated_at = now_iso()
        with self.connect() as conn:
            self._ensure_group(conn, group_name)
            if group_name in ISOLATED_GROUPS:
                rows = conn.execute(
                    f"SELECT id, group_name, previous_group_name, provider FROM emails WHERE id IN ({','.join('?' for _ in ids)})",
                    ids,
                ).fetchall()
                updates = []
                for row in rows:
                    previous_group = (row["previous_group_name"] or "").strip()
                    current_group = (row["group_name"] or "").strip()
                    if current_group and current_group not in ISOLATED_GROUPS:
                        previous_group = current_group
                    if not previous_group:
                        previous_group = (row["provider"] or "").strip()
                    updates.append((group_name, previous_group, updated_at, row["id"]))
                conn.executemany(
                    """
                    UPDATE emails
                       SET group_name = ?,
                           previous_group_name = ?,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    updates,
                )
            else:
                conn.executemany(
                    """
                    UPDATE emails
                       SET group_name = ?,
                           previous_group_name = '',
                           updated_at = ?
                     WHERE id = ?
                    """,
                    [(group_name, updated_at, item) for item in ids],
                )
            self._log(conn, "bulk_group", None, json.dumps({"count": len(ids), "group": group_name}))
        return len(ids)

    def restore_previous_groups(self, ids: Iterable[int]) -> int:
        ids = [int(item) for item in ids]
        if not ids:
            return 0
        updated_at = now_iso()
        restored = 0
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT id, group_name, previous_group_name, provider FROM emails WHERE id IN ({','.join('?' for _ in ids)})",
                ids,
            ).fetchall()
            for row in rows:
                current_group = (row["group_name"] or "").strip()
                if current_group not in ISOLATED_GROUPS:
                    continue
                target_group = (row["previous_group_name"] or "").strip()
                if not target_group or target_group in ISOLATED_GROUPS:
                    target_group = (row["provider"] or "").strip()
                if target_group in ISOLATED_GROUPS:
                    target_group = ""
                self._ensure_group(conn, target_group)
                conn.execute(
                    """
                    UPDATE emails
                       SET group_name = ?,
                           previous_group_name = '',
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (target_group, updated_at, row["id"]),
                )
                restored += 1
            self._log(conn, "restore_previous_group", None, json.dumps({"count": restored}))
        return restored

    def update_status(
        self,
        email_id: int,
        status: str,
        error: str = "",
        log_action: str = "test_imap",
    ) -> dict:
        checked_at = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE emails
                   SET status = ?, last_error = ?, last_check_at = ?, updated_at = ?
                 WHERE id = ?
                """,
                (status, error, checked_at, checked_at, email_id),
            )
            self._log(conn, log_action, email_id, json.dumps({"status": status, "error": error}, ensure_ascii=False))
        return self.list_emails({"ids": [email_id]})[0]

    def save_code(self, email_id: int, code_data: dict | None) -> dict | None:
        if not code_data:
            with self.connect() as conn:
                self._log(conn, "fetch_code_empty", email_id, "")
            return None
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO verification_codes (
                    email_id, code, source_subject, source_from,
                    received_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    email_id,
                    code_data["code"],
                    code_data.get("subject", ""),
                    code_data.get("sender", ""),
                    code_data.get("received_at", now_iso()),
                    now_iso(),
                ),
            )
            self._log(conn, "fetch_code", email_id, json.dumps({"code": "***"}, ensure_ascii=False))
        return self.latest_code(email_id)

    def save_chatgpt_session(self, email_id: int, session_text: str) -> dict:
        session_text = (session_text or "").strip()
        if not session_text:
            raise ValueError("ChatGPT session 内容为空，无法保存。")
        encrypted = self.protector.protect(session_text)
        saved_at = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE emails
                   SET chatgpt_session_encrypted = ?,
                       chatgpt_session_updated_at = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (encrypted, saved_at, saved_at, email_id),
            )
            self._log(conn, "save_chatgpt_session", email_id, json.dumps({"saved_at": saved_at}))
        return self.list_emails({"ids": [email_id]})[0]

    def clear_chatgpt_session(self, email_id: int, log_action: str = "clear_chatgpt_session") -> dict:
        cleared_at = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE emails
                   SET chatgpt_session_encrypted = '',
                       chatgpt_session_updated_at = '',
                       updated_at = ?
                 WHERE id = ?
                """,
                (cleared_at, email_id),
            )
            self._log(conn, log_action, email_id, json.dumps({"cleared_at": cleared_at}))
        return self.list_emails({"ids": [email_id]})[0]

    def clear_all_chatgpt_sessions(self) -> int:
        cleared_at = now_iso()
        with self.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM emails WHERE COALESCE(chatgpt_session_encrypted, '') <> ''"
            ).fetchone()[0]
            conn.execute(
                """
                UPDATE emails
                   SET chatgpt_session_encrypted = '',
                       chatgpt_session_updated_at = '',
                       updated_at = ?
                 WHERE COALESCE(chatgpt_session_encrypted, '') <> ''
                """,
                (cleared_at,),
            )
            self._log(
                conn,
                "clear_all_chatgpt_sessions",
                None,
                json.dumps({"count": count, "cleared_at": cleared_at}),
            )
        return int(count)

    def get_decrypted_chatgpt_session(self, email_id: int) -> str:
        row = self.get_email(email_id)
        if (row["status"] or "") == "banned":
            raise ValueError("这个账号已被标记为被封，旧 ChatGPT session 已不可用。")
        encrypted = row["chatgpt_session_encrypted"] or ""
        if not encrypted:
            raise ValueError("这个账号还没有保存 ChatGPT session，请先点 GPT登录。")
        session_text = self.protector.unprotect(encrypted)
        with self.connect() as conn:
            self._log(conn, "copy_chatgpt_session", email_id, "")
        return session_text

    def list_decrypted_chatgpt_sessions(self, ids: Iterable[int] | None = None) -> list[dict]:
        ids = [int(item) for item in (ids or [])]
        clauses = [
            "COALESCE(chatgpt_session_encrypted, '') <> ''",
            "COALESCE(status, '') <> 'banned'",
        ]
        params: list[int] = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"id IN ({placeholders})")
            params.extend(ids)

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, email, provider, group_name,
                       chatgpt_session_encrypted, chatgpt_session_updated_at
                  FROM emails
                 WHERE {" AND ".join(clauses)}
                 ORDER BY id ASC
                """,
                params,
            ).fetchall()

        sessions = []
        for row in rows:
            sessions.append(
                {
                    "email_id": row["id"],
                    "email": row["email"],
                    "provider": row["provider"] or "",
                    "group_name": row["group_name"] or "",
                    "saved_at": row["chatgpt_session_updated_at"] or "",
                    "session_text": self.protector.unprotect(row["chatgpt_session_encrypted"]),
                }
            )
        return sessions

    def latest_code(self, email_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM verification_codes
                 WHERE email_id = ?
                 ORDER BY received_at DESC, id DESC
                 LIMIT 1
                """,
                (email_id,),
            ).fetchone()
        return dict(row) if row else None

    def mark_code_copied(self, email_id: int) -> None:
        copied_at = now_iso()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM verification_codes
                 WHERE email_id = ?
                 ORDER BY received_at DESC, id DESC
                 LIMIT 1
                """,
                (email_id,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE verification_codes SET copied_at = ? WHERE id = ?",
                    (copied_at, row["id"]),
                )
            self._log(conn, "copy_code", email_id, "")

    def import_preview(self, rows: list[dict]) -> dict:
        existing = {item["email"].lower() for item in self.list_emails()}
        seen: set[str] = set()
        preview = []
        valid_count = 0
        for index, row in enumerate(rows, start=1):
            normalized = self._normalize_import_row(row)
            errors = []
            email_value = normalized["email"].lower()
            if not normalized["email"]:
                errors.append("缺少邮箱账号")
            elif "@" not in normalized["email"]:
                errors.append("邮箱格式不正确")
            if not normalized["password"]:
                errors.append("缺少密码或授权码")
            is_update = email_value in existing
            if email_value in seen:
                errors.append("导入文件中重复")
            seen.add(email_value)
            if not errors:
                valid_count += 1
            preview.append(
                {
                    "row": index,
                    "data": normalized,
                    "errors": errors,
                    "mode": "update" if is_update and not errors else "create",
                }
            )
        return {"rows": preview, "valid_count": valid_count, "error_count": len(preview) - valid_count}

    def import_commit(self, rows: list[dict]) -> dict:
        preview = self.import_preview(rows)
        created = 0
        updated = 0
        skipped = 0
        for item in preview["rows"]:
            if item["errors"]:
                skipped += 1
                continue
            existing = self.get_email_by_address(item["data"]["email"])
            if existing:
                self.update_email(existing["id"], item["data"])
                updated += 1
            else:
                self.create_email(item["data"])
                created += 1
        with self.connect() as conn:
            self._log(
                conn,
                "import_emails",
                None,
                json.dumps({"created": created, "updated": updated, "skipped": skipped}),
            )
        return {"created": created, "updated": updated, "skipped": skipped}

    def log_action(self, action: str, email_id: int | None = None, detail: str = "") -> None:
        with self.connect() as conn:
            self._log(conn, action, email_id, detail)

    def list_logs(self, limit: int = 80) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT l.*, e.email
                  FROM operation_logs l
                  LEFT JOIN emails e ON e.id = l.email_id
                 ORDER BY l.id DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _public_email(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "email": row["email"],
            "password": self.protector.unprotect(row["password_encrypted"]),
            "provider": row["provider"] or "",
            "has_oauth": bool(row["oauth_client_id"] and row["oauth_refresh_token_encrypted"]),
            "imap_host": row["imap_host"] or "",
            "imap_port": row["imap_port"] or 993,
            "group_name": row["group_name"] or "",
            "previous_group_name": row["previous_group_name"] or "",
            "tags": row["tags"] or "",
            "remark": row["remark"] or "",
            "has_chatgpt_session": bool(row["chatgpt_session_encrypted"]) and (row["status"] or "") != "banned",
            "chatgpt_session_updated_at": row["chatgpt_session_updated_at"] or "",
            "status": row["status"] or "unknown",
            "last_error": row["last_error"] or "",
            "last_check_at": row["last_check_at"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "latest_code": row["latest_code"] or "",
            "latest_code_subject": row["latest_code_subject"] or "",
            "latest_code_from": row["latest_code_from"] or "",
            "latest_code_received_at": row["latest_code_received_at"] or "",
        }

    def _normalize_import_row(self, row: dict) -> dict:
        email = str(row.get("email", "")).strip()
        provider = str(row.get("provider", "")).strip() or _infer_provider_from_email(email)
        group_name = str(row.get("group_name", row.get("group", ""))).strip()
        if not group_name:
            group_name = provider or "其他"
        return {
            "email": email,
            "password": str(row.get("password", "")).strip(),
            "oauth_client_id": str(row.get("oauth_client_id", "")).strip(),
            "oauth_refresh_token": str(row.get("oauth_refresh_token", "")).strip(),
            "provider": provider,
            "imap_host": str(row.get("imap_host", "")).strip(),
            "imap_port": str(row.get("imap_port", "") or "993").strip(),
            "group_name": group_name,
            "tags": str(row.get("tags", "")).strip(),
            "remark": str(row.get("remark", "")).strip(),
        }

    def _log(
        self,
        conn: sqlite3.Connection,
        action: str,
        email_id: int | None,
        detail: str,
    ) -> None:
        conn.execute(
            "INSERT INTO operation_logs (action, email_id, detail, created_at) VALUES (?, ?, ?, ?)",
            (action, email_id, detail, now_iso()),
        )
