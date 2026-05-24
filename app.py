from __future__ import annotations

import csv
import concurrent.futures
import io
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from email_manager.chatgpt_login import (
    ChatGPTLoginConfig,
    CHATGPT_SESSION_URL,
    _set_windows_clipboard_text,
    is_chatgpt_account_banned_message,
    run_chatgpt_login,
)
from email_manager.db import EmailDatabase
from email_manager.imap_tools import (
    PROVIDER_DEFAULTS,
    fetch_latest_code,
    provider_defaults,
    test_imap_connection,
)


ROOT = Path(os.environ.get("EMAIL_MANAGER_ROOT", Path(__file__).resolve().parent)).resolve()
STATIC_DIR = ROOT / "static"
DATA_DIR = Path(os.environ.get("EMAIL_MANAGER_DATA_DIR", ROOT / "data")).resolve()
DB_PATH = DATA_DIR / "email_manager.db"
CHATGPT_PROFILE_DIR = DATA_DIR / "chatgpt_browser_profile"
LOGIN_JOBS: dict[str, dict] = {}
LOGIN_JOBS_LOCK = threading.Lock()
BULK_LOGIN_JOBS: dict[str, dict] = {}
BULK_LOGIN_JOBS_LOCK = threading.Lock()
DEFAULT_BULK_LOGIN_CONCURRENCY = 3
MAX_BULK_LOGIN_CONCURRENCY = 5
LOGIN_TERMINAL_STATUSES = {"ok", "failed", "closed", "banned", "retry"}
LOGIN_ACTIVE_STATUSES = {"pending", "running"}


class SessionState:
    def __init__(self) -> None:
        self.unlocked = True
        self.last_activity = 0.0

    def unlock(self) -> None:
        self.unlocked = True
        self.last_activity = time.monotonic()

    def lock(self) -> None:
        self.unlocked = True
        self.last_activity = time.monotonic()

    def is_unlocked(self, db: EmailDatabase) -> bool:
        self.last_activity = time.monotonic()
        return True


class AppHandler(BaseHTTPRequestHandler):
    db: EmailDatabase
    session: SessionState

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/status":
                self._status()
                return
            if path == "/api/provider-defaults":
                self._json({"providers": PROVIDER_DEFAULTS})
                return
            if path.startswith("/api/") and not self._require_unlock():
                return

            if path == "/api/emails":
                query = parse_qs(parsed.query)
                filters = {
                    "search": _first(query, "search"),
                    "provider": _first(query, "provider"),
                    "group_name": _first(query, "group_name"),
                    "status": _first(query, "status"),
                }
                self._json({"emails": self.db.list_emails(filters)})
                return
            if path == "/api/export":
                self._export(parse_qs(parsed.query))
                return
            if path == "/api/chatgpt-sessions/export":
                self._export_chatgpt_sessions(parse_qs(parsed.query))
                return
            if path == "/api/settings":
                self._json({"settings": self._public_settings()})
                return
            if path == "/api/groups":
                self._json({"groups": self.db.list_groups()})
                return
            if path == "/api/logs":
                self._json({"logs": self.db.list_logs()})
                return
            match = re.fullmatch(r"/api/chatgpt-login-jobs/([0-9a-f]+)", path)
            if match:
                job = _get_login_job(match.group(1))
                if not job:
                    self._json({"error": "登录任务不存在。"}, status=404)
                    return
                self._json({"job": job})
                return
            match = re.fullmatch(r"/api/chatgpt-bulk-login-jobs/([0-9a-f]+)", path)
            if match:
                job = _get_bulk_login_job(match.group(1))
                if not job:
                    self._json({"error": "批量登录任务不存在。"}, status=404)
                    return
                self._json({"job": job})
                return

            self._static(path)
        except Exception as exc:
            self._error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            body = self._json_body()
            if path == "/api/setup":
                self.session.unlock()
                self._json({"ok": True})
                return
            if path == "/api/unlock":
                self.session.unlock()
                self._json({"ok": True})
                return
            if path == "/api/lock":
                self._json({"ok": True})
                return
            if path.startswith("/api/") and not self._require_unlock():
                return

            if path == "/api/emails":
                self._json({"email": self.db.create_email(body)})
                return
            if path == "/api/import/preview":
                self._json(self.db.import_preview(body.get("rows", [])))
                return
            if path == "/api/import/commit":
                self._json(self.db.import_commit(body.get("rows", [])))
                return
            if path == "/api/log":
                self.db.log_action(
                    str(body.get("action", "")),
                    _optional_int(body.get("email_id")),
                    str(body.get("detail", "")),
                )
                self._json({"ok": True})
                return
            if path == "/api/groups":
                self._json({"group": self.db.create_group(str(body.get("name", "")))})
                return
            if path == "/api/bulk-delete":
                self._json({"deleted": self.db.bulk_delete(body.get("ids", []))})
                return
            if path == "/api/bulk-group":
                self._json(
                    {
                        "updated": self.db.bulk_group(
                            body.get("ids", []), str(body.get("group_name", ""))
                        )
                    }
                )
                return
            if path == "/api/bulk-restore-group":
                self._json({"restored": self.db.restore_previous_groups(body.get("ids", []))})
                return
            if path == "/api/bulk-test":
                self._json({"results": [self._test_imap(int(item)) for item in body.get("ids", [])]})
                return
            if path == "/api/bulk-fetch-codes":
                self._json({"results": [self._fetch_code(int(item)) for item in body.get("ids", [])]})
                return
            if path == "/api/bulk-chatgpt-login":
                self._json(
                    {
                        "job": self._start_bulk_chatgpt_login(
                            body.get("ids", []),
                            body.get("concurrency", DEFAULT_BULK_LOGIN_CONCURRENCY),
                        )
                    }
                )
                return
            if path == "/api/chatgpt-sessions/copy-all":
                ids = _ids_from_values(body.get("ids", []))
                sessions = self._chatgpt_sessions_import_payload(ids)
                text = json.dumps(sessions, ensure_ascii=False, indent=2)
                clipboard_copied = False
                clipboard_error = ""
                try:
                    _set_windows_clipboard_text(text)
                    clipboard_copied = True
                except Exception as exc:
                    clipboard_error = _friendly_network_error(exc)
                self.db.log_action(
                    "copy_all_chatgpt_sessions",
                    None,
                    json.dumps(
                        {
                            "count": len(sessions),
                            "requested_count": len(ids),
                            "format": "import_array",
                            "clipboard_copied": clipboard_copied,
                        }
                    ),
                )
                self._json(
                    {
                        "count": len(sessions),
                        "text": text,
                        "clipboard_copied": clipboard_copied,
                        "clipboard_error": clipboard_error,
                    }
                )
                return

            match = re.fullmatch(r"/api/emails/(\d+)/(password|test-imap|fetch-code|copy-code|chatgpt-login|copy-chatgpt-session|restore-group)", path)
            if match:
                email_id = int(match.group(1))
                action = match.group(2)
                if action == "password":
                    purpose = str(body.get("purpose", "view_password"))
                    password = self.db.get_decrypted_password(email_id, purpose)
                    self._json({"password": password})
                    return
                if action == "test-imap":
                    self._json({"email": self._test_imap(email_id)})
                    return
                if action == "fetch-code":
                    self._json(self._fetch_code(email_id))
                    return
                if action == "copy-code":
                    self.db.mark_code_copied(email_id)
                    self._json({"ok": True})
                    return
                if action == "copy-chatgpt-session":
                    session_text = self.db.get_decrypted_chatgpt_session(email_id)
                    self._json({"session": session_text})
                    return
                if action == "restore-group":
                    self._json({"restored": self.db.restore_previous_groups([email_id])})
                    return
                if action == "chatgpt-login":
                    self._json({"job": self._start_chatgpt_login(email_id)})
                    return

            self._json({"error": "接口不存在。"}, status=404)
        except Exception as exc:
            self._error(exc)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/") and not self._require_unlock():
                return
            body = self._json_body()
            match = re.fullmatch(r"/api/emails/(\d+)", path)
            if match:
                email_id = int(match.group(1))
                self._json({"email": self.db.update_email(email_id, body)})
                return
            if path == "/api/settings":
                self._json({"settings": self.db.update_settings(body)})
                return
            if path == "/api/groups":
                self._json(
                    {
                        "group": self.db.rename_group(
                            str(body.get("old_name", "")),
                            str(body.get("new_name", "")),
                        )
                    }
                )
                return
            self._json({"error": "接口不存在。"}, status=404)
        except Exception as exc:
            self._error(exc)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/") and not self._require_unlock():
                return
            body = self._json_body()
            if path == "/api/groups":
                self._json({"deleted": self.db.delete_group(str(body.get("name", "")))})
                return
            match = re.fullmatch(r"/api/emails/(\d+)", path)
            if match:
                self.db.delete_email(int(match.group(1)))
                self._json({"ok": True})
                return
            self._json({"error": "接口不存在。"}, status=404)
        except Exception as exc:
            self._error(exc)

    def _status(self) -> None:
        settings = self._public_settings()
        count = len(self.db.list_emails())
        self._json(
            {
                "setup_required": False,
                "unlocked": True,
                "email_count": count,
                "settings": settings,
            }
        )

    def _test_imap(self, email_id: int) -> dict:
        try:
            row = self.db.get_email(email_id)
            password = self.db.get_decrypted_password(email_id, "test_imap_password_access")
            oauth_refresh_token = self.db.get_decrypted_oauth_refresh_token(email_id)
            host, port = _imap_settings(row)
            if not host:
                return self.db.update_status(email_id, "failed", "缺少 IMAP 服务器地址。")
            test_imap_connection(
                row["email"],
                password,
                host,
                int(port),
                row["oauth_client_id"] or "",
                oauth_refresh_token,
            )
            return self.db.update_status(email_id, "ok", "")
        except Exception as exc:
            return self.db.update_status(email_id, "failed", _friendly_network_error(exc))

    def _fetch_code(self, email_id: int) -> dict:
        try:
            row = self.db.get_email(email_id)
            password = self.db.get_decrypted_password(email_id, "fetch_code_password_access")
            oauth_refresh_token = self.db.get_decrypted_oauth_refresh_token(email_id)
            host, port = _imap_settings(row)
            if not host:
                self.db.update_status(email_id, "failed", "缺少 IMAP 服务器地址。")
                return {"code": None, "email": self.db.list_emails({"ids": [email_id]})[0]}
            code = fetch_latest_code(
                row["email"],
                password,
                host,
                int(port),
                row["oauth_client_id"] or "",
                oauth_refresh_token,
            )
            self.db.update_status(email_id, "ok", "")
            saved = self.db.save_code(email_id, code.__dict__ if code else None)
            return {"code": saved, "email": self.db.list_emails({"ids": [email_id]})[0]}
        except Exception as exc:
            error = _friendly_network_error(exc)
            updated = self.db.update_status(email_id, "failed", error)
            return {"code": None, "email": updated, "error": error}

    def _chatgpt_login_config(
        self,
        email_id: int,
        row,
        session_callback,
        copy_session_to_clipboard: bool = True,
    ) -> ChatGPTLoginConfig:
        password = self.db.get_decrypted_password(email_id, "chatgpt_login_password_access")
        oauth_refresh_token = self.db.get_decrypted_oauth_refresh_token(email_id)
        host, port = _imap_settings(row)
        if not host:
            raise ValueError("缺少 IMAP 服务地址，无法自动读取 ChatGPT 验证码。")

        return ChatGPTLoginConfig(
            email_address=row["email"],
            password=password,
            imap_host=host,
            imap_port=int(port),
            oauth_client_id=row["oauth_client_id"] or "",
            oauth_refresh_token=oauth_refresh_token,
            user_data_dir=CHATGPT_PROFILE_DIR / f"email_{email_id}",
            session_callback=session_callback,
            cleanup_user_data_dir=False,
            copy_session_to_clipboard=copy_session_to_clipboard,
            proxy_server=self._public_settings().get("chatgpt_proxy", ""),
        )

    def _start_chatgpt_login(self, email_id: int) -> dict:
        row = self.db.get_email(email_id)
        active_job = _find_active_login_job()
        if active_job:
            if int(active_job.get("email_id") or 0) != email_id:
                raise ValueError(f"{active_job.get('email')} 的 GPT 登录还在进行中，请等它结束后再启动新的账号。")
            return active_job
        active_bulk_job = _find_active_bulk_login_job()
        if active_bulk_job:
            raise ValueError("批量 GPT 登录还在进行中，请等它结束后再启动单个账号登录。")

        job_id = _create_login_job(email_id, row["email"])

        def save_session(session_text: str) -> None:
            self.db.save_chatgpt_session(email_id, session_text)
            _mark_login_job_session_saved(job_id)

        config = self._chatgpt_login_config(email_id, row, save_session)

        def notify(status: str, message: str) -> None:
            _update_login_job(job_id, status, message)
            _update_chatgpt_email_status(self.db, email_id, status, message)

        def worker() -> None:
            try:
                run_chatgpt_login(config, notify)
                current = _get_login_job(job_id)
                if current and current["status"] not in LOGIN_TERMINAL_STATUSES:
                    message = "登录流程结束，但没有确认成功，请重新尝试。"
                    _update_login_job(job_id, "retry", message)
                    _update_chatgpt_email_status(self.db, email_id, "retry", message)
            except Exception as exc:
                message = _friendly_network_error(exc)
                status = "banned" if is_chatgpt_account_banned_message(message) else "retry"
                _update_login_job(job_id, status, message)
                _update_chatgpt_email_status(self.db, email_id, status, message)

        threading.Thread(
            target=worker,
            name=f"chatgpt-login-{job_id}",
            daemon=True,
        ).start()
        self.db.log_action("chatgpt_login_start", email_id, f"job_id={job_id}")
        return _get_login_job(job_id) or {}

    def _start_bulk_chatgpt_login(self, raw_ids: list, raw_concurrency=DEFAULT_BULK_LOGIN_CONCURRENCY) -> dict:
        ids = []
        for item in raw_ids:
            email_id = _optional_int(item)
            if email_id is not None and email_id not in ids:
                ids.append(email_id)
        if not ids:
            raise ValueError("请先选择要登录的账号。")

        active_bulk_job = _find_active_bulk_login_job()
        if active_bulk_job:
            return active_bulk_job
        active_job = _find_active_login_job()
        if active_job:
            raise ValueError(f"{active_job.get('email')} 的 GPT 登录还在进行中，请等它结束后再批量登录。")

        rows = [self.db.get_email(email_id) for email_id in ids]
        concurrency = _bounded_int(
            raw_concurrency,
            DEFAULT_BULK_LOGIN_CONCURRENCY,
            1,
            MAX_BULK_LOGIN_CONCURRENCY,
        )
        concurrency = min(concurrency, len(rows))
        job_id = _create_bulk_login_job(
            [(row["id"], row["email"]) for row in rows],
            concurrency,
        )

        def login_one(index: int, row) -> None:
            email_id = int(row["id"])
            email_address = row["email"]
            _update_bulk_login_item(
                job_id,
                email_id,
                "running",
                f"正在登录第 {index}/{len(rows)} 个账号：{email_address}",
                current_email=email_address,
            )

            def save_session(session_text: str, current_email_id=email_id) -> None:
                self.db.save_chatgpt_session(current_email_id, session_text)
                _mark_bulk_login_item_session_saved(job_id, current_email_id)

            def notify(status: str, message: str, current_email_id=email_id) -> None:
                _update_bulk_login_item(job_id, current_email_id, status, message)
                _update_chatgpt_email_status(self.db, current_email_id, status, message)

            try:
                config = self._chatgpt_login_config(
                    email_id,
                    row,
                    save_session,
                    copy_session_to_clipboard=False,
                )
                run_chatgpt_login(config, notify)
                item = _get_bulk_login_item(job_id, email_id)
                if item and item.get("status") not in LOGIN_TERMINAL_STATUSES:
                    message = "登录流程结束，但没有确认成功，请重新尝试。"
                    _update_bulk_login_item(job_id, email_id, "retry", message)
                    _update_chatgpt_email_status(self.db, email_id, "retry", message)
            except Exception as exc:
                message = _friendly_network_error(exc)
                status = "banned" if is_chatgpt_account_banned_message(message) else "retry"
                _update_bulk_login_item(job_id, email_id, status, message)
                _update_chatgpt_email_status(self.db, email_id, status, message)

        def worker() -> None:
            _update_bulk_login_job(
                job_id,
                "running",
                f"批量 GPT 登录已开始，共 {len(rows)} 个账号，同时运行 {concurrency} 个。",
            )
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=concurrency,
                thread_name_prefix=f"chatgpt-bulk-{job_id[:8]}",
            ) as executor:
                futures = [
                    executor.submit(login_one, index, row)
                    for index, row in enumerate(rows, start=1)
                ]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:
                        _update_bulk_login_job(
                            job_id,
                            "running",
                            f"一个并行登录线程异常：{_friendly_network_error(exc)}",
                        )

            finished = _get_bulk_login_job(job_id) or {}
            failed_count = int(finished.get("failed_count") or 0)
            if failed_count:
                _update_bulk_login_job(
                    job_id,
                    "failed",
                    f"批量 GPT 登录完成，{finished.get('saved_count', 0)} 个已保存 session，{failed_count} 个失败。",
                )
            else:
                _update_bulk_login_job(
                    job_id,
                    "ok",
                    f"批量 GPT 登录完成，{finished.get('saved_count', 0)} 个账号已保存 session。",
                )

        threading.Thread(
            target=worker,
            name=f"chatgpt-bulk-login-{job_id}",
            daemon=True,
        ).start()
        self.db.log_action("chatgpt_bulk_login_start", None, json.dumps({"ids": ids}))
        return _get_bulk_login_job(job_id) or {}

    def _export(self, query: dict) -> None:
        mode = _first(query, "mode") or "masked"
        ids = [_optional_int(item) for item in (_first(query, "ids") or "").split(",") if item]
        ids = [item for item in ids if item is not None]
        emails = self.db.list_emails({"ids": ids} if ids else {})
        output = io.StringIO(newline="")
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "email",
                "password",
                "provider",
                "imap_host",
                "imap_port",
                "group_name",
                "tags",
                "remark",
                "status",
                "last_error",
                "last_check_at",
                "latest_code",
                "latest_code_received_at",
            ],
        )
        writer.writeheader()
        for item in emails:
            password = "******"
            if mode == "full":
                password = self.db.get_decrypted_password(item["id"], "export_full_password_access")
            writer.writerow(
                {
                    "email": item["email"],
                    "password": password,
                    "provider": item["provider"],
                    "imap_host": item["imap_host"],
                    "imap_port": item["imap_port"],
                    "group_name": item["group_name"],
                    "tags": item["tags"],
                    "remark": item["remark"],
                    "status": item["status"],
                    "last_error": item["last_error"],
                    "last_check_at": item["last_check_at"],
                    "latest_code": item["latest_code"],
                    "latest_code_received_at": item["latest_code_received_at"],
                }
            )
        self.db.log_action("export_full" if mode == "full" else "export_masked", None, f"count={len(emails)}")
        data = output.getvalue().encode("utf-8-sig")
        filename = f"emails_export_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _chatgpt_sessions_payload(self, ids: list[int] | None = None) -> dict:
        sessions = []
        for item in self.db.list_decrypted_chatgpt_sessions(ids):
            session_text = item.pop("session_text")
            session_json = None
            try:
                session_json = json.loads(session_text)
            except json.JSONDecodeError:
                pass
            sessions.append(
                {
                    **item,
                    "session_url": CHATGPT_SESSION_URL,
                    "session_text": session_text,
                    "session_json": session_json,
                }
            )
        if not sessions:
            if ids:
                raise ValueError("选中的账号还没有可导出的 ChatGPT session，请先登录成功后再复制或导出。")
            raise ValueError("还没有任何已保存的 ChatGPT session，请先登录成功至少一个账号。")
        return {
            "format": "chatgpt_api_auth_session_export_v1",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "session_url": CHATGPT_SESSION_URL,
            "count": len(sessions),
            "sessions": sessions,
        }

    def _chatgpt_sessions_import_payload(self, ids: list[int] | None = None) -> list[dict]:
        payload = self._chatgpt_sessions_payload(ids)
        sessions = []
        for item in payload["sessions"]:
            session_json = item.get("session_json")
            if not isinstance(session_json, dict):
                raise ValueError(f"{item.get('email', '某个账号')} 的 session 不是有效 JSON，请重新登录后再导出。")
            sessions.append(session_json)
        return sessions

    def _export_chatgpt_sessions(self, query: dict) -> None:
        full_backup = (_first(query, "format") or "").lower() == "full"
        ids = _ids_from_values(_first(query, "ids") or "")
        payload = self._chatgpt_sessions_payload(ids)
        export_payload = payload if full_backup else self._chatgpt_sessions_import_payload(ids)
        self.db.log_action(
            "export_all_chatgpt_sessions",
            None,
            json.dumps(
                {
                    "count": payload["count"],
                    "requested_count": len(ids),
                    "format": "full_backup" if full_backup else "import_array",
                }
            ),
        )
        data = json.dumps(export_payload, ensure_ascii=False, indent=2).encode("utf-8")
        prefix = "chatgpt_sessions_full" if full_backup else "chatgpt_sessions_import"
        filename = f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _public_settings(self) -> dict:
        settings = self.db.get_settings()
        return {
            "clipboard_clear_seconds": settings.get("clipboard_clear_seconds", "30"),
            "chatgpt_proxy": settings.get("chatgpt_proxy", ""),
        }

    def _static(self, path: str) -> None:
        if path == "/":
            file_path = STATIC_DIR / "index.html"
        else:
            file_path = (STATIC_DIR / path.lstrip("/")).resolve()
            if not str(file_path).startswith(str(STATIC_DIR.resolve())):
                self._json({"error": "非法路径。"}, status=400)
                return
        if not file_path.exists() or not file_path.is_file():
            self._json({"error": "文件不存在。"}, status=404)
            return
        content = file_path.read_bytes()
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _require_unlock(self) -> bool:
        return True

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def _json(self, data: dict, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, exc: Exception) -> None:
        status = 400
        if isinstance(exc, KeyError):
            status = 404
        self._json({"error": _friendly_network_error(exc)}, status=status)

    def log_message(self, format: str, *args) -> None:
        return


def _first(query: dict, key: str) -> str:
    value = query.get(key, [""])
    return value[0] if value else ""


def _optional_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ids_from_values(values) -> list[int]:
    if isinstance(values, str):
        values = [item for item in values.split(",") if item]
    ids = []
    for value in values or []:
        parsed = _optional_int(value)
        if parsed is not None:
            ids.append(parsed)
    return ids


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _imap_settings(row) -> tuple[str, int]:
    host = row["imap_host"] or ""
    port = int(row["imap_port"] or 993)
    if not host and row["provider"]:
        host, port = provider_defaults(row["provider"])
    return host, port


def _create_login_job(email_id: int, email_address: str) -> str:
    job_id = uuid.uuid4().hex
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with LOGIN_JOBS_LOCK:
        _prune_login_jobs()
        LOGIN_JOBS[job_id] = {
            "id": job_id,
            "email_id": email_id,
            "email": email_address,
            "status": "pending",
            "message": "登录任务已创建。",
            "clipboard_copied": False,
            "session_saved": False,
            "created_at": now,
            "updated_at": now,
        }
    return job_id


def _update_login_job(job_id: str, status: str, message: str) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with LOGIN_JOBS_LOCK:
        if job_id not in LOGIN_JOBS:
            return
        LOGIN_JOBS[job_id].update(
            {
                "status": status,
                "message": message,
                "clipboard_copied": status == "ok",
                "updated_at": now,
            }
        )


def _mark_login_job_session_saved(job_id: str) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with LOGIN_JOBS_LOCK:
        if job_id not in LOGIN_JOBS:
            return
        LOGIN_JOBS[job_id].update(
            {
                "session_saved": True,
                "updated_at": now,
            }
        )


def _get_login_job(job_id: str) -> dict | None:
    with LOGIN_JOBS_LOCK:
        job = LOGIN_JOBS.get(job_id)
        return dict(job) if job else None


def _find_active_login_job() -> dict | None:
    with LOGIN_JOBS_LOCK:
        for job in LOGIN_JOBS.values():
            if job.get("status") in LOGIN_ACTIVE_STATUSES:
                return dict(job)
    return None


def _create_bulk_login_job(accounts: list[tuple[int, str]], concurrency: int) -> str:
    job_id = uuid.uuid4().hex
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with BULK_LOGIN_JOBS_LOCK:
        _prune_bulk_login_jobs()
        BULK_LOGIN_JOBS[job_id] = {
            "id": job_id,
            "status": "pending",
            "message": "批量登录任务已创建。",
            "total": len(accounts),
            "concurrency": concurrency,
            "completed": 0,
            "running_count": 0,
            "saved_count": 0,
            "failed_count": 0,
            "current_index": 0,
            "current_email": "",
            "items": [
                {
                    "email_id": email_id,
                    "email": email,
                    "status": "queued",
                    "message": "等待登录。",
                    "session_saved": False,
                }
                for email_id, email in accounts
            ],
            "created_at": now,
            "updated_at": now,
        }
    return job_id


def _update_bulk_login_job(job_id: str, status: str, message: str) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with BULK_LOGIN_JOBS_LOCK:
        if job_id not in BULK_LOGIN_JOBS:
            return
        job = BULK_LOGIN_JOBS[job_id]
        job.update(
            {
                "status": status,
                "message": message,
                "updated_at": now,
            }
        )
        _refresh_bulk_login_counts(job)


def _update_bulk_login_item(
    job_id: str,
    email_id: int,
    status: str,
    message: str,
    current_index: int | None = None,
    current_email: str | None = None,
) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with BULK_LOGIN_JOBS_LOCK:
        job = BULK_LOGIN_JOBS.get(job_id)
        if not job:
            return
        for item in job["items"]:
            if int(item["email_id"]) == int(email_id):
                item.update(
                    {
                        "status": status,
                        "message": message,
                        "updated_at": now,
                    }
                )
                break
        if current_index is not None:
            job["current_index"] = current_index
        if current_email is not None:
            job["current_email"] = current_email
        job["message"] = message
        job["updated_at"] = now
        _refresh_bulk_login_counts(job)


def _mark_bulk_login_item_session_saved(job_id: str, email_id: int) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with BULK_LOGIN_JOBS_LOCK:
        job = BULK_LOGIN_JOBS.get(job_id)
        if not job:
            return
        for item in job["items"]:
            if int(item["email_id"]) == int(email_id):
                item["session_saved"] = True
                item["updated_at"] = now
                break
        job["updated_at"] = now
        _refresh_bulk_login_counts(job)


def _get_bulk_login_job(job_id: str) -> dict | None:
    with BULK_LOGIN_JOBS_LOCK:
        job = BULK_LOGIN_JOBS.get(job_id)
        return json.loads(json.dumps(job, ensure_ascii=False)) if job else None


def _get_bulk_login_item(job_id: str, email_id: int) -> dict | None:
    job = _get_bulk_login_job(job_id)
    if not job:
        return None
    for item in job["items"]:
        if int(item["email_id"]) == int(email_id):
            return item
    return None


def _find_active_bulk_login_job() -> dict | None:
    with BULK_LOGIN_JOBS_LOCK:
        for job in BULK_LOGIN_JOBS.values():
            if job.get("status") in {"pending", "running"}:
                return json.loads(json.dumps(job, ensure_ascii=False))
    return None


def _refresh_bulk_login_counts(job: dict) -> None:
    completed_statuses = LOGIN_TERMINAL_STATUSES
    items = job.get("items", [])
    job["completed"] = sum(1 for item in items if item.get("status") in completed_statuses)
    job["running_count"] = sum(1 for item in items if item.get("status") == "running")
    job["saved_count"] = sum(1 for item in items if item.get("session_saved"))
    job["failed_count"] = sum(1 for item in items if item.get("status") in {"failed", "closed", "banned", "retry"})


def _prune_bulk_login_jobs() -> None:
    if len(BULK_LOGIN_JOBS) <= 10:
        return
    removable = [
        job_id
        for job_id, job in BULK_LOGIN_JOBS.items()
        if job.get("status") not in {"pending", "running"}
    ]
    for job_id in removable[:-10]:
        BULK_LOGIN_JOBS.pop(job_id, None)


def _prune_login_jobs() -> None:
    if len(LOGIN_JOBS) <= 20:
        return
    removable = [
        job_id
        for job_id, job in LOGIN_JOBS.items()
        if job.get("status") not in LOGIN_ACTIVE_STATUSES
    ]
    for job_id in removable[:-20]:
        LOGIN_JOBS.pop(job_id, None)


def _friendly_network_error(exc: Exception) -> str:
    message = str(exc)
    if "10013" in message:
        return (
            "当前 Python 服务没有外网 socket 权限，无法连接 Outlook IMAP/OAuth。"
            "请用已授权的方式启动服务，或检查 Windows 防火墙、杀毒软件、代理规则。"
        )
    if "-2146893813" in message or "指定状态" in message:
        return "旧加密数据无法在当前运行状态下读取。请重新导入原始 TXT，新的数据会按明文保存。"
    if "AUTHENTICATE failed" in message or "LOGIN failed" in message:
        return "邮箱认证失败。请确认密码/授权码或 Outlook OAuth refresh_token 是否有效。"
    return message


def _update_chatgpt_email_status(db: EmailDatabase, email_id: int, login_status: str, message: str) -> None:
    if login_status == "ok":
        db.update_status(email_id, "ok", "", "chatgpt_login")
        return
    if login_status == "banned":
        db.clear_chatgpt_session(email_id, "clear_chatgpt_session_banned")
        db.update_status(email_id, "banned", message, "chatgpt_login")
        return
    if login_status in {"failed", "closed", "retry"}:
        db.update_status(email_id, "retry", message, "chatgpt_login")


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    db = EmailDatabase(DB_PATH)
    AppHandler.db = db
    AppHandler.session = SessionState()
    server = ThreadingHTTPServer((host, port), AppHandler)
    _safe_print(f"Email manager running: http://{host}:{port}")
    _safe_print("Press Ctrl+C to stop.")
    server.serve_forever()


def _safe_print(message: str) -> None:
    try:
        if sys.stdout:
            print(message, flush=True)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        run()
    except Exception:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "server_error.log").write_text(
            traceback.format_exc(),
            encoding="utf-8",
        )
        raise
