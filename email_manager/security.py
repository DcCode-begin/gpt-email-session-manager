from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import os
from pathlib import Path


DPAPI_ENTROPY = b"email-manager-local-v1"
LOCAL_KEY_FILE = "secret.key"
LOCAL_TOKEN_PREFIX = "local-v1:"


class PlainTextProtector:
    """Stores secrets as plain text while reading older encrypted values when possible."""

    def __init__(self, key_dir: Path | None = None) -> None:
        self.key_dir = key_dir

    def protect(self, text: str) -> str:
        return text or ""

    def unprotect(self, token: str) -> str:
        token = token or ""
        if token.startswith(LOCAL_TOKEN_PREFIX):
            return self._try_unprotect_local(token)
        if os.name == "nt":
            return self._try_unprotect_dpapi(token)
        return token

    def _try_unprotect_local(self, token: str) -> str:
        key = self._read_local_key()
        if not key:
            return token
        try:
            raw = token[len(LOCAL_TOKEN_PREFIX) :].encode("ascii")
            parts = raw.split(b".")
            if len(parts) != 3:
                return token
            nonce, cipher, tag = (self._b64decode(part) for part in parts)
            expected = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, expected):
                return token
            return self._xor_with_keystream(cipher, key, nonce).decode("utf-8")
        except Exception:
            return token

    def _try_unprotect_dpapi(self, token: str) -> str:
        try:
            protected = base64.b64decode(token.encode("ascii"), validate=True)
        except Exception:
            return token
        if not protected:
            return token

        try:
            import ctypes.wintypes as wintypes

            class DataBlob(ctypes.Structure):
                _fields_ = [
                    ("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
                ]

            crypt32 = ctypes.windll.crypt32
            kernel32 = ctypes.windll.kernel32
            data_blob, data_buffer = self._blob_from_bytes(protected, DataBlob)
            entropy_blob, entropy_buffer = self._blob_from_bytes(DPAPI_ENTROPY, DataBlob)
            out_blob = DataBlob()
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(data_blob),
                None,
                ctypes.byref(entropy_blob),
                None,
                None,
                0x01,
                ctypes.byref(out_blob),
            )
            _ = (data_buffer, entropy_buffer)
            if not ok:
                return token
            try:
                plain = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                return plain.decode("utf-8")
            finally:
                kernel32.LocalFree(out_blob.pbData)
        except Exception:
            return token

    def _read_local_key(self) -> bytes | None:
        if self.key_dir is None:
            return None
        key_path = self.key_dir / LOCAL_KEY_FILE
        if not key_path.exists():
            return None
        try:
            return base64.b64decode(key_path.read_text(encoding="ascii"))
        except Exception:
            return None

    @staticmethod
    def _xor_with_keystream(data: bytes, key: bytes, nonce: bytes) -> bytes:
        output = bytearray()
        counter = 0
        while len(output) < len(data):
            block = hmac.new(
                key,
                nonce + counter.to_bytes(8, "big"),
                hashlib.sha256,
            ).digest()
            output.extend(block)
            counter += 1
        return bytes(a ^ b for a, b in zip(data, output))

    @staticmethod
    def _b64decode(data: bytes) -> bytes:
        padding = b"=" * ((4 - len(data) % 4) % 4)
        return base64.urlsafe_b64decode(data + padding)

    @staticmethod
    def _blob_from_bytes(data: bytes, blob_type):
        buffer = ctypes.create_string_buffer(data)
        blob = blob_type(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return blob, buffer
