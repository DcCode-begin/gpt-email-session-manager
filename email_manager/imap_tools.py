from __future__ import annotations

import json
import imaplib
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

from .code_parser import extract_verification_code, html_to_text
from .proxy import create_proxy_connection, create_url_opener, detect_proxy_config


PROVIDER_DEFAULTS = {
    "QQ": ("imap.qq.com", 993),
    "163": ("imap.163.com", 993),
    "126": ("imap.126.com", 993),
    "Gmail": ("imap.gmail.com", 993),
    "Outlook": ("outlook.office365.com", 993),
    "Yahoo": ("imap.mail.yahoo.com", 993),
    "iCloud": ("imap.mail.me.com", 993),
}

OAUTH_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}


@dataclass
class MailCode:
    code: str
    subject: str
    sender: str
    received_at: str


def provider_defaults(provider: str) -> tuple[str, int]:
    return PROVIDER_DEFAULTS.get((provider or "").strip(), ("", 993))


def test_imap_connection(
    email_address: str,
    password: str,
    imap_host: str,
    imap_port: int,
    oauth_client_id: str = "",
    oauth_refresh_token: str = "",
    timeout: int = 12,
    proxy_server: str = "",
    proxy_bypass: str = "",
) -> None:
    client = _open_imap_ssl(imap_host, int(imap_port), timeout, proxy_server, proxy_bypass)
    try:
        _login(
            client,
            email_address,
            password,
            oauth_client_id,
            oauth_refresh_token,
            proxy_server,
            proxy_bypass,
        )
    finally:
        try:
            client.logout()
        except Exception:
            pass


def fetch_latest_code(
    email_address: str,
    password: str,
    imap_host: str,
    imap_port: int,
    oauth_client_id: str = "",
    oauth_refresh_token: str = "",
    limit: int = 20,
    timeout: int = 15,
    received_after: datetime | None = None,
    sender_subject_keywords: tuple[str, ...] | None = None,
    proxy_server: str = "",
    proxy_bypass: str = "",
) -> MailCode | None:
    client = _open_imap_ssl(imap_host, int(imap_port), timeout, proxy_server, proxy_bypass)
    try:
        _login(
            client,
            email_address,
            password,
            oauth_client_id,
            oauth_refresh_token,
            proxy_server,
            proxy_bypass,
        )
        client.select("INBOX", readonly=True)
        status, data = client.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return None

        message_ids = data[0].split()[-limit:]
        parser = BytesParser(policy=policy.default)
        for message_id in reversed(message_ids):
            status, header_payload = client.fetch(
                message_id,
                "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])",
            )
            if status != "OK":
                continue
            header_raw = _first_message_bytes(header_payload)
            if not header_raw:
                continue
            header_message = parser.parsebytes(header_raw)
            subject = str(header_message.get("subject", ""))
            sender = str(header_message.get("from", ""))
            received_at = _message_datetime(header_message)
            if received_after and received_at and received_at < received_after:
                continue
            if sender_subject_keywords and not _matches_keywords(
                f"{subject}\n{sender}",
                sender_subject_keywords,
            ):
                continue

            code = extract_verification_code(subject)
            if code:
                return MailCode(
                    code=code,
                    subject=subject,
                    sender=sender,
                    received_at=_format_message_date(received_at),
                )

            status, payload = client.fetch(message_id, "(RFC822)")
            if status != "OK":
                continue
            raw = _first_message_bytes(payload)
            if not raw:
                continue
            message = parser.parsebytes(raw)
            body = _message_text(message)
            code = extract_verification_code(f"{subject}\n{body}")
            if code:
                return MailCode(
                    code=code,
                    subject=subject,
                    sender=sender,
                    received_at=_format_message_date(received_at),
                )
        return None
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _first_message_bytes(payload: list) -> bytes | None:
    for item in payload:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def _message_text(message) -> str:
    if message.is_multipart():
        html_parts: list[str] = []
        text_parts: list[str] = []
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = part.get_content_disposition()
            if disposition == "attachment":
                continue
            try:
                content = part.get_content()
            except Exception:
                continue
            if content_type == "text/plain":
                text_parts.append(str(content))
            elif content_type == "text/html":
                html_parts.append(html_to_text(str(content)))
        return "\n".join(text_parts or html_parts)

    try:
        content = message.get_content()
    except Exception:
        return ""
    if message.get_content_type() == "text/html":
        return html_to_text(str(content))
    return str(content)


def _message_datetime(message) -> datetime:
    raw_date = message.get("date")
    if raw_date:
        try:
            return parsedate_to_datetime(raw_date).astimezone()
        except Exception:
            pass
    return datetime.now().astimezone()


def _format_message_date(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="seconds")


def _matches_keywords(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _login(
    client: imaplib.IMAP4_SSL,
    email_address: str,
    password: str,
    oauth_client_id: str = "",
    oauth_refresh_token: str = "",
    proxy_server: str = "",
    proxy_bypass: str = "",
) -> None:
    if oauth_client_id and oauth_refresh_token:
        access_token = _microsoft_access_token(
            oauth_client_id,
            oauth_refresh_token,
            proxy_server,
            proxy_bypass,
        )
        auth_string = f"user={email_address}\x01auth=Bearer {access_token}\x01\x01"
        client.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))
        return
    client.login(email_address, password)


def _microsoft_access_token(
    client_id: str,
    refresh_token: str,
    proxy_server: str = "",
    proxy_bypass: str = "",
) -> str:
    cache_key = (client_id, refresh_token)
    cached = OAUTH_TOKEN_CACHE.get(cache_key)
    if cached and cached[1] > time.monotonic():
        return cached[0]

    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    proxy = detect_proxy_config(proxy_server, proxy_bypass).server_for(
        "login.microsoftonline.com",
        443,
    )
    opener = create_url_opener(proxy)
    try:
        with opener.open(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OAuth 刷新访问令牌失败：{detail}") from exc
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"OAuth 响应中没有 access_token：{payload}")
    expires_in = int(payload.get("expires_in") or 3600)
    OAUTH_TOKEN_CACHE[cache_key] = (
        token,
        time.monotonic() + max(60, expires_in - 120),
    )
    return token


class _ProxyIMAP4SSL(imaplib.IMAP4_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        proxy_server: str,
        *,
        ssl_context: ssl.SSLContext | None = None,
        timeout: int | float | None = None,
    ) -> None:
        self._email_manager_proxy_server = proxy_server
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout):
        raw_socket = create_proxy_connection(
            self.host,
            self.port,
            timeout,
            self._email_manager_proxy_server,
        )
        return self.ssl_context.wrap_socket(raw_socket, server_hostname=self.host)


def _open_imap_ssl(
    imap_host: str,
    imap_port: int,
    timeout: int | float,
    proxy_server: str = "",
    proxy_bypass: str = "",
) -> imaplib.IMAP4_SSL:
    proxy = detect_proxy_config(proxy_server, proxy_bypass).server_for(
        imap_host,
        imap_port,
    )
    if not proxy:
        return imaplib.IMAP4_SSL(imap_host, int(imap_port), timeout=timeout)
    return _ProxyIMAP4SSL(imap_host, int(imap_port), proxy, timeout=timeout)
