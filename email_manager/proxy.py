from __future__ import annotations

import base64
import fnmatch
import ipaddress
import os
import platform
import socket
import ssl
import subprocess
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


DIRECT_PROXY_VALUES = {"", "direct", "none", "off", "false", "0"}
AUTO_PROXY_VALUES = {"auto", "system"}


@dataclass(frozen=True)
class ProxyConfig:
    server: str = ""
    bypass: str = ""
    source: str = ""

    def server_for(self, host: str, port: int | None = None) -> str:
        if not self.server or should_bypass_proxy(host, self.bypass, port):
            return ""
        return self.server

    def browser_proxy(self) -> dict | None:
        if not self.server:
            return None
        proxy = {"server": self.server}
        if self.bypass:
            proxy["bypass"] = self.bypass
        return proxy


@dataclass(frozen=True)
class ProxyEndpoint:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""


def detect_proxy_config(
    configured_proxy: str = "",
    configured_bypass: str = "",
) -> ProxyConfig:
    bypass = _normalize_bypass(configured_bypass or _env_bypass())
    configured = (
        configured_proxy
        or os.environ.get("EMAIL_MANAGER_PROXY")
        or os.environ.get("EMAIL_MANAGER_CHATGPT_PROXY")
        or ""
    ).strip()
    configured_lower = configured.lower()
    if configured_lower in DIRECT_PROXY_VALUES:
        if configured:
            return ProxyConfig("", bypass, "configured")
    elif configured_lower not in AUTO_PROXY_VALUES:
        return ProxyConfig(_normalize_proxy_server(configured), bypass, "configured")

    for env_name in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        server = _normalize_proxy_server(os.environ.get(env_name, ""))
        if server:
            return ProxyConfig(server, bypass, env_name)

    system_server, system_bypass = _detect_system_proxy()
    return ProxyConfig(
        _normalize_proxy_server(system_server),
        _normalize_bypass(configured_bypass or system_bypass or bypass),
        "system" if system_server else "",
    )


def create_proxy_connection(
    target_host: str,
    target_port: int,
    timeout: int | float | None,
    proxy_server: str,
) -> socket.socket:
    endpoint = parse_proxy_endpoint(proxy_server)
    if endpoint.scheme in {"http", "https"}:
        return _create_http_connect_socket(target_host, target_port, timeout, endpoint)
    if endpoint.scheme in {"socks", "socks5", "socks5h"}:
        return _create_socks5_socket(target_host, target_port, timeout, endpoint)
    raise RuntimeError(f"不支持的代理协议：{endpoint.scheme}。请使用 http、https 或 socks5。")


def create_url_opener(proxy_server: str):
    import urllib.request

    if not proxy_server:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    endpoint = parse_proxy_endpoint(proxy_server)
    if endpoint.scheme in {"socks", "socks5", "socks5h"}:
        raise RuntimeError("OAuth 刷新访问令牌暂不支持 SOCKS 代理，请改用 HTTP 代理地址。")
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_server, "https": proxy_server})
    )


def parse_proxy_endpoint(proxy_server: str) -> ProxyEndpoint:
    normalized = _normalize_proxy_server(proxy_server)
    parts = urlsplit(normalized)
    scheme = (parts.scheme or "http").lower()
    host = parts.hostname or ""
    if not host:
        raise RuntimeError(f"代理地址不完整：{proxy_server}")
    port = parts.port or _default_proxy_port(scheme)
    return ProxyEndpoint(
        scheme=scheme,
        host=host,
        port=port,
        username=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
    )


def should_bypass_proxy(host: str, bypass: str, port: int | None = None) -> bool:
    host = _clean_host(host)
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        if ipaddress.ip_address(host).is_loopback:
            return True
    except ValueError:
        pass

    for raw_pattern in _split_bypass_items(bypass):
        pattern = raw_pattern.lower().strip()
        if not pattern:
            continue
        if pattern in {"*", "<local>"}:
            if pattern == "*" or "." not in host:
                return True
            continue
        pattern_host, pattern_port = _split_pattern_port(pattern)
        if pattern_port and port and str(port) != pattern_port:
            continue
        if _host_matches_bypass(host, pattern_host):
            return True
    return False


def _normalize_proxy_server(value: str) -> str:
    value = (value or "").strip()
    if not value or value.lower() in DIRECT_PROXY_VALUES:
        return ""
    if "=" in value or ";" in value:
        return _proxy_from_mapping(value)
    if "://" in value:
        return value
    return f"http://{value}"


def _proxy_from_mapping(value: str) -> str:
    entries: dict[str, str] = {}
    for item in value.split(";"):
        if "=" not in item:
            continue
        key, raw = item.split("=", 1)
        entries[key.strip().lower()] = raw.strip()
    for key, scheme in (("https", "http"), ("http", "http"), ("socks", "socks5")):
        if entries.get(key):
            server = entries[key]
            return server if "://" in server else f"{scheme}://{server}"
    return ""


def _default_proxy_port(scheme: str) -> int:
    if scheme == "https":
        return 443
    if scheme in {"socks", "socks5", "socks5h"}:
        return 1080
    return 80


def _create_http_connect_socket(
    target_host: str,
    target_port: int,
    timeout: int | float | None,
    proxy: ProxyEndpoint,
) -> socket.socket:
    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    if proxy.scheme == "https":
        context = ssl.create_default_context()
        sock = context.wrap_socket(sock, server_hostname=proxy.host)
    try:
        target = _host_port(target_host, target_port)
        headers = [
            f"CONNECT {target} HTTP/1.1",
            f"Host: {target}",
            "Proxy-Connection: Keep-Alive",
            "User-Agent: email-manager/1.0",
        ]
        if proxy.username or proxy.password:
            token = base64.b64encode(
                f"{proxy.username}:{proxy.password}".encode("utf-8")
            ).decode("ascii")
            headers.append(f"Proxy-Authorization: Basic {token}")
        request = "\r\n".join(headers) + "\r\n\r\n"
        sock.sendall(request.encode("ascii"))
        response = _read_http_headers(sock)
        status_line = response.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
        parts = status_line.split()
        if len(parts) < 2 or parts[1] != "200":
            raise RuntimeError(f"代理 CONNECT 到 {target} 失败：{status_line}")
        return sock
    except Exception:
        sock.close()
        raise


def _create_socks5_socket(
    target_host: str,
    target_port: int,
    timeout: int | float | None,
    proxy: ProxyEndpoint,
) -> socket.socket:
    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    try:
        methods = [0x00]
        if proxy.username or proxy.password:
            methods.append(0x02)
        sock.sendall(bytes([0x05, len(methods), *methods]))
        version, method = _recv_exact(sock, 2)
        if version != 0x05 or method == 0xFF:
            raise RuntimeError("SOCKS5 代理没有可用的认证方式。")
        if method == 0x02:
            username = proxy.username.encode("utf-8")
            password = proxy.password.encode("utf-8")
            if len(username) > 255 or len(password) > 255:
                raise RuntimeError("SOCKS5 用户名或密码过长。")
            sock.sendall(
                bytes([0x01, len(username)])
                + username
                + bytes([len(password)])
                + password
            )
            auth_version, auth_status = _recv_exact(sock, 2)
            if auth_version != 0x01 or auth_status != 0x00:
                raise RuntimeError("SOCKS5 代理认证失败。")

        sock.sendall(
            bytes([0x05, 0x01, 0x00])
            + _socks5_address(target_host)
            + int(target_port).to_bytes(2, "big")
        )
        header = _recv_exact(sock, 4)
        if header[0] != 0x05:
            raise RuntimeError("SOCKS5 代理响应无效。")
        if header[1] != 0x00:
            raise RuntimeError(f"SOCKS5 代理连接失败，错误码：{header[1]}")
        _read_socks5_bound_address(sock, header[3])
        _recv_exact(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


def _socks5_address(host: str) -> bytes:
    try:
        ip = ipaddress.ip_address(host)
        if ip.version == 4:
            return bytes([0x01]) + ip.packed
        return bytes([0x04]) + ip.packed
    except ValueError:
        encoded = host.encode("idna")
        if len(encoded) > 255:
            raise RuntimeError("目标主机名过长，SOCKS5 无法连接。")
        return bytes([0x03, len(encoded)]) + encoded


def _read_socks5_bound_address(sock: socket.socket, atyp: int) -> None:
    if atyp == 0x01:
        _recv_exact(sock, 4)
    elif atyp == 0x04:
        _recv_exact(sock, 16)
    elif atyp == 0x03:
        length = _recv_exact(sock, 1)[0]
        _recv_exact(sock, length)
    else:
        raise RuntimeError("SOCKS5 代理返回了未知地址类型。")


def _read_http_headers(sock: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > 65536:
            raise RuntimeError("代理响应头过大。")
    if b"\r\n\r\n" not in data:
        raise RuntimeError("代理没有返回完整 CONNECT 响应。")
    return bytes(data)


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise RuntimeError("代理连接提前关闭。")
        data.extend(chunk)
    return bytes(data)


def _host_port(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _clean_host(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("[") and "]" in host:
        host = host[1 : host.index("]")]
    return host.rstrip(".")


def _split_pattern_port(pattern: str) -> tuple[str, str]:
    if pattern.startswith("[") and "]" in pattern:
        host = pattern[1 : pattern.index("]")]
        rest = pattern[pattern.index("]") + 1 :]
        return host, rest[1:] if rest.startswith(":") else ""
    if pattern.count(":") == 1:
        host, port = pattern.rsplit(":", 1)
        if port.isdigit():
            return host, port
    return pattern, ""


def _host_matches_bypass(host: str, pattern: str) -> bool:
    pattern = pattern.strip().lstrip()
    if not pattern:
        return False
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix)
    if pattern.startswith("."):
        suffix = pattern[1:]
        return host == suffix or host.endswith(pattern)
    return fnmatch.fnmatch(host, pattern) or host == pattern


def _split_bypass_items(bypass: str) -> list[str]:
    normalized = _normalize_bypass(bypass)
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _normalize_bypass(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return ",".join(item for item in value.replace(";", ",").split(",") if item.strip())


def _env_bypass() -> str:
    return os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""


def _detect_system_proxy() -> tuple[str, str]:
    system = platform.system()
    if system == "Windows":
        return _detect_windows_proxy()
    if system == "Darwin":
        return _detect_macos_proxy()
    return "", ""


def _detect_windows_proxy() -> tuple[str, str]:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return "", ""
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            try:
                bypass, _ = winreg.QueryValueEx(key, "ProxyOverride")
            except OSError:
                bypass = ""
            bypass = str(bypass or "").replace("<local>", "localhost,127.0.0.1,::1")
            return str(server or ""), bypass
    except Exception:
        return "", ""


def _detect_macos_proxy() -> tuple[str, str]:
    if platform.system() != "Darwin":
        return "", ""
    try:
        result = subprocess.run(
            ["scutil", "--proxy"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return "", ""
    if result.returncode != 0:
        return "", ""
    values: dict[str, str] = {}
    exceptions: list[str] = []
    in_exceptions = False
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("ExceptionsList"):
            in_exceptions = True
            continue
        if in_exceptions and line == "}":
            in_exceptions = False
            continue
        if in_exceptions and ":" in line:
            _, value = line.split(":", 1)
            exceptions.append(value.strip())
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    for prefix, scheme in (("HTTPS", "http"), ("HTTP", "http"), ("SOCKS", "socks5")):
        if (
            values.get(f"{prefix}Enable") == "1"
            and values.get(f"{prefix}Proxy")
            and values.get(f"{prefix}Port")
        ):
            return (
                f"{scheme}://{values[f'{prefix}Proxy']}:{values[f'{prefix}Port']}",
                ",".join(exceptions),
            )
    return "", ",".join(exceptions)
