#!/usr/bin/env python3
"""
Multi-Protocol Interactive Browser
Supports kepler://, keplers://, spartan://, gemini://, nex://, gopher://, and finger:// protocols.
Views RSS 2.0 / RSS 1.0 / Atom feeds and manages feed subscriptions.

Commands: go, back, forward, reload, up, finger, find, links, history,
          bookmark, bookmarks, source, clear, help, quit, save, set,
          subscribe, unsubscribe, subscriptions, check, input

Spartan input model
-------------------
Spartan has exactly FOUR status codes and NO input-required status code:

    2  success
    3  redirect
    4  client error
    5  server error

Interactive input is driven entirely client-side by the `=:` content line,
whose format is `=: <url> <prompt label>`. When the user selects such a
prompt the client gathers text and uploads it as the *request body* of the
next request to the target URL (NOT as a percent-encoded query string).

A query string typed directly into a Spartan URL is supported as a
convenience: it is percent-DECODED into raw body bytes before upload (so
`?hello%20world` uploads the 11-byte string "hello world", not the literal
"hello%20world").
"""
from __future__ import annotations

import argparse
import functools
import getpass
import hashlib
import html
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
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
#
# Wrapped in a function and invoked explicitly so that merely *importing*
# this module as a library does not mutate global stdlib state as a silent
# import side effect.
# ────────────────────────────────────────────────────────────────────────────
from urllib import parse as _urlparse_mod

_SMOLNET_SCHEMES = (
    "kepler", "keplers", "spartan", "gemini",
    "nex", "gopher", "gopher-search", "finger", "telnet",
)


def _register_url_schemes() -> None:
    """Register smolnet schemes with urllib.parse for RFC 3986 resolution."""
    for scheme in _SMOLNET_SCHEMES:
        if scheme not in _urlparse_mod.uses_relative:
            _urlparse_mod.uses_relative.append(scheme)
        if scheme not in _urlparse_mod.uses_netloc:
            _urlparse_mod.uses_netloc.append(scheme)


_register_url_schemes()

# Initialize mimetypes for Nex/Gopher extension detection
mimetypes.init()

# ── Constants ───────────────────────────────────────────────────────────────

DEFAULT_TIMEOUT = 15
_MAX_REDIRECTS = 10
_MAX_REDIRECTS_KEPLER = 5
# Independent ceiling on interactive input cycles (Kepler-1x / Gemini-1x).
# These reset the redirect counter, so without their own cap a hostile server
# could trap the user in an unbounded prompt loop (issue #1).
#
# NOTE: Spartan has NO server-driven input cycle — its input is purely
# client-side via the `=:` content line — so this ceiling applies only to
# Kepler and Gemini.
_MAX_INPUT_CYCLES = 20
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

# Cap response bodies to defend against memory exhaustion from hostile or
# malfunctioning servers streaming unbounded data.
_MAX_BODY_SIZE = 50 * 1024 * 1024  # 50 MiB

# Feed limits
_FEED_MAX_ENTRIES = 200
_FEED_SUMMARY_MAX = 280
_FEED_NEW_ENTRIES_SHOWN = 12

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
_FEEDS_FILE       = _CONFIG_DIR / "feeds.json"


# ── Atomic JSON I/O ─────────────────────────────────────────────────────────

def _atomic_write_json(path: Path, data, *, mode: int = 0o600) -> None:
    """Write JSON atomically (write to .tmp, then replace).

    On any failure after the temp file is created, the orphaned .tmp is
    unlinked so a crashed/failed write never leaves stale debris behind
    (issue #8).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


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
    feed_compact: bool = False  # hide entry summaries when True

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
    """Determine user's preferred language per Kepler §8.1.2.

    POSIX locale resolution gives LC_ALL and LC_MESSAGES precedence over
    LANG; honour that ordering rather than reading LANG alone.
    """
    raw = (
        os.environ.get("LC_ALL")
        or os.environ.get("LC_MESSAGES")
        or os.environ.get("LANG")
        or ""
    )
    lang = raw.split(".")[0].replace("_", "-")
    if not lang or lang.upper() in ("C", "POSIX"):
        return "?"
    return lang


# ── Known Hosts (TOFU) ──────────────────────────────────────────────────────

def _tofu_key(host: str, port: int) -> str:
    """Build the known_hosts key.

    TOFU pins must be keyed on host:port, not host alone, so that distinct
    TLS services on the same hostname (e.g. gemini:1965 and keplers:10009)
    do not share — or collide on — a single pin.
    """
    return f"{host}:{port}"


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
#
# _USE_COLOR and _SUPPORTS_ANSI are both seeded from isatty(). We treat
# ANSI support as a function of *both* user intent (color on) and the
# output being a TTY, and keep the two flags in sync so that disabling
# colour also suppresses raw cursor-control escapes.

_IS_TTY = sys.stdout.isatty()
_USE_COLOR = _IS_TTY
_SUPPORTS_ANSI = _IS_TTY


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

def bright_magenta(t: str) -> str:
    return _c(t, "95")

def bright_cyan(t: str) -> str:
    return _c(t, "96")

def bright_white(t: str) -> str:
    return _c(t, "97")


def set_use_color(enabled: bool) -> None:
    """Enable/disable colour output and keep ANSI control support in sync."""
    global _USE_COLOR, _SUPPORTS_ANSI
    _USE_COLOR = enabled
    _SUPPORTS_ANSI = enabled and _IS_TTY


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


def _interactive_ui_available() -> bool:
    """True when full-screen interactive UIs (picker/find) are usable."""
    return _SUPPORTS_ANSI and sys.stdin.isatty() and sys.stdout.isatty()


# ── Wire-Safety Helpers ─────────────────────────────────────────────────────

def _strip_crlf(s: str) -> str:
    """Remove CR/LF (and NUL) from a string before placing it on a request
    line. Prevents request-line injection / smuggling where attacker- or
    user-supplied selectors, queries or finger users embed newlines that
    forge additional protocol lines (issue #7).
    """
    return s.replace("\r", "").replace("\n", "").replace("\x00", "")


def _looks_like_text(body: bytes, sample: int = 4096) -> bool:
    """Heuristic: is this body printable UTF-8 text rather than binary?

    Used to rescue mislabelled bodies (e.g. an echo endpoint that returns
    text as application/octet-stream) so we render them instead of offering
    a file download. Conservative: any NUL byte, or a high proportion of
    undecodable / control bytes, means 'binary'.
    """
    if not body:
        return True  # empty body is trivially "text" (renders as nothing)
    chunk = body[:sample]
    if b"\x00" in chunk:
        return False
    try:
        decoded = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return False
    # Allow common whitespace controls; flag everything else in C0/C1.
    allowed = {"\t", "\n", "\r", "\f", "\v"}
    ctrl = sum(
        1 for ch in decoded
        if (ord(ch) < 0x20 or 0x7f <= ord(ch) < 0xa0) and ch not in allowed
    )
    return ctrl / len(decoded) < 0.05


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


def _encode_host(host: str) -> bytes:
    """Encode a hostname for the wire.

    Python's legacy 'idna' codec (IDNA 2003) rejects perfectly routable
    hosts — underscores, trailing dots, over-long labels — and needlessly
    fails for plain ASCII. Prefer a plain ASCII encode and only fall back
    to IDNA for genuinely non-ASCII names.
    """
    try:
        return host.encode("ascii")
    except UnicodeEncodeError:
        pass
    try:
        return host.encode("idna")
    except UnicodeError as exc:
        raise ValueError(f"Invalid hostname for IDNA encoding: {exc}") from exc


def _recv_all(
    sock: socket.socket,
    buf_size: int = _BUFFER_SIZE,
    max_bytes: int = _MAX_BODY_SIZE,
) -> bytes:
    """Read from socket until EOF, with a hard ceiling."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = sock.recv(buf_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(
                f"Response exceeds maximum body size ({max_bytes:,} bytes)"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _read_n_bytes(
    fp,
    n: int,
    buf_size: int = _BUFFER_SIZE,
    max_bytes: int = _MAX_BODY_SIZE,
) -> bytes:
    """Read exactly n bytes (or until EOF).

    `n` is server-declared (Kepler content_length) and therefore
    attacker-controlled; clamp to max_bytes.
    """
    if n > max_bytes:
        raise ValueError(
            f"Declared content length {n:,} exceeds maximum body size "
            f"({max_bytes:,} bytes)"
        )
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = fp.read(min(remaining, buf_size))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_until_eof(fp, max_bytes: int = _MAX_BODY_SIZE) -> bytes:
    """Read a file-like object to EOF with a size cap."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = fp.read(_BUFFER_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(
                f"Response exceeds maximum body size ({max_bytes:,} bytes)"
            )
        chunks.append(chunk)
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
    port: int,
    known_hosts: dict[str, str],
    accept_new_host: bool,
) -> bool:
    """Verify a TLS cert via TOFU. Returns True if known_hosts was modified.

    Keys the pin on host:port so distinct services on the same host are
    pinned independently.

    On first contact with an unknown host we now emit a visible notice that
    a new certificate has been pinned (issue #6), so the user always has a
    signal that trust was established silently.
    """
    key = _tofu_key(host, port)
    actual_fp = _get_cert_fingerprint(sock)
    expected_fp = known_hosts.get(key)

    if expected_fp is None:
        if accept_new_host:
            known_hosts[key] = actual_fp
            short = actual_fp[:16]
            print(dim(
                f"\r  🔑  Pinned new certificate for {key} "
                f"(sha256:{short}…){_clear_eol()}"
            ))
            return True
        raise ssl.SSLError(f"Unknown host {key} and accept_new_host=False")

    if actual_fp == expected_fp:
        return False

    # Fingerprint mismatch — interactive confirmation required
    print(bright_red(f"\n  ⚠  WARNING: Certificate fingerprint mismatch for {key}!"))
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
    known_hosts[key] = actual_fp
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
                needs_host_save = _tofu_check(
                    sock, host, port, known_hosts, accept_new_host
                )
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
    and the remainder is treated as the mimetype.
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

        rest_str = tokens[1] if len(tokens) > 1 else ""
        rest = rest_str.split() if rest_str else []

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

        remaining_tokens = rest[consumed:]
        if remaining_tokens:
            mimetype = " ".join(remaining_tokens)

        if content_length > 0:
            body = _read_n_bytes(fp, content_length)
        else:
            body = _read_until_eof(fp)

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
    """Fetch a spartan:// URL.

    `data` is the request body (upload). Per the Spartan model a non-empty
    body is how interactive input (`=:` prompts) and form submissions are
    delivered.

    If no explicit body is supplied but the URL carries a query string, the
    query is PERCENT-DECODED into raw body bytes and used as the body. This
    mirrors the convenience behaviour of reference clients while ensuring
    that e.g. `?hello%20world` uploads the 11-byte string "hello world"
    rather than the literal 13-byte "hello%20world".
    """
    parts = urlparse(url)
    if parts.scheme != "spartan":
        raise ValueError(f"Unsupported scheme: {parts.scheme!r}")

    host = _require_host(parts)
    port = parts.port or _PORT_SPARTAN
    path = parts.path or "/"
    query = parts.query

    if not data and query:
        # Decode the percent-encoded query into raw body bytes. The body is
        # arbitrary data, not a URL component, so it must be unquoted before
        # going on the wire (bug: previously sent verbatim with %XX intact).
        data = unquote_to_bytes(query)

    encoded_host = _encode_host(host)
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

        # Spartan defines only single-digit status codes (2/3/4/5). Enforce
        # that rather than accepting e.g. "200" as integer 200 (issue #2),
        # which previously fell through to a confusing "Unknown code" branch.
        code_token = tokens[0]
        if len(code_token) != 1 or not code_token.isdigit():
            raise ValueError(f"Invalid Spartan status code: {code_token!r}")
        code = int(code_token)

        meta = tokens[1] if len(tokens) > 1 else ""
        body = _read_until_eof(fp) if code == 2 else b""

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
            needs_host_save = _tofu_check(
                sock, host, port, known_hosts, accept_new_host
            )
            sock.sendall(request)
            fp = sock.makefile("rb")
            status_line = fp.readline(_GEMINI_HEADER_SIZE).decode("ascii", errors="replace").strip("\r\n")
            if not status_line:
                raise ValueError("Empty response from Gemini server")
            tokens = status_line.split(" ", maxsplit=1)
            code = _parse_status_code(tokens[0])
            meta = tokens[1] if len(tokens) > 1 else ""
            body = _read_until_eof(fp) if 20 <= code < 30 else b""

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

    # Sanitise selector/query before building the request line so an
    # embedded CR/LF cannot forge an extra Gopher request (issue #7).
    selector = _strip_crlf(selector)
    if query is not None:
        safe_query = _strip_crlf(query)
        request = f"{selector}\t{safe_query}\r\n".encode("utf-8")
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
    # Strip CR/LF from the user token before the request line (issue #7):
    # a finger:// URL with an embedded newline could otherwise forge a
    # second query line.
    user = _strip_crlf(user)
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


# ── Feed Parsing (RSS 2.0 / RSS 1.0 / Atom) ─────────────────────────────────
#
# Stdlib-only feed support. We deliberately avoid feedparser to keep this
# dependency-free, at the cost of hand-rolling namespace and date handling.
#
# SECURITY: XML is untrusted input. Python's xml.etree does not resolve
# external entities by default, but entity-expansion ("billion laughs")
# and oversized payloads remain concerns. Mitigations here:
#   * Bodies are already capped at _MAX_BODY_SIZE before we ever parse.
#   * We reject any document containing a DOCTYPE (<!DOCTYPE …>), which
#     blocks the entity-definition vector entirely.
# For stronger guarantees, swap ElementTree for the third-party
# `defusedxml` package — a drop-in that hardens these same APIs.
# ────────────────────────────────────────────────────────────────────────────

# Atom uses this namespace; RSS 2.0 is namespace-less; RSS 1.0 (RDF) uses
# the RSS 1.0 namespace plus Dublin Core for dates.
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "rss10": "http://purl.org/rss/1.0/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
}


@dataclass
class FeedEntry:
    title: str = ""
    link: str = ""
    updated: Optional[datetime] = None
    summary: str = ""

    def identity(self) -> str:
        """Stable key for new-entry detection (prefer link, fall back to title+date)."""
        if self.link:
            return self.link
        date = self.updated.isoformat() if self.updated else ""
        return f"{self.title}|{date}"


@dataclass
class Feed:
    title: str = ""
    subtitle: str = ""
    link: str = ""
    updated: Optional[datetime] = None
    kind: str = ""  # "atom", "rss", or "rdf"
    entries: list[FeedEntry] = field(default_factory=list)


def looks_like_feed(body: bytes, mime: str, *, require_strong: bool = False) -> bool:
    """Heuristic: does this look like an RSS/Atom/RDF feed?

    Checks MIME first, then sniffs the leading bytes for a feed root
    element. Generic application/xml + text/xml only qualify if the
    sniff confirms a feed root (so we don't hijack arbitrary XML).

    When `require_strong` is True (used for protocols that hand us an
    empty MIME and arbitrary text bodies, e.g. Gopher type-0 files — see
    issue #3), we demand an actual feed *root* element at the very start
    of the document rather than a loose substring match anywhere in the
    first 512 bytes. This prevents a plain text file that merely mentions
    "<feed>" or "<rss>" from being hijacked into the feed renderer.
    """
    base_mime = (mime or "").split(";")[0].strip().lower()

    if base_mime in ("application/rss+xml", "application/atom+xml",
                      "application/rdf+xml"):
        return True

    head = body[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()

    if require_strong:
        # Require the document to actually *begin* with an XML declaration
        # or a feed root element — a much stronger signal than substring
        # presence. We tolerate a leading <?xml ...?> prolog before the root.
        probe = head
        if probe.startswith(b"<?xml"):
            end = probe.find(b"?>")
            if end != -1:
                probe = probe[end + 2:].lstrip(b" \t\r\n")
        return (
            probe.startswith(b"<rss")
            or probe.startswith(b"<feed")
            or probe.startswith(b"<rdf:rdf")
        )

    sniffed = (
        b"<rss" in head
        or b"<feed" in head
        or b"<rdf:rdf" in head
        or (b"<?xml" in head and (b"rss" in head or b"atom" in head))
    )
    if base_mime in ("application/xml", "text/xml", ""):
        return sniffed
    return False


def _strip_ns(tag: str) -> str:
    """'{namespace}local' -> 'local'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _feed_text(elem: Optional[ET.Element]) -> str:
    """Flattened, unescaped, whitespace-normalised text of an element."""
    if elem is None:
        return ""
    raw = "".join(elem.itertext())
    raw = html.unescape(raw)  # handle CDATA-wrapped HTML entities
    return " ".join(raw.split())


def _parse_feed_date(raw: str) -> Optional[datetime]:
    """Parse RFC 822 (RSS) or RFC 3339/ISO 8601 (Atom) timestamps."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt
    except (TypeError, ValueError):
        pass
    iso = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _atom_link(entry: ET.Element) -> str:
    """Pick the best <link> from an Atom element (prefer rel=alternate)."""
    best = ""
    for link in entry.findall("atom:link", _NS):
        rel = link.get("rel", "alternate")
        href = link.get("href", "")
        if not href:
            continue
        if rel == "alternate":
            return href
        if not best and rel in ("", "self"):
            best = href
    return best


def parse_feed(body: bytes) -> Optional[Feed]:
    """Parse RSS 2.0, RSS 1.0 (RDF) or Atom bytes into a Feed.

    Returns None if the document is not a parseable feed. Raises
    ValueError on a hostile DOCTYPE (entity-expansion vector) or
    malformed XML.
    """
    head = body[:2048].lstrip(b"\xef\xbb\xbf \t\r\n")
    if b"<!DOCTYPE" in head[:512] or b"<!doctype" in head[:512]:
        raise ValueError("Refusing to parse feed with DOCTYPE declaration")

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"Malformed XML feed: {exc}") from exc

    root_tag = _strip_ns(root.tag).lower()

    if root_tag == "feed":
        return _parse_atom(root)
    if root_tag == "rss":
        return _parse_rss2(root)
    if root_tag == "rdf":  # RSS 1.0
        return _parse_rss1(root)
    return None


def _parse_atom(root: ET.Element) -> Feed:
    feed = Feed(kind="atom")
    feed.title = _feed_text(root.find("atom:title", _NS))
    feed.subtitle = _feed_text(root.find("atom:subtitle", _NS))
    feed.link = _atom_link(root)
    feed.updated = _parse_feed_date(_feed_text(root.find("atom:updated", _NS)))

    for entry in root.findall("atom:entry", _NS)[:_FEED_MAX_ENTRIES]:
        feed.entries.append(FeedEntry(
            title=_feed_text(entry.find("atom:title", _NS)) or "(untitled)",
            link=_atom_link(entry),
            updated=_parse_feed_date(
                _feed_text(entry.find("atom:updated", _NS))
                or _feed_text(entry.find("atom:published", _NS))
            ),
            summary=_feed_text(entry.find("atom:summary", _NS))
                    or _feed_text(entry.find("atom:content", _NS)),
        ))
    return feed


def _parse_rss2(root: ET.Element) -> Feed:
    channel = root.find("channel")
    feed = Feed(kind="rss")
    if channel is None:
        return feed
    feed.title = _feed_text(channel.find("title"))
    feed.subtitle = _feed_text(channel.find("description"))
    feed.link = _feed_text(channel.find("link"))
    feed.updated = _parse_feed_date(
        _feed_text(channel.find("lastBuildDate")) or _feed_text(channel.find("pubDate"))
    )

    for item in channel.findall("item")[:_FEED_MAX_ENTRIES]:
        feed.entries.append(FeedEntry(
            title=_feed_text(item.find("title")) or "(untitled)",
            link=_feed_text(item.find("link")),
            updated=_parse_feed_date(
                _feed_text(item.find("pubDate"))
                or _feed_text(item.find("dc:date", _NS))
            ),
            summary=_feed_text(item.find("description")),
        ))
    return feed


def _parse_rss1(root: ET.Element) -> Feed:
    feed = Feed(kind="rdf")
    channel = root.find("rss10:channel", _NS)
    if channel is not None:
        feed.title = _feed_text(channel.find("rss10:title", _NS))
        feed.subtitle = _feed_text(channel.find("rss10:description", _NS))
        feed.link = _feed_text(channel.find("rss10:link", _NS))
        feed.updated = _parse_feed_date(_feed_text(channel.find("dc:date", _NS)))

    for item in root.findall("rss10:item", _NS)[:_FEED_MAX_ENTRIES]:
        feed.entries.append(FeedEntry(
            title=_feed_text(item.find("rss10:title", _NS)) or "(untitled)",
            link=_feed_text(item.find("rss10:link", _NS)),
            updated=_parse_feed_date(_feed_text(item.find("dc:date", _NS))),
            summary=_feed_text(item.find("rss10:description", _NS)),
        ))
    return feed


def _fmt_feed_date(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


def _entry_sort_key(entry: FeedEntry):
    """Sort newest-first; entries without a date sort last."""
    if entry.updated is None:
        return (0, datetime.min.replace(tzinfo=timezone.utc))
    dt = entry.updated
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (1, dt)


def feed_to_gemtext(feed: Feed, base_url: str, summaries: bool = True) -> str:
    """Render a parsed Feed as gemtext.

    Reuses the existing gemtext renderer downstream, so feeds get
    link-numbering, find, save, pager etc. for free. Entry links are
    resolved against the feed URL so relative hrefs work.
    """
    out: list[str] = []
    kind_label = {"atom": "Atom", "rss": "RSS", "rdf": "RSS 1.0"}.get(feed.kind, "Feed")

    out.append(f"# {feed.title or '(untitled feed)'}")
    out.append("")
    meta_bits = [kind_label, f"{len(feed.entries)} entries"]
    if feed.updated:
        meta_bits.append(f"updated {_fmt_feed_date(feed.updated)}")
    out.append("> " + " · ".join(meta_bits))
    if feed.subtitle:
        out.append(f"> {feed.subtitle}")
    if feed.link:
        out.append(f"=> {resolve_url(base_url, feed.link)} Feed homepage")
    out.append("")

    entries = sorted(feed.entries, key=_entry_sort_key, reverse=True)
    if not entries:
        out.append("(This feed contains no entries.)")
        return "\n".join(out)

    for entry in entries:
        date = _fmt_feed_date(entry.updated)
        out.append(f"## {entry.title}")
        if entry.link:
            out.append(f"=> {resolve_url(base_url, entry.link)} Read entry")
        if date:
            out.append(date)
        if summaries and entry.summary:
            summary = entry.summary
            if len(summary) > _FEED_SUMMARY_MAX:
                summary = summary[:_FEED_SUMMARY_MAX - 1].rstrip() + "…"
            out.append(summary)
        out.append("")

    return "\n".join(out)


# ── Subscription Persistence ─────────────────────────────────────────────────
#
# Feeds are stored as a dict keyed by feed URL. Each record tracks a
# human title, the set of entry identities already seen, the last
# successful check time, and an `unread` count so the subscription
# picker badge survives across a check → subs sequence (issue #10).
# New-entry detection compares freshly-fetched entry identities against
# the stored "seen" set.

def _load_subscriptions() -> dict[str, dict]:
    data = _read_json(_FEEDS_FILE)
    if isinstance(data, dict):
        out: dict[str, dict] = {}
        for url, rec in data.items():
            if not isinstance(rec, dict):
                continue
            seen = rec.get("seen", [])
            out[str(url)] = {
                "title": str(rec.get("title", "")),
                "seen": [str(s) for s in seen] if isinstance(seen, list) else [],
                "last_checked": _try_int(str(rec.get("last_checked", 0)), 0),
                "unread": max(0, _try_int(str(rec.get("unread", 0)), 0)),
            }
        return out
    return {}


def _save_subscriptions(subs: dict[str, dict]) -> None:
    try:
        _atomic_write_json(_FEEDS_FILE, subs)
    except OSError as exc:
        print(yellow(f"  Warning: could not save subscriptions: {exc}"))


# ── URL Resolution ──────────────────────────────────────────────────────────

def resolve_url(base: str, link: str) -> str:
    """Resolve a relative link against the base URL using RFC 3986 rules."""
    resolved = urljoin(base, link)

    if "://" not in resolved and "://" in base:
        base_parts = urlparse(base)
        if not base_parts.scheme or not base_parts.netloc:
            return resolved
        if link.startswith("//"):
            return f"{base_parts.scheme}:{link}"
        if link.startswith("/"):
            return f"{base_parts.scheme}://{base_parts.netloc}{link}"
        base_path = base_parts.path or "/"
        base_dir = base_path.rsplit("/", 1)[0] if "/" in base_path else ""
        new_path = f"{base_dir}/{link}" if base_dir else f"/{link}"
        return f"{base_parts.scheme}://{base_parts.netloc}{new_path}"

    return resolved


def replace_query(url: str, new_query: str) -> str:
    """Replace the query component of a URL (preserves fragment)."""
    p = urlparse(url)
    return urlunparse(p._replace(query=new_query))


def _safe_filename_from_url(url: str, default: str = "page.dat") -> str:
    """Derive a safe local filename from a URL (no query/fragment)."""
    parts = urlparse(url)
    path = parts.path or ""
    candidate = path.rstrip("/").rsplit("/", 1)[-1]
    candidate = os.path.basename(candidate)
    if not candidate or candidate in (".", "..") or candidate.startswith("."):
        return default
    return candidate


# ── Gemtext Renderer ────────────────────────────────────────────────────────

SUPPORTED_SCHEMES = (
    "kepler://", "keplers://", "spartan://", "gemini://",
    "nex://", "gopher://", "gopher-search://", "finger://",
)


@dataclass
class PromptLink:
    """A Spartan `=:` input-prompt link.

    The user supplies text which is uploaded as the *request body* of the
    next request to `url` (NOT as a query string). `label` is the prompt
    shown to the user.
    """
    label: str
    url: str


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


@dataclass
class RenderResult:
    """Output of the gemtext renderer.

    `links` are ordinary navigation links (`=>`). `prompts` are Spartan
    `=:` input-prompt links. The two share a single user-facing numbering
    space so a user can type a number to follow a link *or* answer a
    prompt; `numbered` records, in order, which list each [n] refers to.
    """
    lines: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    prompts: list[PromptLink] = field(default_factory=list)
    # Sequence of ("link"|"prompt", index_into_that_list) in display order,
    # so selection number N maps to numbered[N-1].
    numbered: list[tuple[str, int]] = field(default_factory=list)


def render_gemtext(text: str, base_url: str) -> RenderResult:
    """Parse gemtext (with Spartan `=:` prompt support) into a RenderResult.

    Link types handled:
        =>  <url> [label]   ordinary navigation link
        =:  <url> [label]   Spartan input-prompt link (body upload)

    Both consume slots in a single shared numbering space so `open_link`
    can dispatch a typed number to either a navigation or a prompt.
    """
    result = RenderResult()
    preformat = False
    w = term_width()

    def _next_number() -> int:
        return len(result.numbered) + 1

    for raw_line in text.splitlines():
        if raw_line.startswith("```"):
            preformat = not preformat
            result.lines.append(
                dim("┌" + "─" * (w - 2) + "┐") if preformat
                else dim("└" + "─" * (w - 2) + "┘")
            )
            continue

        if preformat:
            result.lines.append(dim("│ ") + raw_line)
            continue

        # Spartan input-prompt line: `=: <url> [label]`
        # Checked explicitly and early so it is never mistaken for body text
        # or word-wrapped into oblivion (the original bug).
        if raw_line.startswith("=:"):
            rest = raw_line[2:].strip()
            tokens = rest.split(None, 1)
            if not tokens:
                continue
            raw_href = tokens[0]
            label = tokens[1] if len(tokens) > 1 else raw_href
            full_url = resolve_url(base_url, raw_href)
            n = _next_number()
            result.prompts.append(PromptLink(label=label, url=full_url))
            result.numbered.append(("prompt", len(result.prompts) - 1))

            # Spartan body-upload prompts only make sense for spartan:// URLs.
            is_spartan = full_url.startswith("spartan://")
            tag = bright_magenta(f"[{n}]")
            marker = bright_magenta("✎") if is_spartan else yellow("✎?")
            label_text = bright_magenta(label) if is_spartan else yellow(label)
            result.lines.append(f"  {tag} {marker} {label_text}  {dim('(input)')}")
            result.lines.append(f"       {dim(full_url)}")
            if not is_spartan:
                result.lines.append(
                    f"       {dim('(non-Spartan target — input upload may not apply)')}"
                )
            continue

        if raw_line.startswith("=>"):
            rest = raw_line[2:].strip()
            tokens = rest.split(None, 1)
            if not tokens:
                continue
            raw_href = tokens[0]
            label = tokens[1] if len(tokens) > 1 else raw_href
            full_url = resolve_url(base_url, raw_href)
            n = _next_number()
            result.links.append((label, full_url))
            result.numbered.append(("link", len(result.links) - 1))

            is_supported = any(full_url.startswith(s) for s in SUPPORTED_SCHEMES)
            url_text = bright_blue(label) if is_supported else yellow(label)
            result.lines.append(f"  {bright_cyan(f'[{n}]')} {cyan('→')} {url_text}")
            result.lines.append(f"       {dim(full_url)}")
            continue

        if raw_line.startswith("###"):
            h = raw_line[3:].strip()
            result.lines += ["", green(f"  ▸▸▸ {h}"), ""]
            continue
        if raw_line.startswith("##"):
            h = raw_line[2:].strip()
            result.lines += ["", bold(bright_green(f"  ▸▸ {h}")), ""]
            continue
        if raw_line.startswith("#"):
            h = raw_line[1:].strip()
            result.lines += [
                "",
                bold(bright_yellow(f"  ▸ {h}")),
                dim("  " + "═" * min(len(h) + 4, w - 4)),
                "",
            ]
            continue

        if raw_line.startswith("* "):
            result.lines.append(f"  {cyan('•')} {raw_line[2:]}")
            continue

        if raw_line.startswith(">"):
            result.lines.append(f"  {dim('│')} {dim(raw_line[1:].strip())}")
            continue

        if not raw_line:
            result.lines.append("")
            continue

        if len(raw_line) > w - 4:
            result.lines.extend(_wrap_line(raw_line, w))
        else:
            result.lines.append("  " + raw_line)

    return result


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
            }.get(ch2, '')
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
            # Distinguish a bare ESC (no following byte within the window)
            # from a CSI/SS3 escape sequence. If nothing is immediately
            # available, treat it as a standalone ESC so a lone ESC keypress
            # is never merged with subsequently-typed input (issue #9).
            if not select.select([sys.stdin], [], [], 0.05)[0]:
                return '\x1b'
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


def _subscription_mode(
    subs: dict[str, dict],
    new_counts: Optional[dict[str, int]] = None,
) -> Optional[str]:
    """Interactive subscription picker. Returns the selected feed URL.

    The unread badge prefers an explicit `new_counts` mapping (a freshly
    completed check), and otherwise falls back to the persisted per-feed
    `unread` count so the badge survives a `check` → `subs` sequence even
    when the caller doesn't thread counts through (issue #10).
    """
    items = list(subs.items())
    if not items:
        print(yellow("  No feed subscriptions yet."))
        return None
    new_counts = new_counts or {}

    def render(i: int, item, num_width: int, is_cursor: bool) -> list[str]:
        url, rec = item
        w = term_width()
        title = rec.get("title") or url
        name_max = max(20, (w // 2) - 10)
        url_max = max(20, (w // 2) - 10)
        title_disp = title if len(title) <= name_max else title[:name_max - 1] + "…"
        url_disp = url if len(url) <= url_max else url[:url_max - 1] + "…"
        num = str(i + 1).rjust(num_width)
        n_new = new_counts.get(url, rec.get("unread", 0))
        badge = bright_green(f" ●{n_new} new") if n_new else ""
        if is_cursor:
            return [f"  {bright_cyan('▶')} {bright_cyan(num)}  "
                    f"{bright_white(title_disp.ljust(name_max))}  {dim(url_disp)}{badge}"]
        return [f"    {dim(num)}  {title_disp.ljust(name_max)}  {dim(url_disp)}{badge}"]

    idx = _interactive_picker(items, "Feed Subscriptions", render)
    return items[idx][0] if idx is not None else None


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
        'input',
        'history', 'hist',
        'delh', 'clearhistory', 'clearhist',
        'bookmark', 'bm', 'mark',
        'bookmarks', 'bms', 'marks',
        'open', 'ob', 'delbm', 'rmbm',
        'subscribe', 'sub', 'unsubscribe', 'unsub',
        'subscriptions', 'subs', 'feeds', 'check',
        'save', 'set', 'clear',
        'help', '?', 'h',
        'quit', 'q', 'exit', 'bye',
    ]
    NAV_CMDS = {'go', 'visit', 'navigate', 'g', 'open', 'ob', 'input'}
    DELBM_CMDS = {'delbm', 'rmbm'}
    UNSUB_CMDS = {'unsubscribe', 'unsub'}

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
                    matches = []
                elif cmd in self.NAV_CMDS:
                    candidates = (
                        list(self.browser.bookmarks.values())
                        + self.browser.history[-50:]
                    )
                    matches = [c for c in candidates if c.startswith(text)]
                elif cmd in self.DELBM_CMDS:
                    matches = [c for c in self.browser.bookmarks if c.startswith(text)]
                elif cmd in self.UNSUB_CMDS:
                    matches = [c for c in self.browser.subscriptions if c.startswith(text)]
                elif cmd == 'set':
                    options = ['pager', 'home', 'timeout', 'color',
                               'history_limit', 'feed_compact']
                    matches = [c for c in options if c.startswith(text)]
                else:
                    matches = []

            self.matches = matches

        return self.matches[state] if state < len(self.matches) else None


# ── Response Handler Result Type ────────────────────────────────────────────

@dataclass
class HandlerResult:
    """Result of handling a protocol response."""
    done: bool = True
    redirect_to: Optional[str] = None
    new_url: Optional[str] = None
    reset_redirects: bool = False
    data: bytes = b""
    # Set True by handlers that issued an interactive input prompt, so the
    # fetch loop can apply an independent interaction-cycle ceiling even
    # though such results reset the redirect counter (issue #1).
    #
    # NOTE: Spartan never sets this — Spartan has no server-driven input
    # status code; its input is purely client-side via `=:` content lines.
    is_input_cycle: bool = False


# ── Browser Class ───────────────────────────────────────────────────────────

class Browser:
    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or _load_config()
        set_use_color(self.config.color)

        self.history: list[str] = _load_history(self.config.history_limit)
        self.hist_pos: int = len(self.history) - 1
        self.current_url: Optional[str] = None
        self.current_links: list[tuple[str, str]] = []
        self.current_prompts: list[PromptLink] = []
        # Display-order map: numbered[n-1] == ("link"|"prompt", index).
        self.current_numbered: list[tuple[str, int]] = []
        self.current_render_lines: list[str] = []
        self.last_body: Optional[bytes] = None
        self.last_mime: Optional[str] = None
        self.bookmarks: dict[str, str] = _load_bookmarks()
        self.known_hosts: dict[str, str] = _load_known_hosts()
        self.subscriptions: dict[str, dict] = _load_subscriptions()
        self.history_save_counter: int = 0
        self.pager_enabled: bool = self.config.pager
        self.feed_compact: bool = self.config.feed_compact

        # Tracks whether the currently displayed page is a feed, and its URL,
        # so `subscribe` with no arg can subscribe to the current feed.
        self.current_is_feed: bool = False

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

    def _truncate_history_in_memory(self, limit: int) -> None:
        """Trim in-memory history to `limit`, keeping hist_pos consistent."""
        if limit < 0:
            return
        if len(self.history) > limit:
            drop = len(self.history) - limit
            self.history = self.history[drop:]
            self.hist_pos = max(-1, self.hist_pos - drop)

    def close(self) -> None:
        """Flush state to disk."""
        self.flush_history()

    def _reset_page_state(self) -> None:
        """Clear per-page link/prompt/render state."""
        self.current_links = []
        self.current_prompts = []
        self.current_numbered = []
        self.current_render_lines = []

    def _apply_render_result(self, result: RenderResult) -> None:
        """Store a RenderResult's link/prompt/numbering state on the browser."""
        self.current_links = result.links
        self.current_prompts = result.prompts
        self.current_numbered = result.numbered
        self.current_render_lines = result.lines

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

    def _fetch_raw(self, url: str) -> tuple[int, str, bytes, dict]:
        """Fetch a URL once (no redirect/input handling). Used by the feed
        checker so it doesn't disturb browsing state or prompt interactively.
        Follows a single redirect best-effort for convenience."""
        scheme = urlparse(url).scheme
        code, meta, body, extras = self._fetch_url(url)
        # Best-effort single redirect follow for feeds (3x across protocols).
        if scheme in ("gemini", "spartan", "kepler", "keplers") and 30 <= code < 40 and meta.strip():
            target = resolve_url(url, meta.strip())
            code, meta, body, extras = self._fetch_url(target)
        return code, meta, body, extras

    # ── Public Navigation ───────────────────────────────────────────────────

    def navigate(self, url: str, push_history: bool = True, data: bytes = b"") -> None:
        self._fetch(url, data=data, push_history=push_history)

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
        # netloc preserves any non-default port, so this correctly retains it.
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
        """Follow a numbered selection — either a navigation link or a
        Spartan `=:` input prompt — using the shared numbering space."""
        try:
            idx = int(raw) - 1
        except ValueError:
            print(red(f"  Not a valid selection number: {raw!r}"))
            return

        if not (0 <= idx < len(self.current_numbered)):
            total = len(self.current_numbered)
            print(red(
                f"  Selection [{raw}] doesn't exist — "
                f"page has {total} selectable item(s)."
            ))
            return

        kind, list_idx = self.current_numbered[idx]

        if kind == "prompt":
            self._follow_prompt(self.current_prompts[list_idx])
            return

        label, url = self.current_links[list_idx]

        if url.startswith("gopher-search://"):
            self._handle_gopher_search(url, label)
            return

        if any(url.startswith(s) for s in SUPPORTED_SCHEMES):
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

    def _follow_prompt(self, prompt: PromptLink) -> None:
        """Gather text for a Spartan `=:` prompt and upload it as the request
        BODY of a request to the prompt's target URL.

        Per the Spartan model, input is delivered as the request body, not as
        a query string. We therefore call navigate() with explicit `data`.
        """
        if not prompt.url.startswith("spartan://"):
            # `=:` is a Spartan construct. If a non-Spartan target somehow
            # appears, refuse to body-upload (the other protocols use query
            # strings / their own input flows) and just navigate.
            print(yellow(
                "  ⚠  Input prompt targets a non-Spartan URL; "
                "following as an ordinary link."
            ))
            if any(prompt.url.startswith(s) for s in SUPPORTED_SCHEMES):
                self.navigate(prompt.url)
            else:
                print(f"  {dim(prompt.url)}")
            return

        print(yellow(f"\n  ✎  {bold(prompt.label)}"))
        try:
            text = input(cyan("  ❯ "))
        except (EOFError, KeyboardInterrupt):
            print()
            return

        # Empty input still uploads an empty body — that is a legitimate
        # Spartan request (the echo endpoint, for instance, will echo back
        # nothing). We allow it but confirm first.
        if text == "":
            if not self._confirm("  Send empty input? [y/N]: "):
                print(dim("  Cancelled."))
                return

        payload = text.replace("\r\n", "\n").encode("utf-8")
        self.navigate(prompt.url, data=payload)

    def input_upload(self, arg: str, arg2: str = "") -> None:
        """Directly upload a body to a Spartan URL (a manual `=:` prompt).

        Usage:
            input <spartan-url>            → prompt for the body interactively
            input <spartan-url> <text…>    → upload <text…> as the body
        """
        url = arg.strip()
        if not url:
            print(yellow("  Usage: input <spartan-url> [text]"))
            return
        if "://" not in url:
            url = f"spartan://{url}"
        if not url.startswith("spartan://"):
            print(yellow(
                "  The 'input' command uploads a request body, which is a "
                "Spartan-only construct.\n"
                "  For Gemini/Kepler input, just open the URL and answer the "
                "server's prompt."
            ))
            return
        # Reuse the prompt machinery. If inline text was given, skip the prompt.
        if arg2:
            payload = arg2.replace("\r\n", "\n").encode("utf-8")
            self.navigate(url, data=payload)
        else:
            self._follow_prompt(PromptLink(label=f"Input for {url}", url=url))

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
            # fetch_gopher strips CR/LF from the query internally (issue #7).
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
        if _interactive_ui_available():
            result = _bookmark_mode(self.bookmarks, self.current_url)
            if result:
                name, url = result
                print(dim(f"  Opening {name}…"))
                self.navigate(url)
        else:
            self.show_bookmarks()

    # ── Feed Subscriptions ──────────────────────────────────────────────────

    def subscribe(self, url_arg: str = "") -> None:
        """Subscribe to a feed. With no arg, subscribes to the current page
        if it is a feed. Records existing entries as 'seen' so the first
        `check` only reports genuinely new items."""
        url = url_arg.strip()
        if not url:
            if self.current_is_feed and self.current_url:
                url = self.current_url
            else:
                print(yellow("  Usage: subscribe <feed-url>   "
                             "(or run with no arg while viewing a feed)"))
                return
        else:
            try:
                url = normalise_url(url)
            except ValueError as e:
                print(yellow(f"  {e}"))
                return

        if url in self.subscriptions:
            print(yellow(f"  Already subscribed to {url}"))
            return

        print(dim(f"  ⟳  Fetching feed {url} …"))
        try:
            code, meta, body, _ = self._fetch_raw(url)
        except (OSError, ValueError, ssl.SSLError) as exc:
            print(bright_red(f"  ✗  Could not fetch feed: {exc}"))
            return

        if not (code == 2 or (20 <= code < 30)):
            print(bright_red(f"  ✗  Feed fetch failed (status {code}): {meta}"))
            return

        try:
            feed = parse_feed(body)
        except ValueError as exc:
            print(bright_red(f"  ✗  Not a parseable feed: {exc}"))
            return
        if feed is None:
            print(bright_red("  ✗  That URL does not appear to be an RSS/Atom feed."))
            return

        seen = [e.identity() for e in feed.entries]
        self.subscriptions[url] = {
            "title": feed.title or url,
            "seen": seen,
            "last_checked": int(time.time()),
            "unread": 0,
        }
        _save_subscriptions(self.subscriptions)
        print(green(f"  ✓  Subscribed to {bold(feed.title or url)} "
                    f"({len(feed.entries)} entries)"))

    def unsubscribe(self, identifier: str) -> None:
        identifier = identifier.strip()
        if not identifier:
            print(yellow("  Usage: unsubscribe <n|url>"))
            return
        # Numeric index into the subscription list?
        try:
            idx = int(identifier) - 1
            urls = list(self.subscriptions.keys())
            if 0 <= idx < len(urls):
                url = urls[idx]
                title = self.subscriptions[url].get("title", url)
                del self.subscriptions[url]
                _save_subscriptions(self.subscriptions)
                print(green(f"  ✓  Unsubscribed from {title}"))
                return
            print(red(f"  No subscription [{identifier}] — "
                      f"valid range is 1–{len(urls)}."))
            return
        except ValueError:
            pass
        if identifier in self.subscriptions:
            title = self.subscriptions[identifier].get("title", identifier)
            del self.subscriptions[identifier]
            _save_subscriptions(self.subscriptions)
            print(green(f"  ✓  Unsubscribed from {title}"))
        else:
            print(red(f"  Not subscribed to {identifier!r}"))

    def show_subscriptions(self, new_counts: Optional[dict[str, int]] = None) -> None:
        if not self.subscriptions:
            print(yellow("  No feed subscriptions yet. Use 'subscribe <url>'."))
            return
        if _interactive_ui_available():
            url = _subscription_mode(self.subscriptions, new_counts)
            if url:
                print(dim(f"  Opening {url}…"))
                self.navigate(url)
            return
        new_counts = new_counts or {}
        print()
        print(bold("  Feed subscriptions:"))
        print()
        for i, (url, rec) in enumerate(self.subscriptions.items(), 1):
            title = rec.get("title") or url
            # Prefer freshly-supplied counts, fall back to persisted unread.
            n_new = new_counts.get(url, rec.get("unread", 0))
            badge = bright_green(f"  ●{n_new} new") if n_new else ""
            print(f"  {bright_cyan(f'[{i}]')} {bold(title)}{badge}")
            print(f"       {dim(url)}")
        print()

    def check_feeds(self) -> None:
        """Re-fetch every subscribed feed and report new entries since the
        last check. Updates each feed's 'seen' set, 'unread' count, and
        'last_checked' time."""
        if not self.subscriptions:
            print(yellow("  No feed subscriptions to check. Use 'subscribe <url>'."))
            return

        new_counts: dict[str, int] = {}
        all_new: list[tuple[str, FeedEntry]] = []  # (feed_title, entry)
        total = len(self.subscriptions)

        print(bold(f"\n  Checking {total} feed(s)…\n"))

        for url, rec in list(self.subscriptions.items()):
            title = rec.get("title") or url
            print(dim(f"  ⟳  {title}"), end="", flush=True)
            try:
                code, meta, body, _ = self._fetch_raw(url)
            except (OSError, ValueError, ssl.SSLError) as exc:
                print(f"\r{yellow(f'  ⚠  {title}: {exc}')}{_clear_eol()}")
                continue

            if not (code == 2 or (20 <= code < 30)):
                print(f"\r{yellow(f'  ⚠  {title}: status {code} {meta}')}{_clear_eol()}")
                continue

            try:
                feed = parse_feed(body)
            except ValueError as exc:
                print(f"\r{yellow(f'  ⚠  {title}: {exc}')}{_clear_eol()}")
                continue
            if feed is None:
                print(f"\r{yellow(f'  ⚠  {title}: not a parseable feed')}{_clear_eol()}")
                continue

            seen_set = set(rec.get("seen", []))
            fresh = [e for e in feed.entries if e.identity() not in seen_set]
            fresh.sort(key=_entry_sort_key, reverse=True)

            n_new = len(fresh)
            new_counts[url] = n_new
            for entry in fresh:
                all_new.append((feed.title or title, entry))

            # Update stored state: title may have changed; merge seen ids.
            # Persist `unread` so the subscription picker badge survives a
            # later `subs` invocation that doesn't thread counts (issue #10).
            prev_unread = max(0, _try_int(str(rec.get("unread", 0)), 0))
            merged_seen = list(seen_set | {e.identity() for e in feed.entries})
            self.subscriptions[url] = {
                "title": feed.title or title,
                "seen": merged_seen[-(_FEED_MAX_ENTRIES * 4):],  # bound growth
                "last_checked": int(time.time()),
                "unread": prev_unread + n_new,
            }

            badge = bright_green(f"{n_new} new") if n_new else dim("up to date")
            print(f"\r  {green('✓')}  {title}  [{badge}]{_clear_eol()}")

        _save_subscriptions(self.subscriptions)

        total_new = sum(new_counts.values())
        print()
        print(hr())
        if total_new == 0:
            print(green("\n  ✓  All feeds up to date — no new entries.\n"))
            return

        # Render the aggregated new entries as a gemtext "river" so the
        # user can open any of them by number.
        all_new.sort(key=lambda fe: _entry_sort_key(fe[1]), reverse=True)
        shown = all_new[:max(_FEED_NEW_ENTRIES_SHOWN, total_new)]

        out: list[str] = [f"# {total_new} new feed entr"
                          f"{'y' if total_new == 1 else 'ies'}", ""]
        last_feed = None
        for feed_title, entry in shown:
            if feed_title != last_feed:
                out.append(f"## {feed_title}")
                last_feed = feed_title
            date = _fmt_feed_date(entry.updated)
            label = entry.title or "(untitled)"
            if entry.link:
                suffix = f"  —  {date}" if date else ""
                out.append(f"=> {entry.link} {label}{suffix}")
            else:
                out.append(f"* {label}{('  —  ' + date) if date else ''}")
            out.append("")

        # Render as a synthetic page so links are numbered and openable.
        base = next(iter(self.subscriptions), "gemini://localhost/")
        gemtext = "\n".join(out)
        result = render_gemtext(gemtext, base)
        self.current_url = None
        self.current_is_feed = False
        self.last_body = gemtext.encode("utf-8")
        self.last_mime = "text/gemini"
        self._apply_render_result(result)

        print()
        self._emit_lines(result.lines)
        print()
        print(hr())
        n_links = len(result.links)
        print(dim(
            f"\n  {n_links} new entr{'y' if n_links == 1 else 'ies'} — "
            f"type a number to open, or 'subs' for the feed list.\n"
        ))

    # ── Display Helpers ─────────────────────────────────────────────────────

    def show_links(self, pattern: str = "") -> None:
        if not self.current_numbered:
            print(yellow("  No links on this page."))
            return
        print()
        print(bold("  Selectable items on this page:"))
        print()
        for n, (kind, list_idx) in enumerate(self.current_numbered, 1):
            if kind == "prompt":
                p = self.current_prompts[list_idx]
                label, url = p.label, p.url
                tag = bright_magenta(f"[{n}]")
                kind_note = dim(" (input)")
            else:
                label, url = self.current_links[list_idx]
                tag = bright_cyan(f"[{n}]")
                kind_note = ""
            if pattern:
                pat_lower = pattern.lower()
                if not (pat_lower in label.lower() or pat_lower in url.lower()):
                    continue
            print(f"  {tag} {label}{kind_note}")
            print(f"       {dim(url)}")
        print()

    def show_history(self) -> None:
        if not self.history:
            print(yellow("  History is empty."))
            return
        if _interactive_ui_available():
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
                filename = _safe_filename_from_url(self.current_url)
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
            print(f"  {cyan(cmd.ljust(28))} {desc}")

        print(bold("  Navigation"))
        row("go <url>",            "Navigate to any supported URL")
        row("<number>",            "Follow a link or answer an input prompt")
        row("back  /  b",          "Go back in history")
        row("forward  /  f",       "Go forward in history")
        row("up  /  ..",           "Go up one directory level")
        row("reload  /  r",        "Reload the current page")
        row("home",                f"Go to {self.HOME}")
        row("finger <user@host>",  "Finger a user")
        print()
        print(bold("  Page"))
        row("links  /  l",         "List links and input prompts")
        row("find <term>  /  /",   "Search page")
        row("source",              "View raw source")
        row("save [file]",         "Save current page")
        row("url",                 "Show current URL")
        print()
        print(bold("  Spartan input"))
        row("input <url> [text]",  "Upload a body to a Spartan URL")
        print(dim("  A `=:` line in Spartan content is also an input prompt, shown"))
        print(dim("  as a magenta [n] ✎ item; selecting it asks for text which is"))
        print(dim("  uploaded as the request BODY. Spartan has no input STATUS"))
        print(dim("  code — input is purely client-side."))
        print()
        print(bold("  Feeds (RSS / Atom)"))
        row("subscribe [url]",     "Subscribe to a feed (or current page)")
        row("unsubscribe <n|url>", "Remove a subscription")
        row("subscriptions / subs","List feed subscriptions")
        row("check",               "Check all feeds for new entries")
        print(dim("  (Feeds are auto-detected and rendered when you open them.)"))
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
        row("set feed_compact on|off", "Hide/show feed entry summaries")
        row("set home <url>",      "Set home page")
        row("set timeout <secs>",  "Set connection timeout")
        row("set history_limit <n>", "Set max history entries")
        print()
        print(bold("  Security note"))
        print(dim("  TLS uses Trust-On-First-Use certificate pinning (per host:port)."))
        print(dim("  A notice is printed whenever a new certificate is pinned."))
        print(dim("  Certificate chain and EXPIRY are NOT verified; only the"))
        print(dim("  fingerprint is pinned. This matches smolnet conventions but"))
        print(dim("  means an expired-yet-unchanged cert is accepted silently."))
        print(dim("  Feeds are XML: DOCTYPE declarations are rejected and bodies"))
        print(dim("  are size-capped to mitigate XML-bomb / XXE attacks."))
        print()
        print(bold("  Input"))
        print(dim("  Bare-hostname input (e.g. 'example.com') defaults to the"))
        print(dim("  scheme of your home page; use a full URL to force another."))
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

        elif option == "feed_compact":
            if value in bool_on:
                self.feed_compact = True
                self.config.feed_compact = True
            elif value in bool_off:
                self.feed_compact = False
                self.config.feed_compact = False
            else:
                print(yellow("  Usage: set feed_compact on|off"))
                return
            _save_config(self.config)
            print(green(f"  ✓  Feed compact mode "
                        f"{'on (summaries hidden)' if self.feed_compact else 'off (summaries shown)'}"))

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
                    self._truncate_history_in_memory(limit)
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
        """Handle a Spartan response.

        Spartan has exactly four status codes and NO input-required code:
            2  success
            3  redirect
            4  client error
            5  server error
        Interactive input is NOT server-driven — it is delivered client-side
        as a request body via `=:` content lines (see _follow_prompt).
        """
        if code == 2:
            mime = meta.split(";")[0].strip().lower() if meta else "text/gemini"
            self._handle_success(current_url, body, mime, push_history)
            return HandlerResult(done=True)
        if code == 3:
            target = meta.strip()
            if not target:
                print(bright_red("\n  ✗  Empty redirect target\n"))
                return HandlerResult(done=True)
            redirect_url = resolve_url(current_url, target)
            print(yellow(f"  ⟶  Redirect → {redirect_url}"))
            return HandlerResult(done=False, redirect_to=redirect_url)
        if code == 4:
            print(bright_red(f"\n  ✗  Client error ({code}): {meta}\n"))
            return HandlerResult(done=True)
        if code == 5:
            print(bright_red(f"\n  ✗  Server error ({code}): {meta}\n"))
            return HandlerResult(done=True)
        print(bright_red(f"\n  ✗  Unknown Spartan code {code}: {meta}\n"))
        return HandlerResult(done=True)

    def _handle_kepler(self, code, meta, body, extras, current_url, push_history):
        scheme = urlparse(current_url).scheme

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
                is_input_cycle=True,
            )

        if 20 <= code < 30:
            mime_part = meta.split(";")[0].strip().lower() if meta else "text/gemini"
            expires = extras.get("expires", -1)
            if expires > 0 and expires < int(time.time()):
                print(yellow("  ⚠  Document is stale (server-reported expiry is in the past)"))
            self._handle_success(current_url, body, mime_part, push_history)
            return HandlerResult(done=True)

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
                done=False, redirect_to=new_url, new_url=new_url,
                reset_redirects=True, is_input_cycle=True,
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

        `data` is an optional request body. For Spartan it is the upload
        body (e.g. text gathered from a `=:` prompt or the `input` command);
        for other protocols it is generally unused (input is delivered via
        query strings).
        """
        current_url = url
        current_data = data
        redirect_depth = 0
        input_cycles = 0
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

            # Independent ceiling on interactive (Kepler/Gemini) input cycles.
            # Input responses reset the redirect counter (so a legitimate form
            # can submit and then be redirected freely), so without this a
            # hostile server could trap the user in an endless prompt loop
            # (issue #1). Spartan never triggers this path.
            if input_cycles > _MAX_INPUT_CYCLES:
                print(bright_red(
                    f"  ✗  Too many input cycles (>{_MAX_INPUT_CYCLES}), aborting.\n"
                ))
                return

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

            current_data = result.data

            if result.is_input_cycle:
                # Count input cycles independently and reset the redirect
                # counter / visited set so a fresh form submission isn't
                # falsely flagged as a redirect loop.
                input_cycles += 1
                redirect_depth = 0
                visited.clear()
            elif result.reset_redirects:
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
            # Bound in-memory history growth during a long session; on-disk
            # saving already slices to the limit, but the live list could
            # otherwise grow without bound (issue #5).
            self._truncate_history_in_memory(self.config.history_limit)
            self.hist_pos = len(self.history) - 1

        self.current_url = url
        self.last_body = body
        self.last_mime = mime
        self.current_is_feed = False
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
            self._reset_page_state()
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

        # Feed detection (RSS / Atom / RDF). Render recognised feeds as
        # gemtext so they inherit link numbering, find, save, and pager.
        if looks_like_feed(body, mime):
            if self._render_feed(body, url):
                return
            # Parsing failed — fall through to normal rendering.

        text: Optional[str]
        try:
            text = body.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            text = None

        if text is not None and ("text/gemini" in mime or mime == ""):
            result = render_gemtext(text, url)
            self._apply_render_result(result)
        elif text is not None and "text/" in mime:
            self._reset_page_state()
            self.current_render_lines = render_plain(text)
        elif text is not None and _looks_like_text(body):
            # Body is labelled binary (e.g. application/octet-stream from an
            # echo endpoint) but is actually printable text — render it as
            # plain text rather than offering a file download.
            print(dim(f"  (rendering {mime or 'binary'} as text — it looks textual)"))
            self._reset_page_state()
            self.current_render_lines = render_plain(text)
        else:
            self._handle_binary(body, url, mime)
            return

        self._emit_lines(self.current_render_lines)
        print()
        print(hr())
        self._print_selection_hint()
        print()

    def _print_selection_hint(self) -> None:
        """Print a footer hint describing how many links / prompts exist."""
        n_links = len(self.current_links)
        n_prompts = len(self.current_prompts)
        if n_links == 0 and n_prompts == 0:
            return
        bits = []
        if n_links:
            bits.append(f"{n_links} link(s)")
        if n_prompts:
            bits.append(bright_magenta(f"{n_prompts} input prompt(s)"))
        joined = " + ".join(bits)
        print(dim(
            f"\n  {joined} on page — "
            f"type a number to follow/answer, or 'links' for the full list."
        ))

    def _render_feed(self, body: bytes, url: str) -> bool:
        """Render a feed as gemtext. Returns True on success, False to let
        the caller fall back to normal rendering."""
        try:
            feed = parse_feed(body)
        except ValueError as exc:
            print(yellow(f"  ⚠  Could not parse feed ({exc}); showing raw document."))
            return False
        if feed is None:
            return False

        gemtext = feed_to_gemtext(feed, url, summaries=not self.feed_compact)
        result = render_gemtext(gemtext, url)
        self._apply_render_result(result)
        self.current_is_feed = True

        self._emit_lines(result.lines)
        print()
        print(hr())

        subscribed = url in self.subscriptions
        sub_hint = (
            "already subscribed — use 'check' for new entries"
            if subscribed
            else "use 'subscribe' to follow this feed"
        )
        print(dim(
            f"\n  Feed: {len(feed.entries)} entries, "
            f"{len(self.current_links)} link(s) — "
            f"type a number to open. {sub_hint}.\n"
        ))
        return True

    def _render_gopher(self, body: bytes, url: str) -> None:
        if is_gopher_menu(body):
            try:
                gemtext = gopher_menu_to_gemtext(body, url)
                result = render_gemtext(gemtext, url)
                self._apply_render_result(result)
            except (ValueError, UnicodeDecodeError) as e:
                print(bright_red(f"  ✗  Error parsing Gopher menu: {e}"))
                self._reset_page_state()
                self.current_render_lines = render_plain(
                    body.decode("utf-8", errors="replace")
                )
        else:
            # A Gopher text file could still be a feed (type 0 .xml selector),
            # but Gopher hands us no MIME, so we require a *strong* feed signal
            # — an actual feed root element at the start of the document —
            # rather than a loose substring match, so a plain text file that
            # merely mentions "<feed>" isn't hijacked (issue #3).
            if looks_like_feed(body, "", require_strong=True):
                if self._render_feed(body, url):
                    return
            try:
                text = body.decode("utf-8", errors="replace")
                self._reset_page_state()
                self.current_render_lines = render_plain(text)
            except UnicodeDecodeError:
                self._handle_binary(body, url, "application/octet-stream")
                return

        self._emit_lines(self.current_render_lines)
        print()
        print(hr())
        self._print_selection_hint()
        print()

    def _emit_lines(self, lines: list[str]) -> None:
        if self.pager_enabled:
            page_content(lines)
        else:
            for line in lines:
                print(line)

    def _handle_binary(self, body: bytes, url: str, mime: str) -> None:
        self._reset_page_state()
        print(yellow(f"  Binary content ({mime or 'unknown'}), {len(body):,} bytes"))
        fname = _safe_filename_from_url(url, default="download")
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
            markers = ""
            if self.current_is_feed:
                markers += bright_green(" ⊚")
            if self.current_prompts:
                markers += bright_magenta(" ✎")
            return f"{scheme_display}:{bright_cyan(display)}{markers} {hist} {bright_cyan('❯')} "
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
  ║                                       finger · feeds ║
  ║  multi-protocol browser — type help or ? to start    ║
  ╚══════════════════════════════════════════════════════╝
"""

# Map a bare-hostname's resolved default scheme. We derive the default
# scheme from the user's configured home page rather than hard-coding
# spartan, so typing a bare hostname is consistent with the user's
# preferred protocol (issue #12).
_VALID_BARE_SCHEMES = (
    "kepler", "keplers", "spartan", "gemini", "nex", "gopher",
)
_DEFAULT_BARE_SCHEME = "spartan"


def _default_bare_scheme(home: Optional[str] = None) -> str:
    """Pick the scheme to apply to a bare hostname.

    Honours the configured home page's scheme when it is a network scheme
    we can reasonably default to; otherwise falls back to spartan.
    """
    if home:
        scheme = urlparse(home).scheme
        if scheme in _VALID_BARE_SCHEMES:
            return scheme
    return _DEFAULT_BARE_SCHEME


def normalise_url(raw: str, *, default_scheme: Optional[str] = None) -> str:
    """Add a default scheme if the input has none.

    A bare hostname (no scheme) is mapped to `default_scheme` (which the
    caller derives from the configured home page — issue #12). When the
    caller passes nothing, we fall back to the module default so this
    function remains usable standalone.
    """
    if "://" in raw:
        return raw
    if "@" in raw:
        user, _, host = raw.partition("@")
        return f"finger://{host}/{user}"
    if raw == "localhost" or ":" in raw or "." in raw:
        scheme = default_scheme or _DEFAULT_BARE_SCHEME
        return f"{scheme}://" + raw
    raise ValueError("Ambiguous input: use full URL or user@host for finger")


# ── REPL Command Dispatch ───────────────────────────────────────────────────

def _make_command_table(browser: Browser) -> dict[str, Callable[[str, str], None]]:
    """Build the REPL command dispatch table."""

    def _norm(raw: str) -> str:
        # Thread the home-derived default scheme through every normalise call
        # made from the REPL so bare hostnames honour the user's preference.
        return normalise_url(raw, default_scheme=_default_bare_scheme(browser.HOME))

    def cmd_go(arg, arg2):
        if not arg:
            print(yellow("  Usage: go <url>"))
            return
        try:
            browser.navigate(_norm(arg))
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
            print(dim("  Options: pager, color, feed_compact, home, timeout, history_limit"))
        else:
            browser.set_option(arg, arg2)

    def cmd_open(arg, arg2):
        if not arg:
            browser.bookmark_picker()
        else:
            browser.bookmark_open(arg)

    def cmd_subscribe(arg, arg2):
        browser.subscribe(arg)

    def cmd_unsubscribe(arg, arg2):
        if not arg:
            print(yellow("  Usage: unsubscribe <n|url>"))
        else:
            browser.unsubscribe(arg)

    def cmd_input(arg, arg2):
        browser.input_upload(arg, arg2)

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
    register(("input", "=:"), cmd_input)
    register(("history", "hist"), lambda a, b: browser.show_history())
    register(("delh",), cmd_delh)
    register(("clearhistory", "clearhist"), lambda a, b: browser.history_clear())
    register(("bookmark", "bm", "mark"), lambda a, b: browser.bookmark_add(a))
    register(("bookmarks", "bms", "marks"), lambda a, b: browser.bookmark_picker())
    register(("open", "ob"), cmd_open)
    register(("delbm", "rmbm"), cmd_delbm)
    register(("subscribe", "sub"), cmd_subscribe)
    register(("unsubscribe", "unsub"), cmd_unsubscribe)
    register(("subscriptions", "subs", "feeds"), lambda a, b: browser.show_subscriptions())
    register(("check",), lambda a, b: browser.check_feeds())
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

    def _norm(raw: str) -> str:
        return normalise_url(raw, default_scheme=_default_bare_scheme(browser.HOME))

    print(bright_cyan(BANNER))

    if start_url:
        try:
            browser.navigate(_norm(start_url))
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
            # Friendly hint when the user pastes raw gemtext syntax (=> or =:)
            # at the command prompt instead of a command. Without this they
            # got a cryptic "Unsupported scheme: ''" from normalise_url.
            stripped = raw.lstrip()
            if stripped.startswith("=>") or stripped.startswith("=:"):
                print(yellow(
                    "  That looks like gemtext page syntax, not a command."
                ))
                if stripped.startswith("=:"):
                    print(dim(
                        "  To upload input to a Spartan URL, use:  "
                        "input <spartan-url> [text]"
                    ))
                else:
                    print(dim(
                        "  To open a link, use:  go <url>   "
                        "(or type its number on a rendered page)."
                    ))
                continue
            try:
                browser.navigate(_norm(raw))
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
    parser.add_argument("--check-feeds", action="store_true",
                        help="Check subscribed feeds for new entries and exit")

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

    # Non-interactive feed check mode (e.g. for cron).
    if args.check_feeds:
        set_use_color(config.color)
        browser = Browser(config)
        try:
            browser.check_feeds()
        finally:
            browser.close()
        return

    try:
        run_repl(args.url)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
