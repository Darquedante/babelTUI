#!/usr/bin/env python3
"""
Multi-Protocol Interactive Browser
Supports kepler://, keplers://, spartan://, gemini://, nex://, gopher://, and finger:// protocols

Commands: go, back, forward, reload, up, finger, find, links, history,
          bookmark, bookmarks, source, clear, help, quit, save, set
"""
from __future__ import annotations

import argparse
import functools
import getpass
import hashlib
import json
import mimetypes
import os
import re
import readline
import shutil
import socket
import ssl
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import (
    quote_from_bytes,
    unquote_to_bytes,
    urljoin,
    urlparse,
    urlunparse,
)

# ── URL Scheme Registration ─────────────────────────────────────────────────
#
# Teach urllib.parse that our smolnet schemes use standard RFC 3986
# relative-resolution semantics (authority + path). Without this,
# urljoin() returns the bare relative reference for unknown schemes,
# breaking server redirects like "30 /index.gmi".
#
# Yes, we're poking at urllib.parse internals. They've been stable since
# Python 2.x and are effectively API even if not formally documented as
# such. If a future stdlib version breaks this, the defensive fallback
# in resolve_url() below will catch it.
# ────────────────────────────────────────────────────────────────────────────
from urllib import parse as _urlparse_mod

for _scheme in ("kepler", "keplers", "spartan", "gemini",
                "nex", "gopher", "gopher-search", "finger", "telnet"):
    if _scheme not in _urlparse_mod.uses_relative:
        _urlparse_mod.uses_relative.append(_scheme)
    if _scheme not in _urlparse_mod.uses_netloc:
        _urlparse_mod.uses_netloc.append(_scheme)

# Initialize mimetypes for Nex/Gopher extension detection
mimetypes.init()

# ── Constants ───────────────────────────────────────────────────────────────

DEFAULT_TIMEOUT = 15
_MAX_REDIRECTS = 10
_MAX_REDIRECTS_KEPLER = 5
_HISTORY_LIMIT = 500
_HISTORY_SAVE_INTERVAL = 10
_BUFFER_SIZE = 4096
_KEPLER_HEADER_SIZE = 8192
_KEPLER_URI_MAX = 1024
_GEMINI_URI_MAX = 1024
_SPARTAN_HEADER_SIZE = 4096
_GEMINI_HEADER_SIZE = 4096
_PROMPT_URL_MAX_LEN = 45
_PROMPT_URL_TAIL_LEN = 30

# Default ports
_PORT_KEPLER = 2009
_PORT_KEPLERS = 10009
_PORT_SPARTAN = 300
_PORT_GEMINI = 1965
_PORT_NEX = 1900
_PORT_GOPHER = 70
_PORT_FINGER = 79
_PORT_TELNET = 23

# ── File Paths ───────────────────────────────────────────────────────────────

_CONFIG_DIR       = Path.home() / ".config" / "babeltui"
_BOOKMARK_FILE    = _CONFIG_DIR / "bookmarks.json"
_HISTORY_FILE     = _CONFIG_DIR / "history.json"
_KNOWN_HOSTS_FILE = _CONFIG_DIR / "known_hosts.json"
_CONFIG_FILE      = _CONFIG_DIR / "config.json"


# ── Atomic JSON I/O ─────────────────────────────────────────────────────────

def _atomic_write_json(path: Path, data, *, mode: int = 0o600) -> None:
    """Write JSON atomically (write to .tmp, then replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    tmp.replace(path)


def _read_json(path: Path):
    """Read and parse JSON; return None on any failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


# ── Config Management ───────────────────────────────────────────────────────

@dataclass
class Config:
    """Configuration object for browser settings."""
    home: str = "spartan://mozz.us/"
    timeout: int = DEFAULT_TIMEOUT
    history_limit: int = _HISTORY_LIMIT
    color: bool = True
    pager: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> Config:
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})

    def to_dict(self) -> dict:
        return asdict(self)


def _load_config() -> Config:
    data = _read_json(_CONFIG_FILE)
    if isinstance(data, dict):
        try:
            return Config.from_dict(data)
        except TypeError:
            pass
    return Config()


def _save_config(config: Config) -> None:
    try:
        _atomic_write_json(_CONFIG_FILE, config.to_dict())
    except OSError as exc:
        print(yellow(f"  Warning: could not save config: {exc}"))


# ── Language Detection (Kepler spec §8) ─────────────────────────────────────

@functools.cache
def _get_user_language() -> str:
    """Determine user's preferred language per Kepler §8.1.2."""
    lang = os.environ.get("LANG", "").split(".")[0].replace("_", "-")
    if not lang or lang.upper() in ("C", "POSIX"):
        return "?"
    return lang


# ── Known Hosts (TOFU) ──────────────────────────────────────────────────────

def _load_known_hosts() -> dict[str, str]:
    data = _read_json(_KNOWN_HOSTS_FILE)
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    return {}


def _save_known_hosts(hosts: dict[str, str]) -> None:
    try:
        _atomic_write_json(_KNOWN_HOSTS_FILE, hosts)
    except OSError as exc:
        print(yellow(f"  Warning: could not save known_hosts: {exc}"))


def _get_cert_fingerprint(sock: ssl.SSLSocket) -> str:
    cert_der = sock.getpeercert(binary_form=True)
    if not cert_der:
        raise ssl.SSLError("Server presented no certificate")
    return hashlib.sha256(cert_der).hexdigest()


# ── ANSI Color Helpers ──────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()
_SUPPORTS_ANSI = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def bold(t: str) -> str:
    return _c(t, "1")

def dim(t: str) -> str:
    return _c(t, "2")

def red(t: str) -> str:
    return _c(t, "31")

def green(t: str) -> str:
    return _c(t, "32")

def yellow(t: str) -> str:
    return _c(t, "33")

def cyan(t: str) -> str:
    return _c(t, "36")

def bright_red(t: str) -> str:
    return _c(t, "91")

def bright_green(t: str) -> str:
    return _c(t, "92")

def bright_yellow(t: str) -> str:
    return _c(t, "93")

def bright_blue(t: str) -> str:
    return _c(t, "94")

def bright_cyan(t: str) -> str:
    return _c(t, "96")

def bright_white(t: str) -> str:
    return _c(t, "97")


def set_use_color(enabled: bool) -> None:
    global _USE_COLOR
    _USE_COLOR = enabled


# ── Terminal Helpers ────────────────────────────────────────────────────────

def term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def term_height() -> int:
    try:
        return os.get_terminal_size().lines
    except OSError:
        return 24


def hr(char: str = "─", color_fn=dim) -> str:
    return color_fn(char * term_width())


def clear_screen() -> None:
    if _SUPPORTS_ANSI:
        print("\033[2J\033[H", end="", flush=True)
    else:
        print("\n" * term_height(), end="")


def _clear_eol() -> str:
    """ANSI 'erase to end of line' (or empty string if no ANSI support)."""
    return "\033[K" if _SUPPORTS_ANSI else "    "


# ── Pager Support ───────────────────────────────────────────────────────────

def get_pager_command() -> Optional[list[str]]:
    pager = os.environ.get("PAGER")
    if pager:
        return pager.split()
    if shutil.which("less"):
        return ["less", "-R"]
    if shutil.which("more"):
        return ["more"]
    return None


def page_content(lines: list[str]) -> None:
    cmd = get_pager_command()
    if cmd:
        try:
            text = "\n".join(lines) + "\n"
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, text=True)
            proc.communicate(text)
            return
        except BrokenPipeError:
            return
        except (OSError, subprocess.SubprocessError) as exc:
            print(yellow(f"  Pager failed ({exc}), falling back to normal display"))
    for line in lines:
        print(line)


# ── Bookmark / History Persistence ──────────────────────────────────────────

def _load_bookmarks() -> dict[str, str]:
    data = _read_json(_BOOKMARK_FILE)
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    return {}


def _save_bookmarks(bookmarks: dict[str, str]) -> None:
    try:
        _atomic_write_json(_BOOKMARK_FILE, bookmarks)
    except OSError as exc:
        print(yellow(f"  Warning: could not save bookmarks: {exc}"))


def _load_history(limit: int = _HISTORY_LIMIT) -> list[str]:
    data = _read_json(_HISTORY_FILE)
    if isinstance(data, list):
        return [str(u) for u in data[-limit:]]
    return []


def _save_history(history: list[str], limit: int = _HISTORY_LIMIT) -> None:
    try:
        _atomic_write_json(_HISTORY_FILE, history[-limit:])
    except OSError as exc:
        print(yellow(f"  Warning: could not save history: {exc}"))


# ── Network Helpers ─────────────────────────────────────────────────────────

def _require_host(parts) -> str:
    host = parts.hostname
    if not host:
        raise ValueError(f"{parts.scheme} URI missing host")
    return host


def _recv_all(sock: socket.socket, buf_size: int = _BUFFER_SIZE) -> bytes:
    """Read from socket until EOF."""
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(buf_size)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _read_n_bytes(fp, n: int, buf_size: int = _BUFFER_SIZE) -> bytes:
    """Read exactly n bytes (or until EOF)."""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = fp.read(min(remaining, buf_size))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _parse_status_code(token: str) -> int:
    """Validate a 2-digit status code and return it as int."""
    if len(token) != 2 or not token.isdigit():
        raise ValueError(f"Invalid status code: {token!r}")
    return int(token)


def _try_int(s: str, default: int = -1) -> int:
    """Best-effort int parse; returns default on failure."""
    try:
        return int(s)
    except (ValueError, TypeError):
        return default


def _make_tls_context(min_tls_1_2: bool = False) -> ssl.SSLContext:
    """Build a TOFU-style SSL context (chain verification disabled)."""
    context = ssl.create_default_context()
    context.check_hostname = False
    # We intentionally disable CA verification — TOFU pins the cert fingerprint.
    # Note: this also disables expiry checks; consider this acceptable for
    # smolnet-style protocols where self-signed is the norm.
    context.verify_mode = ssl.CERT_NONE
    if min_tls_1_2:
        try:
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        except (AttributeError, ValueError):
            pass
    return context


def _tofu_check(
    sock: ssl.SSLSocket,
    host: str,
    known_hosts: dict[str, str],
    accept_new_host: bool,
) -> bool:
    """Verify a TLS cert via TOFU. Returns True if known_hosts was modified."""
    actual_fp = _get_cert_fingerprint(sock)
    expected_fp = known_hosts.get(host)

    if expected_fp is None:
        if accept_new_host:
            known_hosts[host] = actual_fp
            return True
        raise ssl.SSLError(f"Unknown host {host} and accept_new_host=False")

    if actual_fp == expected_fp:
        return False

    # Fingerprint mismatch — interactive confirmation required
    print(bright_red(f"\n  ⚠  WARNING: Certificate fingerprint mismatch for {host}!"))
    print(f"  Expected: {expected_fp}")
    print(f"  Actual:   {actual_fp}")
    if not accept_new_host or not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ssl.SSLError("Certificate fingerprint mismatch, connection rejected")
    try:
        ans = input(cyan("  Accept new certificate? [y/N]: ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        raise ssl.SSLError("User rejected new certificate")
    if ans != "y":
        raise ssl.SSLError("User rejected new certificate")
    known_hosts[host] = actual_fp
    return True


# ── Protocol Fetch Functions ────────────────────────────────────────────────

def fetch_kepler(
    url: str,
    timeout: int,
    known_hosts: dict[str, str],
    accept_new_host: bool = True,
    last_cached: int = 0,
    language: Optional[str] = None,
) -> tuple[int, str, bytes, dict]:
    """Fetch a kepler:// or keplers:// URL. Returns (code, meta, body, extras)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("kepler", "keplers"):
        raise ValueError(f"Unsupported scheme: {parsed.scheme!r}")

    # §2.2.2
    if parsed.username or parsed.password:
        raise ValueError("Kepler URIs MUST NOT contain userinfo")
    clean_url = url.split("#")[0]
    if len(clean_url.encode("utf-8")) > _KEPLER_URI_MAX:
        raise ValueError(f"Kepler URI exceeds {_KEPLER_URI_MAX} bytes")

    use_tls = parsed.scheme == "keplers"
    host = _require_host(parsed)
    port = parsed.port or (_PORT_KEPLERS if use_tls else _PORT_KEPLER)

    lang = language if language is not None else _get_user_language()
    request = f"{clean_url} {last_cached} {lang}\r\n".encode("utf-8")

    needs_host_save = False

    with socket.create_connection((host, port), timeout=timeout) as raw_sock:
        if use_tls:
            context = _make_tls_context(min_tls_1_2=True)
            with context.wrap_socket(raw_sock, server_hostname=host) as sock:
                needs_host_save = _tofu_check(sock, host, known_hosts, accept_new_host)
                sock.sendall(request)
                result = _parse_kepler_response(sock.makefile("rb"))
        else:
            raw_sock.sendall(request)
            result = _parse_kepler_response(raw_sock.makefile("rb"))

    if needs_host_save:
        _save_known_hosts(known_hosts)

    return result


def _parse_kepler_response(fp) -> tuple[int, str, bytes, dict]:
    """Parse a Kepler response per §3.1.3.

    Success line format (§3.1.3):
        <code> [<content_length> <last_updated> <expires>] <mimetype>

    We parse defensively: any numeric prefix tokens (up to 3) are
    interpreted as content_length / last_updated / expires in order,
    and the remainder is treated as the mimetype. This means a minimal
    success line such as ``20 text/gemini`` is handled correctly, and
    ``20 1024 text/gemini`` will set content_length=1024 with the other
    fields left at their -1 defaults.
    """
    status_line = fp.readline(_KEPLER_HEADER_SIZE).decode("utf-8", errors="replace").strip("\r\n")
    if not status_line:
        raise ValueError("Empty response from Kepler server")

    tokens = status_line.split(" ", maxsplit=1)
    code = _parse_status_code(tokens[0])

    extras: dict = {}

    # Success (20-29)
    if 20 <= code < 30:
        content_length = -1
        last_updated = -1
        expires = -1
        mimetype = "text/gemini"

        # Split everything after the status code.
        rest_str = tokens[1] if len(tokens) > 1 else ""
        rest = rest_str.split() if rest_str else []

        # Consume leading numeric tokens as the optional metadata triple.
        # Up to 3 numerics: content_length, last_updated, expires.
        numeric_slots: list[Optional[int]] = [None, None, None]
        consumed = 0
        for i, tok in enumerate(rest[:3]):
            try:
                numeric_slots[i] = int(tok)
                consumed += 1
            except ValueError:
                break

        if numeric_slots[0] is not None:
            content_length = numeric_slots[0]
        if numeric_slots[1] is not None:
            last_updated = numeric_slots[1]
        if numeric_slots[2] is not None:
            expires = numeric_slots[2]

        # Whatever remains is the mimetype.
        remaining_tokens = rest[consumed:]
        if remaining_tokens:
            mimetype = " ".join(remaining_tokens)

        if content_length > 0:
            body = _read_n_bytes(fp, content_length)
        else:
            body = fp.read()

        extras = {
            "last_updated": last_updated,
            "expires": expires,
            "content_length": content_length,
        }
        return code, mimetype, body, extras

    # Unchanged (70-79)
    if 70 <= code < 80:
        parts = status_line.split(None, 1)
        expires_str = parts[1] if len(parts) > 1 else "-1"
        extras["expires"] = _try_int(expires_str, -1)
        return code, expires_str, b"", extras

    # 1x/3x/4x/5x/6x
    meta = tokens[1] if len(tokens) > 1 else ""
    return code, meta, b"", extras


def fetch_spartan(url: str, data: bytes, timeout: int) -> tuple[int, str, bytes]:
    """Fetch a spartan:// URL."""
    parts = urlparse(url)
    if parts.scheme != "spartan":
        raise ValueError(f"Unsupported scheme: {parts.scheme!r}")

    host = _require_host(parts)
    port = parts.port or _PORT_SPARTAN
    path = parts.path or "/"
    query = parts.query

    if not data and query:
        data = query.encode("utf-8")

    try:
        encoded_host = host.encode("idna")
    except UnicodeError as exc:
        raise ValueError(f"Invalid hostname for IDNA encoding: {exc}") from exc

    encoded_path = quote_from_bytes(unquote_to_bytes(path)).encode("ascii")

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(b"%s %s %d\r\n" % (encoded_host, encoded_path, len(data)))
        if data:
            sock.sendall(data)

        fp = sock.makefile("rb")
        response_line = fp.readline(_SPARTAN_HEADER_SIZE).decode("ascii", errors="replace").strip("\r\n")
        tokens = response_line.split(" ", maxsplit=1)
        if len(tokens) < 1 or not tokens[0]:
            raise ValueError(f"Malformed server response: {response_line!r}")

        try:
            code = int(tokens[0])
        except ValueError as exc:
            raise ValueError(f"Invalid status code: {tokens[0]!r}") from exc

        meta = tokens[1] if len(tokens) > 1 else ""
        body = fp.read() if code == 2 else b""

    return code, meta, body


def fetch_gemini(
    url: str,
    timeout: int,
    known_hosts: dict[str, str],
    accept_new_host: bool = True,
) -> tuple[int, str, bytes]:
    """Fetch a gemini:// URL with TOFU certificate pinning."""
    parts = urlparse(url)
    if parts.scheme != "gemini":
        raise ValueError(f"Unsupported scheme: {parts.scheme!r}")

    host = _require_host(parts)
    port = parts.port or _PORT_GEMINI

    clean_url = url.split("#")[0]
    if len(clean_url.encode("utf-8")) > _GEMINI_URI_MAX:
        raise ValueError(f"Gemini URI exceeds {_GEMINI_URI_MAX} bytes")
    request = clean_url.encode("utf-8") + b"\r\n"

    context = _make_tls_context()
    needs_host_save = False

    with socket.create_connection((host, port), timeout=timeout) as raw_sock:
        with context.wrap_socket(raw_sock, server_hostname=host) as sock:
            needs_host_save = _tofu_check(sock, host, known_hosts, accept_new_host)
            sock.sendall(request)
            fp = sock.makefile("rb")
            status_line = fp.readline(_GEMINI_HEADER_SIZE).decode("ascii", errors="replace").strip("\r\n")
            if not status_line:
                raise ValueError("Empty response from Gemini server")
            tokens = status_line.split(" ", maxsplit=1)
            code = _parse_status_code(tokens[0])
            meta = tokens[1] if len(tokens) > 1 else ""
            body = fp.read() if 20 <= code < 30 else b""

    if needs_host_save:
        _save_known_hosts(known_hosts)

    return code, meta, body


def fetch_nex(url: str, timeout: int) -> tuple[int, str, bytes]:
    """Fetch a nex:// URL."""
    parts = urlparse(url)
    if parts.scheme != "nex":
        raise ValueError(f"Unsupported scheme: {parts.scheme!r}")

    host = _require_host(parts)
    port = parts.port or _PORT_NEX
    path = parts.path or "/"

    request = unquote_to_bytes(path) + b"\r\n"

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(request)
        body = _recv_all(sock)

    if not body:
        return 4, "Empty response (Nex error)", b""
    return 2, "", body


def fetch_gopher(
    url: str,
    timeout: int,
    query: Optional[str] = None,
) -> tuple[int, str, bytes]:
    """Fetch a gopher:// URL with optional search query."""
    parts = urlparse(url)
    if parts.scheme != "gopher":
        raise ValueError(f"Unsupported scheme: {parts.scheme!r}")

    host = _require_host(parts)
    port = parts.port or _PORT_GOPHER
    selector = parts.path.lstrip("/") if parts.path else ""

    if query is not None:
        request = f"{selector}\t{query}\r\n".encode("utf-8")
    else:
        request = selector.encode("utf-8") + b"\r\n"

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(request)
        body = _recv_all(sock)

    return 2, "", body


def fetch_finger(url: str, timeout: int) -> tuple[int, str, bytes]:
    """Fetch a finger:// URL."""
    parts = urlparse(url)
    if parts.scheme != "finger":
        raise ValueError(f"Unsupported scheme: {parts.scheme!r}")

    host = _require_host(parts)
    port = parts.port or _PORT_FINGER

    user = parts.username or parts.path.lstrip("/").strip()
    request = user.encode("utf-8") + b"\r\n"

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(request)
        body = _recv_all(sock)

    if not body:
        return 4, "Empty response from finger server", b""
    return 2, user, body


# ── Finger Renderer ─────────────────────────────────────────────────────────

_FINGER_KNOWN_FIELDS = frozenset({
    "login", "name", "mail", "shell", "directory",
    "office", "phone", "plan", "project", "on since",
    "last login", "no mail", "new mail",
})


def render_finger(body: bytes, url: str) -> list[str]:
    """Render a finger response with light decoration."""
    parts = urlparse(url)
    host = parts.hostname or url
    user = parts.username or parts.path.lstrip("/").strip()
    target = f"{user}@{host}" if user else host

    lines = body.decode("utf-8", errors="replace").splitlines()
    output: list[str] = []
    w = term_width()

    output.append(bold(bright_cyan(f"  Finger: {target}")))
    output.append(dim("  " + "─" * min(len(f"Finger: {target}") + 2, w - 4)))
    output.append("")

    for line in lines:
        stripped = line.strip()
        if ":" in stripped[:20]:
            field_name, _, rest = stripped.partition(":")
            if field_name.lower() in _FINGER_KNOWN_FIELDS:
                output.append(f"  {cyan(bold(field_name + ':'))}{rest}")
                continue
        if stripped.startswith("Plan:") or stripped.startswith("Project:"):
            output.append(f"  {cyan(bold(stripped))}")
            continue
        output.append("  " + line)

    return output


# ── Gopher Helpers ──────────────────────────────────────────────────────────

GOPHER_ITEM_TYPES: dict[str, str] = {
    '0': 'Text file', '1': 'Directory', '2': 'CSO phone-book',
    '3': 'Error code', '4': 'BinHexed Mac file', '5': 'DOS binary archive',
    '6': 'UUEncoded file', '7': 'Search server', '8': 'Telnet session',
    '9': 'Binary file', 'g': 'GIF image', 'I': 'Image file',
    'h': 'HTML/web link', 'i': 'Informational message', 's': 'Audio file',
    'T': 'TN3270 session',
}


def is_gopher_menu(body: bytes) -> bool:
    """Return True if the bytes look like a Gopher directory menu."""
    try:
        text = body.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return False

    lines = text.strip().splitlines()
    if len(lines) < 3 or lines[-1].strip() != ".":
        return False

    valid = 0
    total = 0
    for line in lines[:-1]:
        if not line.strip():
            continue
        total += 1
        # Real Gopher menu lines are tab-separated with a 1-char type prefix
        if line[0] in GOPHER_ITEM_TYPES and "\t" in line:
            valid += 1
        else:
            return False

    return total > 0 and valid >= max(1, total * 0.7)


def gopher_menu_to_gemtext(body: bytes, base_url: str) -> str:
    """Convert a Gopher directory menu to clean gemtext for rendering."""
    text = body.decode("utf-8", errors="replace")
    lines = text.splitlines()
    out: list[str] = []
    base_parts = urlparse(base_url)
    base_host = base_parts.hostname
    base_port = base_parts.port or _PORT_GOPHER

    for line in lines:
        line = line.rstrip("\r\n")
        if line.strip() == ".":
            break
        if not line:
            out.append("")
            continue

        item_type = line[0]
        fields_ = line[1:].split("\t")
        display = fields_[0] if len(fields_) > 0 else ""
        selector = fields_[1] if len(fields_) > 1 else ""
        host = fields_[2] if len(fields_) > 2 and fields_[2] else base_host
        try:
            port = int(fields_[3]) if len(fields_) > 3 and fields_[3] else base_port
        except ValueError:
            port = base_port

        port_part = f":{port}" if port != _PORT_GOPHER else ""
        path = f"/{selector}" if selector else "/"

        if item_type == 'i':
            if display.strip():
                out.append(display)
        elif item_type == 'h':
            if selector.startswith("URL:"):
                out.append(f"=> {selector[4:]} {display}")
            else:
                out.append(f"=> {selector} {display}")
        elif item_type in ('0', '1'):
            out.append(f"=> gopher://{host}{port_part}{path} {display}")
        elif item_type == '7':
            out.append(f"=> gopher-search://{host}{port_part}{path} {display} 🔍")
        elif item_type in ('8', 'T'):
            telnet_port = f":{port}" if port != _PORT_TELNET else ""
            out.append(f"=> telnet://{host}{telnet_port} {display} (telnet)")
        else:
            desc = GOPHER_ITEM_TYPES.get(item_type, item_type)
            out.append(f"=> gopher://{host}{port_part}{path} 📎 {display} ({desc})")

    return "\n".join(out)


# ── URL Resolution ──────────────────────────────────────────────────────────

def resolve_url(base: str, link: str) -> str:
    """Resolve a relative link against the base URL using RFC 3986 rules.

    Delegates to urllib.parse.urljoin, which (after our scheme registration
    at module load) handles smolnet schemes correctly. As a defence in
    depth, if urljoin returns a schemeless reference — which would indicate
    either the registration failed or a future stdlib version changed
    behaviour — we fall back to a manual netloc-based join.
    """
    # If the link is already absolute, urljoin returns it unchanged.
    resolved = urljoin(base, link)

    # Defensive fallback: if no scheme survived, do it by hand.
    if "://" not in resolved and "://" in base:
        base_parts = urlparse(base)
        if not base_parts.scheme or not base_parts.netloc:
            return resolved  # nothing more we can do
        if link.startswith("//"):
            return f"{base_parts.scheme}:{link}"
        if link.startswith("/"):
            return f"{base_parts.scheme}://{base_parts.netloc}{link}"
        # Relative path — join against base's directory
        base_path = base_parts.path or "/"
        base_dir = base_path.rsplit("/", 1)[0] if "/" in base_path else ""
        new_path = f"{base_dir}/{link}" if base_dir else f"/{link}"
        return f"{base_parts.scheme}://{base_parts.netloc}{new_path}"

    return resolved


def replace_query(url: str, new_query: str) -> str:
    """Replace the query component of a URL (preserves fragment)."""
    p = urlparse(url)
    return urlunparse(p._replace(query=new_query))


# ── Gemtext Renderer ────────────────────────────────────────────────────────

SUPPORTED_SCHEMES = (
    "kepler://", "keplers://", "spartan://", "gemini://",
    "nex://", "gopher://", "gopher-search://", "finger://",
)


def _wrap_line(raw_line: str, w: int) -> list[str]:
    """Word-wrap a single line to width w, hard-breaking tokens longer than w."""
    wrapped = textwrap.wrap(
        raw_line,
        width=max(1, w - 2),
        initial_indent="  ",
        subsequent_indent="  ",
        break_long_words=True,
        break_on_hyphens=False,
    )
    return wrapped or ["  "]


def render_gemtext(text: str, base_url: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Parse gemtext into rendered lines and link list."""
    output: list[str] = []
    links: list[tuple[str, str]] = []
    preformat = False
    w = term_width()

    for raw_line in text.splitlines():
        if raw_line.startswith("```"):
            preformat = not preformat
            output.append(
                dim("┌" + "─" * (w - 2) + "┐") if preformat
                else dim("└" + "─" * (w - 2) + "┘")
            )
            continue

        if preformat:
            output.append(dim("│ ") + raw_line)
            continue

        if raw_line.startswith("=>"):
            rest = raw_line[2:].strip()
            tokens = rest.split(None, 1)
            if not tokens:
                continue
            raw_href = tokens[0]
            label = tokens[1] if len(tokens) > 1 else raw_href
            full_url = resolve_url(base_url, raw_href)
            n = len(links) + 1
            links.append((label, full_url))

            is_supported = any(full_url.startswith(s) for s in SUPPORTED_SCHEMES)
            url_text = bright_blue(label) if is_supported else yellow(label)
            output.append(f"  {bright_cyan(f'[{n}]')} {cyan('→')} {url_text}")
            output.append(f"       {dim(full_url)}")
            continue

        if raw_line.startswith("###"):
            h = raw_line[3:].strip()
            output += ["", green(f"  ▸▸▸ {h}"), ""]
            continue
        if raw_line.startswith("##"):
            h = raw_line[2:].strip()
            output += ["", bold(bright_green(f"  ▸▸ {h}")), ""]
            continue
        if raw_line.startswith("#"):
            h = raw_line[1:].strip()
            output += [
                "",
                bold(bright_yellow(f"  ▸ {h}")),
                dim("  " + "═" * min(len(h) + 4, w - 4)),
                "",
            ]
            continue

        if raw_line.startswith("* "):
            output.append(f"  {cyan('•')} {raw_line[2:]}")
            continue

        if raw_line.startswith(">"):
            output.append(f"  {dim('│')} {dim(raw_line[1:].strip())}")
            continue

        if not raw_line:
            output.append("")
            continue

        if len(raw_line) > w - 4:
            output.extend(_wrap_line(raw_line, w))
        else:
            output.append("  " + raw_line)

    return output, links


def render_plain(text: str) -> list[str]:
    return ["  " + line for line in text.splitlines()]


# ── Find Mode Helpers ───────────────────────────────────────────────────────

_ANSI_RE = re.compile(r'\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]')


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


def highlight_query(line: str, query: str, current: bool) -> str:
    if not query:
        return line
    pat = re.compile(re.escape(query), re.IGNORECASE)
    repl = (lambda m: bold(bright_yellow(m.group()))) if current \
        else (lambda m: bold(yellow(m.group())))
    return pat.sub(repl, line)


def getch() -> str:
    """Read exactly one keypress without requiring Enter."""
    if os.name == 'nt':
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ('\x00', '\xe0'):
            ch2 = msvcrt.getwch()
            return {
                'H': '\x1b[A', 'P': '\x1b[B',
                'M': '\x1b[C', 'K': '\x1b[D',
            }.get(ch2, ch2)
        return ch

    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == '\x1b':
            if select.select([sys.stdin], [], [], 0.1)[0]:
                ch2 = sys.stdin.read(1)
                if ch2 == '[':
                    seq = ''
                    while select.select([sys.stdin], [], [], 0.05)[0]:
                        ch3 = sys.stdin.read(1)
                        if not ch3 or ch3.isalpha() or ch3 in '~':
                            return '\x1b[' + seq + ch3
                        seq += ch3
                    return '\x1b[' + seq
                return '\x1b' + ch2
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _find_mode(render_lines: list[str], match_indices: list[int], query: str) -> None:
    """Interactive find mode."""
    if not match_indices:
        return

    current = 0
    match_set = set(match_indices)
    n_matches = len(match_indices)
    plain_lines = [strip_ansi(l) for l in render_lines]

    def draw() -> None:
        clear_screen()
        for i, rendered in enumerate(render_lines):
            plain = plain_lines[i]
            is_cur = (i == match_indices[current])
            is_match = (i in match_set)
            if is_cur:
                content = plain[2:] if plain.startswith("  ") else plain
                print(bright_yellow("▶ ") + highlight_query(content, query, True))
            elif is_match:
                content = plain[2:] if plain.startswith("  ") else plain
                print(dim("· ") + highlight_query(content, query, False))
            else:
                print(rendered)
        print()
        print(hr("─", bright_yellow))
        pct = f"{current + 1}/{n_matches}"
        print(
            f"  Find: {bold(repr(query))}  "
            f"[{bright_yellow(pct)}]  "
            f"{cyan('n/↓/→')} next  "
            f"{cyan('p/↑/←')} prev  "
            f"{cyan('q/ESC')} exit"
        )

    while True:
        draw()
        try:
            key = getch()
        except (OSError, KeyboardInterrupt):
            break

        if key in ('q', 'Q', '\x1b', '\x03'):
            break
        elif key in ('n', 'N', '\x1b[B', '\x1b[C'):
            current = (current + 1) % n_matches
        elif key in ('p', 'P', '\x1b[A', '\x1b[D'):
            current = (current - 1) % n_matches

    clear_screen()
    for line in render_lines:
        print(line)
    print()


def _interactive_picker(
    items: list,
    title: str,
    render_item: Callable[[int, object, int, bool], list[str]],
    initial_cursor: int = 0,
    footer_extra: str = "",
) -> Optional[int]:
    """Generic interactive picker. Returns selected index or None."""
    n = len(items)
    if n == 0:
        return None

    cursor = max(0, min(initial_cursor, n - 1))
    num_buf = ""
    num_width = len(str(n))

    def draw() -> None:
        clear_screen()
        w = term_width()
        viewport = max(3, term_height() - 8)
        half = viewport // 2
        v_start = max(0, cursor - half)
        v_end = min(n, v_start + viewport)
        v_start = max(0, v_end - viewport)

        print()
        print(f"  {bold(title)}  {dim(f'({n} entries)')}")
        print(hr())

        for i in range(v_start, v_end):
            for ln in render_item(i, items[i], num_width, i == cursor):
                print(ln)

        if n > viewport:
            pct = int(100 * v_end / n)
            print(dim(f"  {'─' * (num_width + 2)}  [{pct:3d}%]"))

        print(hr())
        jump = (f"  Jump → #{bright_yellow(num_buf)}  │  ") if num_buf else "  "
        print(
            f"{jump}"
            f"{cyan('↑/↓')} move  "
            f"{cyan('Enter')} open  "
            f"{cyan('0-9')} jump  "
            f"{cyan('BS')} erase  "
            f"{cyan('q/ESC')} cancel{footer_extra}"
        )
        print()

    while True:
        draw()
        try:
            key = getch()
        except (OSError, KeyboardInterrupt):
            clear_screen()
            return None

        if key in ('q', 'Q', '\x1b', '\x03'):
            clear_screen()
            return None
        elif key in ('\r', '\n'):
            if num_buf:
                try:
                    target = int(num_buf) - 1
                    if 0 <= target < n:
                        clear_screen()
                        return target
                except ValueError:
                    pass
                num_buf = ""
            else:
                clear_screen()
                return cursor
        elif key == '\x1b[A':
            num_buf = ""
            cursor = max(0, cursor - 1)
        elif key == '\x1b[B':
            num_buf = ""
            cursor = min(n - 1, cursor + 1)
        elif key in ('\x7f', '\x08'):
            num_buf = num_buf[:-1]
        elif key.isdigit():
            candidate = num_buf + key
            try:
                val = int(candidate)
                if val <= n and len(candidate) <= len(str(n)):
                    num_buf = candidate
            except ValueError:
                pass


def _trunc(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    keep = max_len - 1
    half = keep // 2
    return s[:half] + "…" + s[-(keep - half):]


def _history_mode(history: list[str], hist_pos: int) -> Optional[int]:
    def render(i: int, url: str, num_width: int, is_cursor: bool) -> list[str]:
        w = term_width()
        url_max = max(20, w - (num_width + 6))
        url_disp = _trunc(url, url_max)
        num = str(i + 1).rjust(num_width)
        if is_cursor:
            return [f"  {bright_cyan('▶')} {bright_cyan(num)}  {bright_white(url_disp)}"]
        elif i == hist_pos:
            return [f"  {cyan('·')} {dim(num)}  {cyan(url_disp)}"]
        return [f"    {dim(num)}  {dim(url_disp)}"]

    return _interactive_picker(history, "Browsing History", render, initial_cursor=hist_pos)


def _bookmark_mode(
    bookmarks: dict[str, str],
    current_url: Optional[str] = None,
) -> Optional[tuple[str, str]]:
    items = list(bookmarks.items())
    if not items:
        print(yellow("  No bookmarks saved yet."))
        return None

    def render(i: int, item, num_width: int, is_cursor: bool) -> list[str]:
        name, url = item
        w = term_width()
        name_max = max(20, (w // 2) - 8)
        url_max = max(20, (w // 2) - 8)
        name_disp = name if len(name) <= name_max else name[:name_max - 1] + "…"
        url_disp = url if len(url) <= url_max else url[:url_max - 1] + "…"
        num = str(i + 1).rjust(num_width)
        out = []
        if is_cursor:
            out.append(f"  {bright_cyan('▶')} {bright_cyan(num)}  "
                       f"{bright_white(name_disp.ljust(name_max))}  {dim(url_disp)}")
            if current_url and url == current_url:
                out.append(f"       {yellow('← current page')}")
        else:
            out.append(f"    {dim(num)}  {name_disp.ljust(name_max)}  {dim(url_disp)}")
        return out

    idx = _interactive_picker(items, "Bookmarks", render)
    return items[idx] if idx is not None else None


# ── Tab Completion ──────────────────────────────────────────────────────────

class BrowserCompleter:
    """Readline completer for browser commands."""

    COMMANDS = [
        'go', 'visit', 'navigate', 'g',
        'back', 'b', 'prev',
        'forward', 'fwd', 'f', 'next',
        'reload', 'r', 'refresh',
        'up', '..',
        'finger', 'home',
        'links', 'l', 'ls',
        'find', '/',
        'source', 'url',
        'history', 'hist',
        'delh', 'clearhistory', 'clearhist',
        'bookmark', 'bm', 'mark',
        'bookmarks', 'bms', 'marks',
        'open', 'ob', 'delbm', 'rmbm',
        'save', 'set', 'clear',
        'help', '?', 'h',
        'quit', 'q', 'exit', 'bye',
    ]
    NAV_CMDS = {'go', 'visit', 'navigate', 'g', 'open', 'ob'}
    DELBM_CMDS = {'delbm', 'rmbm'}

    def __init__(self, browser: Browser):
        self.browser = browser
        self.matches: list[str] = []

    def complete(self, text: str, state: int) -> Optional[str]:
        if state == 0:
            line = readline.get_line_buffer()
            words = line.split()

            if len(words) <= 1 and not line.endswith(" "):
                matches = [c for c in self.COMMANDS if c.startswith(text.lower())]
            else:
                cmd = words[0].lower() if words else ""
                if not text:
                    matches = []  # avoid spamming completions on empty
                elif cmd in self.NAV_CMDS:
                    candidates = (
                        list(self.browser.bookmarks.values())
                        + self.browser.history[-50:]
                    )
                    matches = [c for c in candidates if c.startswith(text)]
                elif cmd in self.DELBM_CMDS:
                    matches = [c for c in self.browser.bookmarks if c.startswith(text)]
                elif cmd == 'set':
                    options = ['pager', 'home', 'timeout', 'color', 'history_limit']
                    matches = [c for c in options if c.startswith(text)]
                else:
                    matches = []

            self.matches = matches

        return self.matches[state] if state < len(self.matches) else None


# ── Response Handler Result Type ────────────────────────────────────────────

@dataclass
class HandlerResult:
    """Result of handling a protocol response.

    Fields:
        done:            If True, stop the fetch loop.
        redirect_to:     If set, fetch this URL next.
        new_url:         If set, replaces current_url for the next iteration
                         (used by input substitution).
        reset_redirects: If True, reset redirect depth and visited set
                         (because the next request represents new user intent,
                         e.g. submitting input).
        data:            Request body bytes to send with the next fetch
                         (e.g. for Spartan code 5 input prompts). Defaults
                         to empty.
    """
    done: bool = True
    redirect_to: Optional[str] = None
    new_url: Optional[str] = None
    reset_redirects: bool = False
    data: bytes = b""


# ── Browser Class ───────────────────────────────────────────────────────────

class Browser:
    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or _load_config()
        set_use_color(self.config.color)

        self.history: list[str] = _load_history(self.config.history_limit)
        self.hist_pos: int = len(self.history) - 1
        self.current_url: Optional[str] = None
        self.current_links: list[tuple[str, str]] = []
        self.current_render_lines: list[str] = []
        self.last_body: Optional[bytes] = None
        self.last_mime: Optional[str] = None
        self.bookmarks: dict[str, str] = _load_bookmarks()
        self.known_hosts: dict[str, str] = _load_known_hosts()
        self.history_save_counter: int = 0
        self.pager_enabled: bool = self.config.pager

        # Dispatch tables
        self._fetchers: dict[str, Callable] = {
            "spartan": self._fetch_spartan_wrapper,
            "kepler":  self._fetch_kepler_wrapper,
            "keplers": self._fetch_kepler_wrapper,
            "gemini":  self._fetch_gemini_wrapper,
            "nex":     self._fetch_nex_wrapper,
            "gopher":  self._fetch_gopher_wrapper,
            "finger":  self._fetch_finger_wrapper,
        }
        self._response_handlers: dict[str, Callable] = {
            "spartan": self._handle_spartan,
            "kepler":  self._handle_kepler,
            "keplers": self._handle_kepler,
            "gemini":  self._handle_gemini,
            "nex":     self._handle_nex,
            "gopher":  self._handle_gopher,
            "finger":  self._handle_finger,
        }

        self._setup_readline()

    def _setup_readline(self) -> None:
        completer = BrowserCompleter(self)
        readline.set_completer(completer.complete)
        try:
            if sys.platform == "darwin":
                readline.parse_and_bind("bind ^I rl_complete")
            else:
                readline.parse_and_bind("tab: complete")
        except OSError:
            pass

    @property
    def HOME(self) -> str:
        return self.config.home

    def _get_timeout(self) -> int:
        return self.config.timeout

    def _increment_history_counter(self) -> None:
        self.history_save_counter += 1
        if self.history_save_counter >= _HISTORY_SAVE_INTERVAL:
            _save_history(self.history, self.config.history_limit)
            self.history_save_counter = 0

    def flush_history(self) -> None:
        _save_history(self.history, self.config.history_limit)
        self.history_save_counter = 0

    def close(self) -> None:
        """Flush state to disk."""
        self.flush_history()

    # ── Protocol Fetch Wrappers ─────────────────────────────────────────────

    def _fetch_spartan_wrapper(self, url: str, data: bytes):
        code, meta, body = fetch_spartan(url, data, self._get_timeout())
        return code, meta, body, {}

    def _fetch_kepler_wrapper(self, url: str, data: bytes):
        return fetch_kepler(url, self._get_timeout(), self.known_hosts)

    def _fetch_gemini_wrapper(self, url: str, data: bytes):
        code, meta, body = fetch_gemini(url, self._get_timeout(), self.known_hosts)
        return code, meta, body, {}

    def _fetch_nex_wrapper(self, url: str, data: bytes):
        code, meta, body = fetch_nex(url, self._get_timeout())
        return code, meta, body, {}

    def _fetch_gopher_wrapper(self, url: str, data: bytes):
        code, meta, body = fetch_gopher(url, self._get_timeout())
        return code, meta, body, {}

    def _fetch_finger_wrapper(self, url: str, data: bytes):
        code, meta, body = fetch_finger(url, self._get_timeout())
        return code, meta, body, {}

    def _fetch_url(self, url: str, data: bytes = b""):
        scheme = urlparse(url).scheme
        fetcher = self._fetchers.get(scheme)
        if fetcher is None:
            raise ValueError(f"Unsupported scheme: {scheme!r}")
        return fetcher(url, data)

    # ── Public Navigation ───────────────────────────────────────────────────

    def navigate(self, url: str, push_history: bool = True) -> None:
        self._fetch(url, push_history=push_history)

    def go_back(self) -> None:
        if self.hist_pos > 0:
            self.hist_pos -= 1
            self._fetch(self.history[self.hist_pos], push_history=False)
        else:
            print(yellow("  Already at the beginning of history."))

    def go_forward(self) -> None:
        if self.hist_pos < len(self.history) - 1:
            self.hist_pos += 1
            self._fetch(self.history[self.hist_pos], push_history=False)
        else:
            print(yellow("  Already at the end of history."))

    def reload(self) -> None:
        if self.current_url:
            self._fetch(self.current_url, push_history=False)
        else:
            print(yellow("  Nothing to reload."))

    def go_up(self) -> None:
        if not self.current_url:
            print(yellow("  No page loaded."))
            return
        parts = urlparse(self.current_url)
        path = parts.path.rstrip("/") or "/"
        if path == "/":
            print(yellow("  Already at the root — can't go higher."))
            return
        parent = path.rsplit("/", 1)[0] + "/"
        up_url = f"{parts.scheme}://{parts.netloc}{parent}"
        print(dim(f"  ↑  {up_url}"))
        self.navigate(up_url)

    def finger_query(self, target: str) -> None:
        target = target.strip()
        if not target:
            print(yellow("  Usage: finger user@host  or  finger host"))
            return
        if target.startswith("finger://"):
            url = target
        elif "@" in target:
            user, _, host = target.partition("@")
            url = f"finger://{host}/{user}"
        else:
            url = f"finger://{target}"
        self.navigate(url)

    def find(self, query: str) -> None:
        if not self.current_render_lines:
            print(yellow("  No page loaded — nothing to search."))
            return
        query = query.strip().strip("\"\'")
        if not query:
            print(yellow("  Usage: find <term>   (quotes optional)"))
            return
        q_lower = query.lower()
        match_indices = [
            i for i, line in enumerate(self.current_render_lines)
            if q_lower in strip_ansi(line).lower()
        ]
        if not match_indices:
            print(yellow(f"  No matches for {query!r} on this page."))
            return
        print(dim(f"  Found {len(match_indices)} match(es) — entering find mode…"))
        _find_mode(self.current_render_lines, match_indices, query)

    def open_link(self, raw: str) -> None:
        try:
            idx = int(raw) - 1
        except ValueError:
            print(red(f"  Not a valid link number: {raw!r}"))
            return

        if not (0 <= idx < len(self.current_links)):
            print(red(
                f"  Link [{raw}] doesn't exist — "
                f"page has {len(self.current_links)} link(s)."
            ))
            return

        label, url = self.current_links[idx]

        if url.startswith("gopher-search://"):
            self._handle_gopher_search(url, label)
            return

        if any(url.startswith(s) for s in SUPPORTED_SCHEMES):
            # §4.7.1: warn on TLS→plaintext for Kepler
            if (self.current_url
                    and self.current_url.startswith("keplers://")
                    and url.startswith("kepler://")):
                print(bright_red(
                    "  ⚠  Following link from encrypted keplers:// to plaintext kepler://"
                ))
                if not self._confirm("  Continue? [y/N]: "):
                    return
            self.navigate(url)
        else:
            print(yellow("  External link (unsupported scheme):"))
            print(f"  {dim(url)}")

    def _handle_gopher_search(self, search_url: str, label: str) -> None:
        parts = urlparse(search_url)
        host = parts.hostname
        port = parts.port or _PORT_GOPHER
        selector = parts.path.lstrip("/") if parts.path else ""

        base_url = f"gopher://{host}"
        if port != _PORT_GOPHER:
            base_url += f":{port}"
        if selector:
            base_url += f"/{selector}"

        print(yellow(f"  🔍  {bold(label)}"))
        try:
            query = input(cyan("  ❯ Search query: ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not query:
            return

        try:
            code, meta, body = fetch_gopher(base_url, self._get_timeout(), query=query)
            if code == 2:
                self._handle_success(base_url, body, "", push_history=True)
            else:
                print(bright_red(f"  ✗  Gopher search error: {meta}"))
        except (OSError, ValueError) as exc:
            print(bright_red(f"  ✗  Search failed: {exc}"))

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _confirm(self, prompt: str) -> bool:
        try:
            ans = input(cyan(prompt)).strip().lower()
            return ans == "y"
        except (EOFError, KeyboardInterrupt):
            print()
            return False

    def _prompt_input(self, prompt_text: str, sensitive: bool = False) -> Optional[str]:
        marker = "🔒" if sensitive else "?"
        print(yellow(f"\n  {marker}  {bold(prompt_text)}"))
        try:
            if sensitive:
                return getpass.getpass(cyan("  ❯ "))
            return input(cyan("  ❯ ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

    # ── Bookmarks ───────────────────────────────────────────────────────────

    def bookmark_add(self, name: str = "") -> None:
        if not self.current_url:
            print(yellow("  No page to bookmark."))
            return
        if self.current_url.startswith("gopher-search://"):
            print(yellow("  Cannot bookmark a Gopher search endpoint directly."))
            return
        if not name:
            try:
                name = input(cyan(f"  Bookmark name [{self.current_url}]: ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not name:
                name = self.current_url
        self.bookmarks[name] = self.current_url
        _save_bookmarks(self.bookmarks)
        print(green(f"  ✓ Bookmarked: {bold(name)} → {self.current_url}"))

    def bookmark_open(self, identifier: str) -> None:
        try:
            idx = int(identifier) - 1
            items = list(self.bookmarks.items())
            if 0 <= idx < len(items):
                self.navigate(items[idx][1])
                return
        except ValueError:
            pass
        if identifier in self.bookmarks:
            self.navigate(self.bookmarks[identifier])
        else:
            print(red(f"  Bookmark not found: {identifier!r}"))

    def bookmark_delete(self, name: str) -> None:
        if name in self.bookmarks:
            del self.bookmarks[name]
            _save_bookmarks(self.bookmarks)
            print(green(f"  ✓ Removed bookmark: {name}"))
        else:
            print(red(f"  No bookmark named {name!r}"))

    def bookmark_picker(self) -> None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            result = _bookmark_mode(self.bookmarks, self.current_url)
            if result:
                name, url = result
                print(dim(f"  Opening {name}…"))
                self.navigate(url)
        else:
            self.show_bookmarks()

    # ── Display Helpers ─────────────────────────────────────────────────────

    def show_links(self, pattern: str = "") -> None:
        if not self.current_links:
            print(yellow("  No links on this page."))
            return
        print()
        print(bold("  Links on this page:"))
        print()
        for i, (label, url) in enumerate(self.current_links, 1):
            if pattern:
                pat_lower = pattern.lower()
                if not (pat_lower in label.lower() or pat_lower in url.lower()):
                    continue
            print(f"  {bright_cyan(f'[{i}]')} {label}")
            print(f"       {dim(url)}")
        print()

    def show_history(self) -> None:
        if not self.history:
            print(yellow("  History is empty."))
            return
        if sys.stdin.isatty() and sys.stdout.isatty():
            idx = _history_mode(self.history, self.hist_pos)
            if idx is not None:
                self.hist_pos = idx
                self._fetch(self.history[idx], push_history=False)
            return
        print()
        print(bold("  Browsing history:"))
        print()
        for i, url in enumerate(self.history):
            marker = bright_cyan("▶ ") if i == self.hist_pos else "  "
            print(f"  {marker}{dim(str(i + 1).rjust(3))}  {url}")
        print()

    def history_delete(self, identifier: str) -> None:
        if not self.history:
            print(yellow("  History is empty."))
            return
        idx: Optional[int] = None
        try:
            n = int(identifier) - 1
            if 0 <= n < len(self.history):
                idx = n
            else:
                print(red(f"  No history entry [{identifier}] — "
                           f"valid range is 1–{len(self.history)}."))
                return
        except ValueError:
            for i, url in enumerate(self.history):
                if url == identifier:
                    idx = i
                    break
            if idx is None:
                print(red(f"  No history entry matching {identifier!r}"))
                return

        removed = self.history.pop(idx)
        if self.hist_pos >= len(self.history):
            self.hist_pos = len(self.history) - 1
        elif idx < self.hist_pos:
            self.hist_pos -= 1
        self.flush_history()
        print(green(f"  ✓  Removed: {dim(removed)}"))

    def history_clear(self) -> None:
        if not self.history:
            print(yellow("  History is already empty."))
            return
        if not self._confirm(f"  Clear all {len(self.history)} history entries? [y/N]: "):
            print(dim("  Cancelled."))
            return
        self.history = []
        self.hist_pos = -1
        self.flush_history()
        print(green("  ✓  History cleared."))

    def show_bookmarks(self) -> None:
        if not self.bookmarks:
            print(yellow("  No bookmarks saved yet."))
            return
        print()
        print(bold("  Bookmarks:"))
        print()
        for i, (name, url) in enumerate(self.bookmarks.items(), 1):
            marker = "← " if url == self.current_url else "  "
            print(f"  {bright_cyan(f'[{i}]')} {marker}{bold(name)}")
            print(f"       {dim(url)}")
        print()

    def save_page(self, filename: str = "") -> None:
        if self.last_body is None:
            print(yellow("  No page loaded to save."))
            return
        if not filename:
            if self.current_url:
                filename = self.current_url.rstrip("/").split("/")[-1] or "page.dat"
            else:
                filename = "page.dat"
        filename = os.path.basename(filename)
        if not filename or filename.startswith("."):
            filename = "page.dat"

        if Path(filename).exists():
            if not self._confirm(f"  {filename} exists. Overwrite? [y/N]: "):
                print(dim("  Cancelled."))
                return
        try:
            with open(filename, "wb") as fh:
                fh.write(self.last_body)
            print(green(f"  ✓  Saved {len(self.last_body):,} bytes → {filename}"))
        except OSError as exc:
            print(bright_red(f"  ✗  Failed to save: {exc}"))

    def show_source(self) -> None:
        if self.last_body is None:
            print(yellow("  No source available."))
            return
        print()
        print(hr())
        try:
            text = self.last_body.decode("utf-8", errors="replace")
            for line in text.splitlines():
                print(dim("  " + line))
        except UnicodeDecodeError:
            print(red("  Could not display source (binary content)"))
        print(hr())
        print()

    def show_url(self) -> None:
        if self.current_url:
            print(f"\n  {bright_cyan('URL:')} {self.current_url}\n")
        else:
            print(yellow("  No page loaded."))

    def show_help(self) -> None:
        print()
        print(hr("═", bright_cyan))
        print(bold(bright_cyan("  MULTI-PROTOCOL BROWSER — HELP")))
        print(hr("═", bright_cyan))
        print()

        def row(cmd: str, desc: str) -> None:
            print(f"  {cyan(cmd.ljust(26))} {desc}")

        print(bold("  Navigation"))
        row("go <url>",            "Navigate to any supported URL")
        row("<number>",            "Follow a link by its number")
        row("back  /  b",          "Go back in history")
        row("forward  /  f",       "Go forward in history")
        row("up  /  ..",           "Go up one directory level")
        row("reload  /  r",        "Reload the current page")
        row("home",                f"Go to {self.HOME}")
        row("finger <user@host>",  "Finger a user")
        print()
        print(bold("  Page"))
        row("links  /  l",         "List all links")
        row("find <term>  /  /",   "Search page")
        row("source",              "View raw source")
        row("save [file]",         "Save current page")
        row("url",                 "Show current URL")
        print()
        print(bold("  History & Bookmarks"))
        row("history  /  hist",    "Browse history")
        row("delh <n>",            "Delete history entry")
        row("clearhistory",        "Purge all history")
        row("bookmark [name]",     "Bookmark current page")
        row("bookmarks  /  bms",   "List bookmarks")
        row("open <n|name>",       "Open bookmark")
        row("delbm <name>",        "Delete a bookmark")
        print()
        print(bold("  Configuration"))
        row("set pager on|off",    "Enable/disable pager")
        row("set color on|off",    "Enable/disable colors")
        row("set home <url>",      "Set home page")
        row("set timeout <secs>",  "Set connection timeout")
        row("set history_limit <n>", "Set max history entries")
        print()
        print(bold("  Misc"))
        row("clear",               "Clear the screen")
        row("help  /  ?",          "Show this help")
        row("quit  /  q  /  exit", "Exit the browser")
        print()
        print(hr("═", bright_cyan))
        print()

    # ── Settings Management ─────────────────────────────────────────────────

    def set_option(self, option: str, value: str) -> None:
        option = option.lower()
        value = value.lower()
        bool_on = ("on", "true", "yes", "1")
        bool_off = ("off", "false", "no", "0")

        if option == "pager":
            if value in bool_on:
                self.pager_enabled = True
                self.config.pager = True
            elif value in bool_off:
                self.pager_enabled = False
                self.config.pager = False
            else:
                print(yellow("  Usage: set pager on|off"))
                return
            _save_config(self.config)
            print(green(f"  ✓  Pager {'enabled' if self.config.pager else 'disabled'}"))

        elif option == "color":
            if value in bool_on:
                set_use_color(True)
                self.config.color = True
            elif value in bool_off:
                set_use_color(False)
                self.config.color = False
            else:
                print(yellow("  Usage: set color on|off"))
                return
            _save_config(self.config)
            print(green(f"  ✓  Colors {'enabled' if self.config.color else 'disabled'}"))

        elif option == "home":
            try:
                _ = normalise_url(value)
                self.config.home = value
                _save_config(self.config)
                print(green(f"  ✓  Home page set to {value!r}"))
            except ValueError as e:
                print(yellow(f"  Invalid URL for home: {e}"))

        elif option == "timeout":
            try:
                t = int(value)
                if t > 0:
                    self.config.timeout = t
                    _save_config(self.config)
                    print(green(f"  ✓  Connection timeout set to {t} seconds"))
                else:
                    print(yellow("  Timeout must be a positive integer."))
            except ValueError:
                print(yellow("  Usage: set timeout <integer_seconds>"))

        elif option == "history_limit":
            try:
                limit = int(value)
                if limit >= 0:
                    self.config.history_limit = limit
                    self.flush_history()
                    _save_config(self.config)
                    print(green(f"  ✓  History limit set to {limit} entries"))
                else:
                    print(yellow("  History limit must be non-negative."))
            except ValueError:
                print(yellow("  Usage: set history_limit <integer>"))

        else:
            print(yellow(f"  Unknown option: {option!r}"))

    # ── Response Handlers (one per scheme) ──────────────────────────────────

    def _handle_spartan(self, code, meta, body, extras, current_url, push_history):
        if code == 2:
            mime = meta.split(";")[0].strip().lower() if meta else "text/gemini"
            self._handle_success(current_url, body, mime, push_history)
            return HandlerResult(done=True)
        if code == 3:
            return HandlerResult(done=False, redirect_to=resolve_url(current_url, meta.strip()))
        if code == 4:
            print(bright_red(f"\n  ✗  Client error ({code}): {meta}\n"))
            return HandlerResult(done=True)
        if code == 5:
            # Spartan input request: prompt the user and re-issue the
            # request with the typed text as the request body. The
            # `data` field on HandlerResult carries the bytes through
            # to the next iteration of the fetch loop.
            user_input = self._prompt_input(meta or "Input")
            if user_input is None or user_input == "":
                return HandlerResult(done=True)
            payload = user_input.encode("utf-8")
            return HandlerResult(
                done=False,
                redirect_to=current_url,
                new_url=current_url,
                reset_redirects=True,
                data=payload,
            )
        print(bright_red(f"\n  ✗  Unknown Spartan code {code}: {meta}\n"))
        return HandlerResult(done=True)

    def _handle_kepler(self, code, meta, body, extras, current_url, push_history):
        scheme = urlparse(current_url).scheme

        # 1x — Input
        if 10 <= code < 20:
            sensitive = (code == 11)
            user_input = self._prompt_input(meta or "Input", sensitive=sensitive)
            if not user_input:
                return HandlerResult(done=True)
            text_norm = user_input.replace("\r\n", "\n")
            encoded = quote_from_bytes(text_norm.encode("utf-8"), safe=b"")
            new_url = replace_query(current_url, encoded)
            return HandlerResult(
                done=False,
                redirect_to=new_url,
                new_url=new_url,
                reset_redirects=True,
            )

        # 2x — Success
        if 20 <= code < 30:
            mime_part = meta.split(";")[0].strip().lower() if meta else "text/gemini"
            expires = extras.get("expires", -1)
            if expires > 0 and expires < int(time.time()):
                print(yellow("  ⚠  Document is stale (server-reported expiry is in the past)"))
            self._handle_success(current_url, body, mime_part, push_history)
            return HandlerResult(done=True)

        # 3x — Redirect
        if 30 <= code < 40:
            target = meta.strip()
            if not target:
                print(bright_red("\n  ✗  Empty redirect target\n"))
                return HandlerResult(done=True)
            redirect_url = resolve_url(current_url, target)
            if scheme == "keplers" and redirect_url.startswith("kepler://"):
                print(bright_red("  ⚠  Redirect from encrypted keplers:// to plaintext kepler://"))
                if not self._confirm("  Continue? [y/N]: "):
                    return HandlerResult(done=True)
            label = "Permanent redirect" if code == 31 else "Redirect"
            print(yellow(f"  ⟶  {label} → {redirect_url}"))
            return HandlerResult(done=False, redirect_to=redirect_url)

        # 4x/5x/6x error tables
        errors = {
            40: "Unspecified temporary failure", 41: "Server unavailable",
            42: "CGI error", 43: "Proxy error", 44: "Slow down",
            50: "General permanent failure", 51: "Not found",
            52: "Gone", 53: "Proxy request refused", 59: "Bad request",
            60: "Certificate required", 61: "Certificate not authorized",
            62: "Certificate not valid",
        }
        if 40 <= code < 70:
            desc = errors.get(code, "Error")
            print(bright_red(f"\n  ✗  {desc} ({code}): {meta}"))
            if 60 <= code < 70:
                print(yellow("  Client certificates are not yet supported.\n"))
            return HandlerResult(done=True)

        # 7x — Cache unchanged
        if 70 <= code < 80:
            print(yellow("\n  ℹ  Server reports document unchanged (cached version valid)."))
            print(dim("     (This browser does not cache; use reload to force fetch)\n"))
            return HandlerResult(done=True)

        print(bright_red(f"\n  ✗  Unknown Kepler code {code}: {meta}\n"))
        return HandlerResult(done=True)

    def _handle_gemini(self, code, meta, body, extras, current_url, push_history):
        if 20 <= code < 30:
            mime = meta.split(";")[0].strip().lower() if meta else "text/gemini"
            self._handle_success(current_url, body, mime, push_history)
            return HandlerResult(done=True)
        if 10 <= code < 20:
            user_input = self._prompt_input(meta or "Input")
            if not user_input:
                return HandlerResult(done=True)
            encoded = quote_from_bytes(user_input.encode("utf-8"), safe=b"")
            new_url = replace_query(current_url, encoded)
            return HandlerResult(
                done=False, redirect_to=new_url, new_url=new_url, reset_redirects=True,
            )
        if 30 <= code < 40:
            redirect_url = resolve_url(current_url, meta.strip())
            print(yellow(f"  ⟶  Redirect → {redirect_url}"))
            return HandlerResult(done=False, redirect_to=redirect_url)
        if 40 <= code < 50:
            print(bright_red(f"\n  ✗  Temporary failure ({code}): {meta}\n"))
            return HandlerResult(done=True)
        if 50 <= code < 60:
            print(bright_red(f"\n  ✗  Permanent failure ({code}): {meta}\n"))
            return HandlerResult(done=True)
        print(bright_red(f"\n  ✗  Unknown Gemini code {code}: {meta}\n"))
        return HandlerResult(done=True)

    def _handle_nex(self, code, meta, body, extras, current_url, push_history):
        if code == 2:
            self._handle_success(current_url, body, "", push_history)
        elif code == 4:
            print(bright_red(f"\n  ✗  Nex error: {meta}\n"))
        else:
            print(bright_red(f"\n  ✗  Unknown Nex code {code}: {meta}\n"))
        return HandlerResult(done=True)

    def _handle_gopher(self, code, meta, body, extras, current_url, push_history):
        if code == 2:
            self._handle_success(current_url, body, "", push_history)
        else:
            print(bright_red(f"\n  ✗  Unknown Gopher code {code}: {meta}\n"))
        return HandlerResult(done=True)

    def _handle_finger(self, code, meta, body, extras, current_url, push_history):
        if code == 2:
            self._handle_success(current_url, body, "text/finger", push_history)
        elif code == 4:
            print(bright_red(f"\n  ✗  Finger error: {meta}\n"))
        else:
            print(bright_red(f"\n  ✗  Finger error ({code}): {meta}\n"))
        return HandlerResult(done=True)

    # ── Main Fetch Loop ─────────────────────────────────────────────────────

    def _fetch(self, url: str, data: bytes = b"", push_history: bool = True) -> None:
        """Iteratively fetch+render a URL, handling redirects and input.

        The `current_data` for a request is always taken from either the
        initial `data` parameter or from a HandlerResult.data emitted by
        the previous iteration's response handler. Each iteration resets
        `current_data` to b"" by default; handlers that need to send a
        body (e.g. Spartan code 5 input) must explicitly populate
        HandlerResult.data.
        """
        current_url = url
        current_data = data
        redirect_depth = 0
        visited: set[str] = set()

        while True:
            scheme = urlparse(current_url).scheme
            max_redirects = (
                _MAX_REDIRECTS_KEPLER
                if scheme in ("kepler", "keplers")
                else _MAX_REDIRECTS
            )
            if redirect_depth > max_redirects:
                print(bright_red(f"  ✗  Too many redirects (>{max_redirects}), aborting.\n"))
                return

            # Visited-set guard: only apply when there is no request body.
            # A POST-like request with data is meaningfully different from
            # a previous GET-like fetch of the same URL, so allow it.
            if not current_data and current_url in visited:
                print(bright_red(f"  ✗  Redirect loop detected for {current_url}\n"))
                return
            visited.add(current_url)

            print(dim(f"\n  ⟳  {current_url}"), end="", flush=True)
            try:
                code, meta, body, extras = self._fetch_url(current_url, current_data)
            except (OSError, ValueError, ssl.SSLError) as exc:
                print(f"\r{bright_red(f'  ✗  Error: {exc}')}{_clear_eol()}")
                return

            # Status echo — use ANSI 'erase to end of line' to scrub any
            # leftover characters from the longer "⟳ <url>" line above.
            print(f"\r{dim(f'  {code}  {meta[:60]}')}{_clear_eol()}")

            handler = self._response_handlers.get(scheme)
            if handler is None:
                print(bright_red(f"\n  ✗  No handler for scheme {scheme!r}\n"))
                return

            result = handler(code, meta, body, extras, current_url, push_history)

            if result.done:
                return

            if result.new_url is not None:
                current_url = result.new_url
            elif result.redirect_to is not None:
                current_url = result.redirect_to
            else:
                return

            # Carry request body forward only if the handler set it;
            # otherwise the next request is a plain GET-like fetch.
            current_data = result.data

            if result.reset_redirects:
                redirect_depth = 0
                visited.clear()
            else:
                redirect_depth += 1

    def _handle_success(
        self, url: str, body: bytes, mime: str, push_history: bool
    ) -> None:
        """Shared success handling for all protocols."""
        if push_history:
            del self.history[self.hist_pos + 1:]
            if not self.history or self.history[-1] != url:
                self.history.append(url)
                self._increment_history_counter()
            self.hist_pos = len(self.history) - 1

        self.current_url = url
        self.last_body = body
        self.last_mime = mime
        self._render(body, mime, url)

    def _render(self, body: bytes, mime: str, url: str) -> None:
        """Render the response body based on MIME type and protocol."""
        print()
        print(hr())
        print()

        scheme = urlparse(url).scheme

        if scheme == "gopher":
            self._render_gopher(body, url)
            return

        if scheme == "finger":
            lines = render_finger(body, url)
            self.current_links = []
            self.current_render_lines = lines
            self._emit_lines(lines)
            print()
            print(hr())
            print()
            return

        if scheme == "nex" and not mime:
            parts = urlparse(url)
            guessed_mime, _ = mimetypes.guess_type(parts.path)
            mime = guessed_mime or "text/plain"
            if not parts.path or parts.path.endswith("/"):
                mime = "text/gemini"

        text: Optional[str]
        try:
            text = body.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            text = None

        if text is not None and ("text/gemini" in mime or mime == ""):
            lines, links = render_gemtext(text, url)
            self.current_links = links
        elif text is not None and "text/" in mime:
            lines, links = render_plain(text), []
            self.current_links = []
        else:
            self._handle_binary(body, url, mime)
            return

        self.current_render_lines = lines
        self._emit_lines(lines)
        print()
        print(hr())
        if self.current_links:
            print(dim(
                f"\n  {len(self.current_links)} link(s) on page — "
                f"type a number to follow, or 'links' for the full list."
            ))
        print()

    def _render_gopher(self, body: bytes, url: str) -> None:
        if is_gopher_menu(body):
            try:
                gemtext = gopher_menu_to_gemtext(body, url)
                lines, links = render_gemtext(gemtext, url)
                self.current_links = links
            except (ValueError, UnicodeDecodeError) as e:
                print(bright_red(f"  ✗  Error parsing Gopher menu: {e}"))
                lines = render_plain(body.decode("utf-8", errors="replace"))
                self.current_links = []
        else:
            try:
                text = body.decode("utf-8", errors="replace")
                lines = render_plain(text)
                self.current_links = []
            except UnicodeDecodeError:
                self._handle_binary(body, url, "application/octet-stream")
                return

        self.current_render_lines = lines
        self._emit_lines(lines)
        print()
        print(hr())
        if self.current_links:
            print(dim(
                f"\n  {len(self.current_links)} link(s) on page — "
                f"type a number to follow, or 'links' for the full list."
            ))
        print()

    def _emit_lines(self, lines: list[str]) -> None:
        if self.pager_enabled:
            page_content(lines)
        else:
            for line in lines:
                print(line)

    def _handle_binary(self, body: bytes, url: str, mime: str) -> None:
        self.current_render_lines = []
        self.current_links = []
        print(yellow(f"  Binary content ({mime or 'unknown'}), {len(body):,} bytes"))
        fname = os.path.basename(url.rstrip("/").split("/")[-1] or "download")
        if self._confirm(f"  Save as [{fname}]? [y/N]: "):
            if Path(fname).exists() and not self._confirm(f"  {fname} exists. Overwrite? [y/N]: "):
                print(dim("  Cancelled."))
                return
            try:
                with open(fname, "wb") as fh:
                    fh.write(body)
                print(green(f"  ✓  Saved → {fname}"))
            except OSError as exc:
                print(bright_red(f"  ✗  Failed to save: {exc}"))

    def prompt(self) -> str:
        if self.current_url:
            parts = urlparse(self.current_url)
            scheme = parts.scheme
            netloc = parts.netloc
            path = parts.path or "/"
            display = f"{netloc}{path}"
            if len(display) > _PROMPT_URL_MAX_LEN:
                display = (
                    f"{netloc}…{path[-_PROMPT_URL_TAIL_LEN:]}"
                    if len(path) > _PROMPT_URL_TAIL_LEN
                    else f"…{display[-(_PROMPT_URL_MAX_LEN - 1):]}"
                )
            hist = dim(f"[{self.hist_pos + 1}/{len(self.history)}]")
            scheme_display = green(scheme) if scheme == "keplers" else cyan(scheme)
            return f"{scheme_display}:{bright_cyan(display)} {hist} {bright_cyan('❯')} "
        return f"{bright_cyan('browser')} {bright_cyan('❯')} "


# ── REPL ────────────────────────────────────────────────────────────────────

BANNER = r"""
  ╔══════════════════════════════════════════════════════╗
  ║    ██████╗  █████╗ ██████╗ ███████╗██╗      kepler   ║
  ║    ██╔══██╗██╔══██╗██╔══██╗██╔════╝██║      keplers  ║
  ║    ██████╔╝███████║██████╔╝█████╗  ██║      gemini   ║
  ║    ██╔══██╗██╔══██║██╔══██╗██╔══╝  ██║      gopher   ║
  ║    ██████╔╝██║  ██║██████╔╝███████╗███████╗  nex     ║
  ║    ╚═════╝ ╚═╝  ╚═╝╚═════╝ ╚══════╝╚══════╝  spartan ║
  ║                                              finger  ║
  ║  multi-protocol browser — type help or ? to start    ║
  ╚══════════════════════════════════════════════════════╝
"""


def normalise_url(raw: str) -> str:
    """Add a default scheme if the input has none."""
    if "://" in raw:
        return raw
    if "@" in raw:
        user, _, host = raw.partition("@")
        return f"finger://{host}/{user}"
    if raw == "localhost" or ":" in raw or "." in raw:
        return "spartan://" + raw
    raise ValueError("Ambiguous input: use full URL or user@host for finger")


# ── REPL Command Dispatch ───────────────────────────────────────────────────

def _make_command_table(browser: Browser) -> dict[str, Callable[[str, str], None]]:
    """Build the REPL command dispatch table."""

    def cmd_go(arg, arg2):
        if not arg:
            print(yellow("  Usage: go <url>"))
            return
        try:
            browser.navigate(normalise_url(arg))
        except ValueError as e:
            print(yellow(f"  {e}"))

    def cmd_find(arg, arg2):
        full = (arg + (" " + arg2 if arg2 else "")).strip()
        if not full:
            print(yellow("  Usage: find <term>"))
        else:
            browser.find(full)

    def cmd_delh(arg, arg2):
        if not arg:
            print(yellow("  Usage: delh <n>"))
        else:
            browser.history_delete(arg)

    def cmd_delbm(arg, arg2):
        if not arg:
            print(yellow("  Usage: delbm <name>"))
        else:
            browser.bookmark_delete(arg)

    def cmd_set(arg, arg2):
        if not arg or not arg2:
            print(yellow("  Usage: set <option> <value>"))
            print(dim("  Options: pager, color, home, timeout, history_limit"))
        else:
            browser.set_option(arg, arg2)

    def cmd_open(arg, arg2):
        if not arg:
            browser.bookmark_picker()
        else:
            browser.bookmark_open(arg)

    table: dict[str, Callable[[str, str], None]] = {}

    def register(names, fn):
        for n in names:
            table[n] = fn

    register(("help", "?", "h"), lambda a, b: browser.show_help())
    register(("go", "visit", "navigate", "g"), cmd_go)
    register(("back", "b", "prev"), lambda a, b: browser.go_back())
    register(("forward", "fwd", "f", "next"), lambda a, b: browser.go_forward())
    register(("reload", "r", "refresh"), lambda a, b: browser.reload())
    register(("up", ".."), lambda a, b: browser.go_up())
    register(("finger",), lambda a, b: browser.finger_query(a))
    register(("home",), lambda a, b: browser.navigate(browser.HOME))
    register(("links", "l", "ls"), lambda a, b: browser.show_links(a))
    register(("find", "/"), cmd_find)
    register(("source",), lambda a, b: browser.show_source())
    register(("url",), lambda a, b: browser.show_url())
    register(("save",), lambda a, b: browser.save_page(a))
    register(("history", "hist"), lambda a, b: browser.show_history())
    register(("delh",), cmd_delh)
    register(("clearhistory", "clearhist"), lambda a, b: browser.history_clear())
    register(("bookmark", "bm", "mark"), lambda a, b: browser.bookmark_add(a))
    register(("bookmarks", "bms", "marks"), lambda a, b: browser.bookmark_picker())
    register(("open", "ob"), cmd_open)
    register(("delbm", "rmbm"), cmd_delbm)
    register(("set",), cmd_set)
    register(("clear",), lambda a, b: clear_screen())

    return table


def run_repl(start_url: Optional[str] = None) -> None:
    """Run the interactive REPL."""
    config = _load_config()
    set_use_color(config.color)

    browser = Browser(config)
    commands = _make_command_table(browser)
    quit_cmds = {"quit", "q", "exit", "bye"}

    print(bright_cyan(BANNER))

    if start_url:
        try:
            browser.navigate(normalise_url(start_url))
        except ValueError as e:
            print(yellow(f"  {e}"))

    while True:
        try:
            print(browser.prompt(), end="", flush=True)
            raw = input().strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            continue

        if not raw:
            continue

        if raw.isdigit():
            browser.open_link(raw)
            continue

        tokens = raw.split(None, 2)
        command = tokens[0].lower()
        arg = tokens[1].strip() if len(tokens) > 1 else ""
        arg2 = tokens[2].strip() if len(tokens) > 2 else ""

        if command in quit_cmds:
            browser.close()
            print(bright_cyan("\n  Goodbye! 👋\n"))
            break

        handler = commands.get(command)
        if handler:
            handler(arg, arg2)
        else:
            try:
                browser.navigate(normalise_url(raw))
            except ValueError as e:
                print(yellow(f"  {e}"))


# ── Entry Point ─────────────────────────────────────────────────────────────

def main() -> None:
    """Main entry point for the browser application."""
    parser = argparse.ArgumentParser(description="Multi-Protocol Interactive Browser")
    parser.add_argument(
        "url", nargs="?",
        help="URL to open on start (kepler://, keplers://, spartan://, gemini://, nex://, gopher://, finger://)",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colours")
    parser.add_argument("--pager", action="store_true", help="Enable pager mode")
    parser.add_argument("--home", type=str, help="Set home URL (overrides config)")
    parser.add_argument("--timeout", type=int, help="Set connection timeout (overrides config)")
    parser.add_argument("--history-limit", type=int, help="Set max history entries (overrides config)")

    args = parser.parse_args()
    config = _load_config()

    if args.no_color:
        config.color = False
    if args.pager:
        config.pager = True
    if args.home:
        try:
            _ = normalise_url(args.home)
            config.home = args.home
        except ValueError as e:
            print(yellow(f"  Warning: invalid --home URL: {e}"))
    if args.timeout is not None:
        if args.timeout > 0:
            config.timeout = args.timeout
        else:
            print(yellow("  Warning: --timeout must be positive."))
    if args.history_limit is not None:
        if args.history_limit >= 0:
            config.history_limit = args.history_limit
        else:
            print(yellow("  Warning: --history-limit must be non-negative."))

    _save_config(config)

    try:
        run_repl(args.url)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
