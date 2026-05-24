from __future__ import annotations

import os
import re
import time
import ctypes
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .imap_tools import fetch_latest_code
from .proxy import detect_proxy_config


CHATGPT_LOGIN_URL = "https://chatgpt.com/auth/login"
CHATGPT_HOME_URL = "https://chatgpt.com/"
CHATGPT_SESSION_URL = "https://chatgpt.com/api/auth/session"

OPENAI_MAIL_KEYWORDS = ("openai", "chatgpt", "chat gpt")
CHATGPT_ACCOUNT_BANNED_KEYWORDS = (
    "account_deactivated",
    "deactivated",
    "disabled",
    "deleted",
    "identity verification error",
    "authentication error",
    "身份验证错误",
    "账户已被删除或停用",
    "账号已被删除或停用",
    "账户已停用",
    "账号已停用",
)


SessionCallback = Callable[[str], None]


@dataclass(frozen=True)
class ChatGPTLoginConfig:
    email_address: str
    password: str
    imap_host: str
    imap_port: int
    user_data_dir: Path
    oauth_client_id: str = ""
    oauth_refresh_token: str = ""
    login_timeout_seconds: int = 240
    code_poll_seconds: int = 180
    headless: bool = True
    keep_browser_open: bool = False
    session_callback: SessionCallback | None = None
    cleanup_user_data_dir: bool = True
    copy_session_to_clipboard: bool = True
    proxy_server: str = ""
    proxy_bypass: str = ""


StatusCallback = Callable[[str, str], None]


def run_chatgpt_login(config: ChatGPTLoginConfig, notify: StatusCallback) -> None:
    """Open a local browser, sign in to ChatGPT with an email code, then keep it open."""

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - depends on local install
        raise RuntimeError("本机缺少 Playwright，无法启动自动登录浏览器。") from exc

    config.user_data_dir.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    context = None
    try:
        launch_kwargs = _browser_launch_kwargs(playwright, config)
        notify("running", "正在打开 ChatGPT 登录页...")
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(config.user_data_dir),
            headless=config.headless,
            viewport={"width": 1280, "height": 900} if config.headless else None,
            args=_browser_args(config),
            **launch_kwargs,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(10000)
        page.goto(CHATGPT_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        _dismiss_cookie_prompt(page)

        if _looks_logged_in(page):
            clipboard_copied = _copy_chatgpt_session_to_clipboard(page, config)
            notify("ok", _session_done_message(config, "ChatGPT 已处于登录状态", clipboard_copied))
            _maybe_keep_browser_open(context, config)
            return

        code_requested_at = datetime.now().astimezone() - timedelta(seconds=20)
        notify("running", "正在填写邮箱并请求验证码...")
        _fill_email_and_continue(page, config.email_address)
        _ensure_email_code_requested(page)

        notify("running", "验证码已请求，正在从邮箱读取最新验证码...")
        code = _wait_for_email_code(config, code_requested_at, notify)
        notify("running", "已读到验证码，正在提交到 ChatGPT...")
        _fill_code_and_continue(page, code)

        _wait_until_logged_in(page, config.login_timeout_seconds)
        clipboard_copied = _copy_chatgpt_session_to_clipboard(page, config)
        notify("ok", _session_done_message(config, "ChatGPT 登录完成", clipboard_copied))
        _maybe_keep_browser_open(context, config)
    except PlaywrightTimeoutError as exc:
        message = "等待 ChatGPT 页面或验证码输入框超时。"
        if context is not None:
            _handle_login_failure(context, config, notify, message)
        else:
            raise RuntimeError(message) from exc
    except PlaywrightError as exc:
        message = _friendly_playwright_error(str(exc))
        if context is not None:
            _handle_login_failure(context, config, notify, message)
        else:
            raise RuntimeError(message) from exc
    except RuntimeError as exc:
        if context is not None:
            _handle_login_failure(context, config, notify, str(exc))
        else:
            raise
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        playwright.stop()
        if config.headless and not config.keep_browser_open and config.cleanup_user_data_dir:
            shutil.rmtree(config.user_data_dir, ignore_errors=True)


def _browser_launch_kwargs(playwright, config: ChatGPTLoginConfig) -> dict:
    executable = _find_browser_executable(playwright)
    kwargs = {"executable_path": str(executable)} if executable else {}
    proxy = _browser_proxy_config(config)
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def _browser_args(config: ChatGPTLoginConfig) -> list[str]:
    args = ["--disable-blink-features=AutomationControlled"]
    if not config.headless:
        args.append("--start-maximized")
    return args


def _browser_proxy_config(config: ChatGPTLoginConfig) -> dict | None:
    return detect_proxy_config(config.proxy_server, config.proxy_bypass).browser_proxy()


def _find_browser_executable(playwright) -> Path | None:
    candidates: list[Path] = []
    for env_name in ("EMAIL_MANAGER_BROWSER", "CHROME_PATH", "EDGE_PATH"):
        if os.environ.get(env_name):
            candidates.append(Path(os.environ[env_name]))

    program_files = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
    for root in [Path(item) for item in program_files if item]:
        candidates.extend(
            [
                root / "Google" / "Chrome" / "Application" / "chrome.exe",
                root / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )

    try:
        candidates.append(Path(playwright.chromium.executable_path))
    except Exception:
        pass

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _fill_email_and_continue(page, email_address: str) -> None:
    page.wait_for_load_state("domcontentloaded")
    email_input = _first_visible(
        page,
        [
            "input[type='email']",
            "input[autocomplete='email']",
            "input[name='email']",
            "input[name='username']",
            "input[id*='email' i]",
        ],
    )
    if not email_input:
        raise RuntimeError("没有找到 ChatGPT 登录页的邮箱输入框。")
    _type_text(email_input, email_address)
    _click_continue(page)
    _settle_after_action(page)
    _advance_email_login_page(page, email_address)


def _type_text(locator, text: str) -> None:
    locator.click()
    locator.fill(text)
    value = locator.evaluate("(element) => element.value")
    if value != text:
        locator.fill("")
        locator.type(text, delay=5)
        value = locator.evaluate("(element) => element.value")
    if value != text:
        locator.fill(text)


def _settle_after_action(page, timeout: int = 30000) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    page.wait_for_timeout(600)


def _advance_email_login_page(page, email_address: str) -> None:
    clicks = 0
    deadline = time.monotonic() + 18
    while time.monotonic() < deadline:
        _raise_if_login_blocked(page)
        if "email-verification" in page.url:
            return
        try:
            if _has_code_input(page):
                return
        except Exception:
            pass
        if _first_visible(
            page,
            [
                "input[type='password']",
                "input[autocomplete='current-password']",
                "input[name*='password' i]",
                "input[id*='password' i]",
            ],
        ):
            return

        email_input = _first_visible(
            page,
            [
                "input[type='email']",
                "input[autocomplete*='email' i]",
                "input[name='email']",
                "input[name='username']",
                "input[id*='email' i]",
            ],
        )
        if email_input and clicks < 4:
            try:
                value = email_input.evaluate("(element) => element.value || ''")
            except Exception:
                value = ""
            if value != email_address:
                _type_text(email_input, email_address)
            _click_continue(page)
            clicks += 1
            _settle_after_action(page, timeout=10000)
            continue

        if _click_email_code_option(page):
            _settle_after_action(page, timeout=10000)
            continue

        page.wait_for_timeout(1000)


def _dismiss_cookie_prompt(page) -> None:
    patterns = [
        re.compile(r"reject non-essential", re.I),
        re.compile(r"reject", re.I),
        re.compile(r"decline", re.I),
        re.compile(r"拒绝"),
        re.compile(r"非必要"),
    ]
    for pattern in patterns:
        try:
            button = page.get_by_role("button", name=pattern).first
            if button.count() and button.is_visible() and button.is_enabled():
                button.click(timeout=3000)
                return
        except Exception:
            pass


def _click_continue(page) -> None:
    scoped_submit = _first_visible(
        page,
        [
            "form:has(input[type='email']) button[type='submit']",
            "form:has(input[name='email']) button[type='submit']",
            "form:has(input[autocomplete*='email' i]) button[type='submit']",
            "form:has(input[type='password']) button[type='submit']",
            "form:has(input[autocomplete='one-time-code']) button[type='submit']",
            "form:has(input[inputmode='numeric']) button[type='submit']",
        ],
    )
    if scoped_submit:
        scoped_submit.click()
        return

    button_patterns = [
        re.compile(r"continue", re.I),
        re.compile(r"next", re.I),
        re.compile(r"log in", re.I),
        re.compile(r"sign in", re.I),
        re.compile(r"继续"),
        re.compile(r"下一步"),
        re.compile(r"发送"),
        re.compile(r"登录"),
    ]
    for pattern in button_patterns:
        button = page.get_by_role("button", name=pattern).first
        try:
            if button.count() and button.is_visible() and button.is_enabled():
                button.click()
                return
        except Exception:
            pass

    raise RuntimeError("没有找到 ChatGPT 登录页的继续按钮。")


def _ensure_email_code_requested(page) -> None:
    page.wait_for_timeout(800)
    _raise_if_login_blocked(page)
    if "email-verification" in page.url:
        _wait_for_code_input(page, timeout_seconds=30)
        return
    try:
        if _has_code_input(page):
            return
    except Exception:
        _settle_after_action(page, timeout=15000)
        _raise_if_login_blocked(page)
        if "email-verification" in page.url:
            _wait_for_code_input(page, timeout_seconds=30)
            return
        if _has_code_input(page):
            return
    if "email-verification" in page.url:
        _wait_for_code_input(page, timeout_seconds=30)
        return
    if _click_email_code_option(page):
        _settle_after_action(page)
        page.wait_for_timeout(700)
        _raise_if_login_blocked(page)
        if "email-verification" in page.url or _has_code_input(page):
            _wait_for_code_input(page, timeout_seconds=30)
            return

    password_input = _first_visible(
        page,
        [
            "input[type='password']",
            "input[autocomplete='current-password']",
            "input[name*='password' i]",
            "input[id*='password' i]",
        ],
    )
    if password_input:
        raise RuntimeError("ChatGPT 当前要求输入账号密码，没有出现邮箱验证码登录入口。")
    raise RuntimeError(f"ChatGPT 没有进入邮箱验证码页面。{_current_page_summary(page)}")


def _click_email_code_option(page) -> bool:
    patterns = [
        re.compile(r"email code", re.I),
        re.compile(r"verification code", re.I),
        re.compile(r"one-time code", re.I),
        re.compile(r"send code", re.I),
        re.compile(r"use.*code", re.I),
        re.compile(r"验证码"),
        re.compile(r"校验码"),
        re.compile(r"一次性"),
        re.compile(r"发送.*码"),
        re.compile(r"邮箱.*码"),
    ]
    for role in ("button", "link"):
        for pattern in patterns:
            try:
                item = page.get_by_role(role, name=pattern).first
                if item.count() and item.is_visible() and item.is_enabled():
                    item.click(timeout=5000)
                    return True
            except Exception:
                pass
    return False


def _has_code_input(page) -> bool:
    selectors = [
        "input[autocomplete='one-time-code']",
        "input[inputmode='numeric']",
        "input[name*='code' i]",
        "input[id*='code' i]",
    ]
    if _first_visible(page, selectors):
        return True
    return len(_one_char_code_inputs(page)) >= 4


def _wait_for_email_code(
    config: ChatGPTLoginConfig,
    received_after: datetime,
    notify: StatusCallback,
) -> str:
    started_at = time.monotonic()
    deadline = time.monotonic() + config.code_poll_seconds
    last_notice = 0.0
    while time.monotonic() < deadline:
        code = fetch_latest_code(
            config.email_address,
            config.password,
            config.imap_host,
            config.imap_port,
            config.oauth_client_id,
            config.oauth_refresh_token,
            limit=12,
            timeout=6,
            received_after=received_after,
            sender_subject_keywords=OPENAI_MAIL_KEYWORDS,
            proxy_server=config.proxy_server,
            proxy_bypass=config.proxy_bypass,
        )
        waited = time.monotonic() - started_at
        if not code and waited > 12:
            code = fetch_latest_code(
                config.email_address,
                config.password,
                config.imap_host,
                config.imap_port,
                config.oauth_client_id,
                config.oauth_refresh_token,
                limit=16,
                timeout=6,
                sender_subject_keywords=OPENAI_MAIL_KEYWORDS,
                proxy_server=config.proxy_server,
                proxy_bypass=config.proxy_bypass,
            )
        if not code and waited > 30:
            code = fetch_latest_code(
                config.email_address,
                config.password,
                config.imap_host,
                config.imap_port,
                config.oauth_client_id,
                config.oauth_refresh_token,
                limit=20,
                timeout=6,
                proxy_server=config.proxy_server,
                proxy_bypass=config.proxy_bypass,
            )
        if code:
            return code.code
        if time.monotonic() - last_notice > 20:
            notify("running", "还在等待 ChatGPT 验证码邮件...")
            last_notice = time.monotonic()
        time.sleep(1.5)
    raise RuntimeError("没有在邮箱里等到新的 ChatGPT 验证码。")


def _fill_code_and_continue(page, code: str) -> None:
    page.wait_for_load_state("domcontentloaded")
    _wait_for_code_input(page)
    one_char_inputs = _one_char_code_inputs(page)
    if len(one_char_inputs) >= len(code):
        for char, input_node in zip(code, one_char_inputs):
            input_node.fill(char)
        try:
            _click_continue(page)
        except RuntimeError:
            pass
        return

    selectors = [
        "input[autocomplete='one-time-code']",
        "input[inputmode='numeric']",
        "input[name*='code' i]",
        "input[id*='code' i]",
        "input[type='text']",
        "input:not([type])",
    ]
    code_input = _first_visible(page, selectors)
    if not code_input:
        raise RuntimeError("没有找到 ChatGPT 验证码输入框。")
    code_input.fill(code)
    try:
        _click_continue(page)
    except RuntimeError:
        pass


def _wait_for_code_input(page, timeout_seconds: int = 60) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            _raise_if_login_blocked(page)
            if _has_code_input(page):
                return
        except Exception:
            blocker = _login_blocker_message(page)
            if blocker:
                raise RuntimeError(blocker)
        page.wait_for_timeout(1000)
    raise RuntimeError(f"没有找到 ChatGPT 验证码输入框。{_current_page_summary(page)}")


def _wait_until_logged_in(page, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        if _looks_logged_in(page):
            return
        blocker = _login_blocker_message(page)
        if blocker:
            raise RuntimeError(blocker)
    raise RuntimeError(f"验证码已提交，但没有确认 ChatGPT 登录完成。{_current_page_summary(page)}")


def _login_blocker_message(page) -> str:
    try:
        text = page.locator("body").inner_text(timeout=1000)
    except Exception:
        return ""
    normalized = re.sub(r"\s+", " ", text).strip()
    if is_chatgpt_account_banned_message(normalized):
        if len(normalized) > 500:
            normalized = f"{normalized[:500]}..."
        return f"ChatGPT 登录被拒绝：{normalized}"
    return ""


def _raise_if_login_blocked(page) -> None:
    blocker = _login_blocker_message(page)
    if blocker:
        raise RuntimeError(blocker)


def is_chatgpt_account_banned_message(message: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(message or "")).strip()
    lowered = normalized.lower()
    return any(keyword in lowered or keyword in normalized for keyword in CHATGPT_ACCOUNT_BANNED_KEYWORDS)


def _current_page_summary(page) -> str:
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        text = ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 1200:
        text = f"{text[:1200]}..."
    return f" 当前页面：{page.url}；页面文字：{text}"


def _looks_logged_in(page) -> bool:
    url = page.url.lower()
    if url.startswith(CHATGPT_HOME_URL) and "/auth/" not in url and "/login" not in url:
        return True
    try:
        if page.locator("textarea, [contenteditable='true']").first.is_visible(timeout=1000):
            return True
    except Exception:
        pass
    return False


def _maybe_keep_browser_open(context, config: ChatGPTLoginConfig) -> None:
    if config.keep_browser_open:
        _keep_browser_open(context)


def _keep_browser_open(context) -> None:
    while True:
        try:
            if not context.pages:
                return
            time.sleep(1)
        except Exception:
            return


def _copy_chatgpt_session_to_clipboard(page, config: ChatGPTLoginConfig | None = None) -> bool:
    session_page = page.context.new_page()
    try:
        session_page.goto(CHATGPT_SESSION_URL, wait_until="domcontentloaded", timeout=60000)
        session_text = session_page.locator("body").inner_text(timeout=15000).strip()
    finally:
        try:
            session_page.close()
        except Exception:
            pass

    if not session_text:
        raise RuntimeError("已登录，但没有读取到 ChatGPT session 内容。")
    if config and config.session_callback:
        config.session_callback(session_text)
    if not config or config.copy_session_to_clipboard:
        try:
            _set_clipboard_text(session_text)
            return True
        except Exception:
            return False
    return False


def _session_done_message(config: ChatGPTLoginConfig, prefix: str, clipboard_copied: bool = False) -> str:
    if config.copy_session_to_clipboard and clipboard_copied:
        return f"{prefix}，session 已复制成功。"
    if config.copy_session_to_clipboard:
        return f"{prefix}，session 已保存到账号。当前系统未写入剪贴板，可在页面中复制。"
    return f"{prefix}，session 已保存到账号。"


def _set_windows_clipboard_text(text: str) -> None:
    _set_clipboard_text(text)


def _set_clipboard_text(text: str) -> None:
    if os.name != "nt":
        _set_unix_clipboard_text(text)
        return

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    data = (text + "\0").encode("utf-16le")
    handle = kernel32.GlobalAlloc(0x0002, len(data))
    if not handle:
        raise ctypes.WinError()

    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        raise ctypes.WinError()

    ctypes.memmove(locked, data, len(data))
    kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise ctypes.WinError()

    clipboard_owns_handle = False
    try:
        if not user32.EmptyClipboard():
            raise ctypes.WinError()
        if not user32.SetClipboardData(13, handle):
            raise ctypes.WinError()
        clipboard_owns_handle = True
    finally:
        user32.CloseClipboard()
        if not clipboard_owns_handle:
            kernel32.GlobalFree(handle)


def _set_unix_clipboard_text(text: str) -> None:
    commands: list[list[str]] = []
    if shutil.which("pbcopy"):
        commands.append(["pbcopy"])
    if shutil.which("wl-copy"):
        commands.append(["wl-copy"])
    if shutil.which("xclip"):
        commands.append(["xclip", "-selection", "clipboard"])
    if shutil.which("xsel"):
        commands.append(["xsel", "--clipboard", "--input"])

    errors = []
    for command in commands:
        try:
            subprocess.run(
                command,
                input=text,
                text=True,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return
        except Exception as exc:
            errors.append(str(exc))
    detail = f"：{'; '.join(errors)}" if errors else ""
    raise RuntimeError(f"当前系统没有可用剪贴板工具，请安装 pbcopy、wl-copy、xclip 或 xsel{detail}")


def _handle_login_failure(
    context,
    config: ChatGPTLoginConfig,
    notify: StatusCallback,
    message: str,
) -> None:
    if is_chatgpt_account_banned_message(message):
        notify("banned", f"{message} 后台登录已停止。")
        return
    if config.keep_browser_open and not config.headless:
        notify("retry", f"{message} 登录已停止，请重新尝试。")
        return
    notify("retry", f"{message} 后台登录已停止，请重新尝试。")


def _first_visible(page, selectors: list[str]):
    for selector in selectors:
        locator = page.locator(selector)
        count = locator.count()
        for index in range(count):
            item = locator.nth(index)
            try:
                if item.is_visible() and item.is_enabled():
                    return item
            except Exception:
                continue
    return None


def _visible_inputs(page) -> list:
    items = []
    locator = page.locator("input")
    for index in range(locator.count()):
        item = locator.nth(index)
        try:
            if item.is_visible() and item.is_enabled():
                items.append(item)
        except Exception:
            pass
    return items


def _one_char_code_inputs(page) -> list:
    inputs = _visible_inputs(page)
    return [
        item
        for item in inputs
        if _input_max_length(item) == 1
        or _input_attr(item, "aria-label").lower().find("digit") >= 0
        or _input_attr(item, "inputmode").lower() == "numeric"
    ]


def _input_attr(input_node, name: str) -> str:
    try:
        return input_node.get_attribute(name) or ""
    except Exception:
        return ""


def _input_max_length(input_node) -> int:
    raw = _input_attr(input_node, "maxlength")
    try:
        return int(raw)
    except ValueError:
        return 0


def _friendly_playwright_error(message: str) -> str:
    if "ERR_NETWORK_ACCESS_DENIED" in message:
        return "浏览器无法访问 chatgpt.com，请用有网络权限的方式启动本工具，或检查代理/防火墙。"
    if "Executable doesn't exist" in message or "download new browsers" in message:
        return "没有找到可用浏览器。请安装 Chrome/Edge，或设置 EMAIL_MANAGER_BROWSER 指向浏览器 exe。"
    if "Target page, context or browser has been closed" in message:
        return "浏览器窗口已关闭，自动登录已停止。"
    return message
