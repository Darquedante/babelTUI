#!/usr/bin/env python3
"""
Unit tests for the multi-protocol browser.

Run with:
    python -m pytest test_browser.py -v
    python -m pytest test_browser.py --cov=browser --cov-report=term-missing
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import browser as br


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

class TempConfigMixin:
    """Mixin to redirect config paths to a tempdir for the duration of a test."""

    def _setup_tempdir(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmppath = Path(self._tmpdir.name)
        self._patches = [
            patch.object(br, "_CONFIG_DIR", self.tmppath),
            patch.object(br, "_BOOKMARK_FILE", self.tmppath / "bookmarks.json"),
            patch.object(br, "_HISTORY_FILE", self.tmppath / "history.json"),
            patch.object(br, "_KNOWN_HOSTS_FILE", self.tmppath / "known_hosts.json"),
            patch.object(br, "_CONFIG_FILE", self.tmppath / "config.json"),
        ]
        for p in self._patches:
            p.start()

    def _teardown_tempdir(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()


def make_fake_fp(data: bytes):
    """Return a BytesIO that mimics socket.makefile('rb')."""
    return io.BytesIO(data)


class FakeSocket:
    """A minimal context-managed socket for fetch_* tests."""

    def __init__(self, response: bytes):
        self.response = response
        self.sent = b""
        self.fp = io.BytesIO(response)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        return self.fp.read(n)

    def makefile(self, mode="rb"):
        return self.fp


class FakeTLSSocket(FakeSocket):
    """Like FakeSocket but also fakes the TLS layer."""

    def __init__(self, response: bytes, cert: bytes = b"\x42" * 64):
        super().__init__(response)
        self.cert = cert

    def getpeercert(self, binary_form=False):
        return self.cert if binary_form else {}


class FakeTLSContext:
    """Stand-in for ssl.SSLContext that returns a FakeTLSSocket on wrap_socket."""

    def __init__(self, tls_sock: FakeTLSSocket):
        self.tls_sock = tls_sock
        self.check_hostname = False
        self.verify_mode = ssl.CERT_NONE
        self.minimum_version = None

    def wrap_socket(self, sock, server_hostname=None):
        return self.tls_sock


# ════════════════════════════════════════════════════════════════════════════
# Atomic JSON I/O
# ════════════════════════════════════════════════════════════════════════════

class TestAtomicJSON(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "test.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_atomic_write_and_read(self):
        data = {"a": 1, "b": "two", "c": [1, 2, 3]}
        br._atomic_write_json(self.path, data)
        self.assertEqual(br._read_json(self.path), data)

    def test_atomic_write_creates_parent(self):
        nested = self.path.parent / "sub" / "deep" / "file.json"
        br._atomic_write_json(nested, {"k": "v"})
        self.assertTrue(nested.exists())

    def test_atomic_write_no_leftover_tmp(self):
        br._atomic_write_json(self.path, {"x": 1})
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        self.assertFalse(tmp.exists())
        self.assertTrue(self.path.exists())

    def test_atomic_write_unicode(self):
        data = {"greeting": "héllo wörld 🌍", "lang": "日本語"}
        br._atomic_write_json(self.path, data)
        self.assertEqual(br._read_json(self.path), data)

    def test_read_json_missing_file(self):
        self.assertIsNone(br._read_json(self.path))

    def test_read_json_corrupt(self):
        self.path.write_text("{not valid json", encoding="utf-8")
        self.assertIsNone(br._read_json(self.path))

    def test_read_json_empty(self):
        self.path.write_text("", encoding="utf-8")
        self.assertIsNone(br._read_json(self.path))

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_atomic_write_mode(self):
        br._atomic_write_json(self.path, {"k": "v"}, mode=0o600)
        st_mode = self.path.stat().st_mode & 0o777
        self.assertEqual(st_mode, 0o600)


# ════════════════════════════════════════════════════════════════════════════
# Config
# ════════════════════════════════════════════════════════════════════════════

class TestConfig(TempConfigMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tempdir()

    def tearDown(self):
        self._teardown_tempdir()

    def test_default_config(self):
        cfg = br.Config()
        self.assertEqual(cfg.home, "spartan://mozz.us/")
        self.assertEqual(cfg.timeout, br.DEFAULT_TIMEOUT)
        self.assertTrue(cfg.color)
        self.assertFalse(cfg.pager)

    def test_from_dict_valid(self):
        cfg = br.Config.from_dict({"home": "gemini://example.org/", "timeout": 30})
        self.assertEqual(cfg.home, "gemini://example.org/")
        self.assertEqual(cfg.timeout, 30)

    def test_from_dict_ignores_unknown_keys(self):
        cfg = br.Config.from_dict({"home": "x", "garbage": "ignored"})
        self.assertEqual(cfg.home, "x")
        self.assertFalse(hasattr(cfg, "garbage"))

    def test_to_dict_roundtrip(self):
        cfg = br.Config(home="nex://test/", timeout=42, pager=True)
        restored = br.Config.from_dict(cfg.to_dict())
        self.assertEqual(cfg, restored)

    def test_load_config_missing_returns_default(self):
        cfg = br._load_config()
        self.assertEqual(cfg, br.Config())

    def test_load_config_corrupt_returns_default(self):
        br._CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        br._CONFIG_FILE.write_text("garbage")
        cfg = br._load_config()
        self.assertEqual(cfg, br.Config())

    def test_save_then_load(self):
        cfg = br.Config(home="kepler://h/", timeout=99, color=False, pager=True)
        br._save_config(cfg)
        loaded = br._load_config()
        self.assertEqual(loaded, cfg)


# ════════════════════════════════════════════════════════════════════════════
# Small helpers
# ════════════════════════════════════════════════════════════════════════════

class TestSmallHelpers(unittest.TestCase):
    def test_try_int_valid(self):
        self.assertEqual(br._try_int("42"), 42)
        self.assertEqual(br._try_int("-7"), -7)

    def test_try_int_invalid(self):
        self.assertEqual(br._try_int("abc"), -1)
        self.assertEqual(br._try_int(""), -1)
        self.assertEqual(br._try_int("3.14"), -1)
        self.assertEqual(br._try_int("abc", default=99), 99)

    def test_parse_status_code_valid(self):
        self.assertEqual(br._parse_status_code("20"), 20)
        self.assertEqual(br._parse_status_code("59"), 59)

    def test_parse_status_code_invalid(self):
        for bad in ("2", "200", "xy", ""):
            with self.assertRaises(ValueError):
                br._parse_status_code(bad)

    def test_require_host_ok(self):
        parts = br.urlparse("gemini://example.org/path")
        self.assertEqual(br._require_host(parts), "example.org")

    def test_require_host_missing(self):
        parts = br.urlparse("gemini:///path")
        with self.assertRaises(ValueError):
            br._require_host(parts)


# ════════════════════════════════════════════════════════════════════════════
# URL resolution / normalisation
# ════════════════════════════════════════════════════════════════════════════

class TestResolveURL(unittest.TestCase):
    def test_absolute_url_unchanged(self):
        self.assertEqual(br.resolve_url("gemini://a/b", "gemini://c/d"), "gemini://c/d")

    def test_absolute_path_replaces_path(self):
        self.assertEqual(br.resolve_url("gemini://a/b/c", "/index.gmi"), "gemini://a/index.gmi")

    def test_relative_path_joins_to_dir(self):
        self.assertEqual(br.resolve_url("gemini://a/b/c.gmi", "d.gmi"), "gemini://a/b/d.gmi")

    def test_protocol_relative(self):
        self.assertEqual(br.resolve_url("gemini://a/x", "//other.host/y"), "gemini://other.host/y")

    def test_kepler_relative_redirect(self):
        self.assertEqual(br.resolve_url("kepler://h/a/", "/index.gmi"), "kepler://h/index.gmi")

    def test_spartan_relative(self):
        self.assertEqual(br.resolve_url("spartan://h/a/b", "c"), "spartan://h/a/c")

    def test_nex_relative(self):
        self.assertEqual(br.resolve_url("nex://h/a/b", "c"), "nex://h/a/c")

    def test_dot_dot_traversal(self):
        self.assertEqual(br.resolve_url("gemini://h/a/b/c", "../x"), "gemini://h/a/x")

    def test_replace_query_basic(self):
        self.assertEqual(br.replace_query("gemini://h/p?old", "new"), "gemini://h/p?new")

    def test_replace_query_preserves_fragment(self):
        result = br.replace_query("gemini://h/p?old#frag", "new")
        self.assertIn("?new", result)
        self.assertIn("#frag", result)


class TestNormaliseURL(unittest.TestCase):
    def test_already_normalised(self):
        self.assertEqual(br.normalise_url("gemini://h/"), "gemini://h/")

    def test_user_at_host(self):
        self.assertEqual(br.normalise_url("alice@example.org"), "finger://example.org/alice")

    def test_bare_host(self):
        self.assertEqual(br.normalise_url("example.org"), "spartan://example.org")

    def test_localhost(self):
        self.assertEqual(br.normalise_url("localhost"), "spartan://localhost")

    def test_host_with_port(self):
        self.assertEqual(br.normalise_url("example.org:1965"), "spartan://example.org:1965")

    def test_ambiguous_raises(self):
        with self.assertRaises(ValueError):
            br.normalise_url("just-a-word")


# ════════════════════════════════════════════════════════════════════════════
# Language detection
# ════════════════════════════════════════════════════════════════════════════

class TestUserLanguage(unittest.TestCase):
    def setUp(self):
        br._get_user_language.cache_clear()

    def tearDown(self):
        br._get_user_language.cache_clear()

    def test_lang_en_us(self):
        with patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=False):
            br._get_user_language.cache_clear()
            self.assertEqual(br._get_user_language(), "en-US")

    def test_lang_c(self):
        with patch.dict(os.environ, {"LANG": "C"}, clear=False):
            br._get_user_language.cache_clear()
            self.assertEqual(br._get_user_language(), "?")

    def test_lang_posix(self):
        with patch.dict(os.environ, {"LANG": "POSIX"}, clear=False):
            br._get_user_language.cache_clear()
            self.assertEqual(br._get_user_language(), "?")

    def test_lang_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "LANG"}
        with patch.dict(os.environ, env, clear=True):
            br._get_user_language.cache_clear()
            self.assertEqual(br._get_user_language(), "?")

    def test_lang_simple(self):
        with patch.dict(os.environ, {"LANG": "ja_JP.UTF-8"}, clear=False):
            br._get_user_language.cache_clear()
            self.assertEqual(br._get_user_language(), "ja-JP")


# ════════════════════════════════════════════════════════════════════════════
# Kepler response parser
# ════════════════════════════════════════════════════════════════════════════

class TestParseKeplerResponse(unittest.TestCase):
    def test_minimal_success(self):
        fp = make_fake_fp(b"20 text/gemini\r\nhello world")
        code, mime, body, extras = br._parse_kepler_response(fp)
        self.assertEqual(code, 20)
        self.assertEqual(mime, "text/gemini")
        self.assertEqual(body, b"hello world")
        self.assertEqual(extras["content_length"], -1)

    def test_success_with_content_length(self):
        payload = b"hello world"
        fp = make_fake_fp(f"20 {len(payload)} text/gemini\r\n".encode() + payload + b"trailing")
        code, mime, body, extras = br._parse_kepler_response(fp)
        self.assertEqual(code, 20)
        self.assertEqual(mime, "text/gemini")
        self.assertEqual(body, payload)
        self.assertEqual(extras["content_length"], len(payload))

    def test_success_with_full_metadata(self):
        fp = make_fake_fp(b"20 3 100 200 text/plain\r\nabc")
        code, mime, body, extras = br._parse_kepler_response(fp)
        self.assertEqual(code, 20)
        self.assertEqual(mime, "text/plain")
        self.assertEqual(body, b"abc")
        self.assertEqual(extras["content_length"], 3)
        self.assertEqual(extras["last_updated"], 100)
        self.assertEqual(extras["expires"], 200)

    def test_success_mimetype_with_params(self):
        fp = make_fake_fp(b"20 text/gemini; charset=utf-8\r\nbody")
        code, mime, body, extras = br._parse_kepler_response(fp)
        self.assertEqual(code, 20)
        self.assertEqual(mime, "text/gemini; charset=utf-8")
        self.assertEqual(body, b"body")

    def test_redirect(self):
        fp = make_fake_fp(b"30 /new-location\r\n")
        code, meta, body, extras = br._parse_kepler_response(fp)
        self.assertEqual(code, 30)
        self.assertEqual(meta, "/new-location")
        self.assertEqual(body, b"")

    def test_input_prompt(self):
        fp = make_fake_fp(b"10 Enter search query\r\n")
        code, meta, body, extras = br._parse_kepler_response(fp)
        self.assertEqual(code, 10)
        self.assertEqual(meta, "Enter search query")

    def test_unchanged_70(self):
        fp = make_fake_fp(b"70 12345\r\n")
        code, meta, body, extras = br._parse_kepler_response(fp)
        self.assertEqual(code, 70)
        self.assertEqual(extras["expires"], 12345)

    def test_empty_response_raises(self):
        fp = make_fake_fp(b"")
        with self.assertRaises(ValueError):
            br._parse_kepler_response(fp)

    def test_invalid_status_raises(self):
        fp = make_fake_fp(b"xx whatever\r\n")
        with self.assertRaises(ValueError):
            br._parse_kepler_response(fp)


# ════════════════════════════════════════════════════════════════════════════
# Gopher helpers
# ════════════════════════════════════════════════════════════════════════════

class TestGopherHelpers(unittest.TestCase):
    def test_is_gopher_menu_simple(self):
        body = (
            b"iWelcome to Gopher\tfake\t(NULL)\t0\r\n"
            b"0Read me\t/readme.txt\thost\t70\r\n"
            b"1Subdirectory\t/sub\thost\t70\r\n"
            b".\r\n"
        )
        self.assertTrue(br.is_gopher_menu(body))

    def test_is_gopher_menu_no_terminator(self):
        body = b"iHello\tfoo\thost\t70\r\n0Read\t/r\thost\t70\r\n"
        self.assertFalse(br.is_gopher_menu(body))

    def test_is_gopher_menu_plain_text(self):
        body = b"This is just plain text\nNo gopher menu here\n.\r\n"
        self.assertFalse(br.is_gopher_menu(body))

    def test_is_gopher_menu_too_short(self):
        self.assertFalse(br.is_gopher_menu(b".\r\n"))
        self.assertFalse(br.is_gopher_menu(b""))

    def test_gopher_menu_to_gemtext_info(self):
        body = b"iAn info line\tfake\thost\t70\r\n.\r\n"
        result = br.gopher_menu_to_gemtext(body, "gopher://host/")
        self.assertIn("An info line", result)

    def test_gopher_menu_to_gemtext_text(self):
        # Selector "/readme.txt" gets prefixed with "/" → "//readme.txt".
        # This is faithful to the raw selector the server expects back.
        body = b"0Readme\t/readme.txt\thost\t70\r\n.\r\n"
        result = br.gopher_menu_to_gemtext(body, "gopher://host/")
        self.assertIn("gopher://host//readme.txt", result)
        self.assertIn("Readme", result)

    def test_gopher_menu_to_gemtext_directory(self):
        body = b"1Subdir\t/sub\thost\t70\r\n.\r\n"
        result = br.gopher_menu_to_gemtext(body, "gopher://host/")
        self.assertIn("gopher://host//sub", result)
        self.assertIn("Subdir", result)

    def test_gopher_menu_to_gemtext_search(self):
        body = b"7Search\t/q\thost\t70\r\n.\r\n"
        result = br.gopher_menu_to_gemtext(body, "gopher://host/")
        self.assertIn("gopher-search://host//q", result)

    def test_gopher_menu_to_gemtext_bare_selector(self):
        """Selectors without a leading slash should produce single-slash URLs."""
        body = b"0Readme\treadme.txt\thost\t70\r\n.\r\n"
        result = br.gopher_menu_to_gemtext(body, "gopher://host/")
        self.assertIn("gopher://host/readme.txt", result)
        self.assertNotIn("gopher://host//readme.txt", result)

    def test_gopher_menu_to_gemtext_http_link(self):
        body = b"hExternal\tURL:https://example.org\thost\t70\r\n.\r\n"
        result = br.gopher_menu_to_gemtext(body, "gopher://host/")
        self.assertIn("=> https://example.org", result)
        self.assertNotIn("URL:", result)

    def test_gopher_menu_to_gemtext_telnet(self):
        body = b"8Telnet\t/t\thost\t23\r\n.\r\n"
        result = br.gopher_menu_to_gemtext(body, "gopher://host/")
        self.assertIn("telnet://host", result)

    def test_gopher_menu_non_default_port(self):
        body = b"0Read\t/r\thost\t7070\r\n.\r\n"
        result = br.gopher_menu_to_gemtext(body, "gopher://host/")
        self.assertIn(":7070", result)


# ════════════════════════════════════════════════════════════════════════════
# Gemtext renderer
# ════════════════════════════════════════════════════════════════════════════

class TestRenderGemtext(unittest.TestCase):
    def test_link_extraction(self):
        text = "=> gemini://h/page Some label"
        lines, links = br.render_gemtext(text, "gemini://h/")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0], ("Some label", "gemini://h/page"))

    def test_link_without_label_uses_url(self):
        text = "=> gemini://h/page"
        lines, links = br.render_gemtext(text, "gemini://h/")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0][0], "gemini://h/page")

    def test_link_relative(self):
        text = "=> /sub Label"
        lines, links = br.render_gemtext(text, "gemini://h/a/")
        self.assertEqual(links[0][1], "gemini://h/sub")

    def test_headings(self):
        text = "# H1\n## H2\n### H3"
        lines, links = br.render_gemtext(text, "gemini://h/")
        flat = "\n".join(br.strip_ansi(l) for l in lines)
        self.assertIn("H1", flat)
        self.assertIn("H2", flat)
        self.assertIn("H3", flat)

    def test_preformatted_block(self):
        text = "```\ncode line\n```\nafter"
        lines, links = br.render_gemtext(text, "gemini://h/")
        flat = "\n".join(br.strip_ansi(l) for l in lines)
        self.assertIn("code line", flat)
        self.assertIn("after", flat)

    def test_quote(self):
        lines, _ = br.render_gemtext("> quoted text", "gemini://h/")
        flat = "\n".join(br.strip_ansi(l) for l in lines)
        self.assertIn("quoted text", flat)

    def test_list(self):
        lines, _ = br.render_gemtext("* one\n* two", "gemini://h/")
        flat = "\n".join(br.strip_ansi(l) for l in lines)
        self.assertIn("one", flat)
        self.assertIn("two", flat)

    def test_multiple_links_numbered(self):
        text = "=> a/1 A\n=> a/2 B\n=> a/3 C"
        _, links = br.render_gemtext(text, "gemini://h/")
        self.assertEqual(len(links), 3)

    def test_no_links_empty(self):
        _, links = br.render_gemtext("Just text\nNo links", "gemini://h/")
        self.assertEqual(links, [])

    def test_render_plain(self):
        lines = br.render_plain("line one\nline two")
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(l.startswith("  ") for l in lines))


# ════════════════════════════════════════════════════════════════════════════
# Finger renderer
# ════════════════════════════════════════════════════════════════════════════

class TestRenderFinger(unittest.TestCase):
    def test_basic(self):
        body = b"Login: alice\nName: Alice Liddell\nShell: /bin/bash\n"
        lines = br.render_finger(body, "finger://h/alice")
        flat = "\n".join(br.strip_ansi(l) for l in lines)
        self.assertIn("alice@h", flat)
        self.assertIn("Login:", flat)
        self.assertIn("Alice Liddell", flat)

    def test_no_user(self):
        lines = br.render_finger(b"some content", "finger://h/")
        flat = "\n".join(br.strip_ansi(l) for l in lines)
        self.assertIn("Finger: h", flat)

    def test_plan_field(self):
        lines = br.render_finger(b"Plan:\nDo cool things\n", "finger://h/u")
        flat = "\n".join(br.strip_ansi(l) for l in lines)
        self.assertIn("Plan:", flat)


# ════════════════════════════════════════════════════════════════════════════
# ANSI helpers + trunc + _read_n_bytes
# ════════════════════════════════════════════════════════════════════════════

class TestANSIHelpers(unittest.TestCase):
    def test_strip_ansi(self):
        s = "\033[31mred\033[0m \033[1mbold\033[0m plain"
        self.assertEqual(br.strip_ansi(s), "red bold plain")

    def test_strip_ansi_no_codes(self):
        self.assertEqual(br.strip_ansi("plain text"), "plain text")

    def test_highlight_query_current(self):
        result = br.highlight_query("hello world", "world", current=True)
        self.assertIn("world", br.strip_ansi(result))

    def test_highlight_query_case_insensitive(self):
        result = br.highlight_query("Hello World", "world", current=False)
        self.assertIn("World", br.strip_ansi(result))

    def test_highlight_empty_query(self):
        self.assertEqual(br.highlight_query("hello", "", current=True), "hello")


class TestTrunc(unittest.TestCase):
    def test_short_unchanged(self):
        self.assertEqual(br._trunc("hello", 10), "hello")

    def test_long_truncated(self):
        result = br._trunc("hello world this is long", 10)
        self.assertEqual(len(result), 10)
        self.assertIn("…", result)

    def test_exact_length(self):
        self.assertEqual(br._trunc("hello", 5), "hello")


class TestReadNBytes(unittest.TestCase):
    def test_exact(self):
        fp = io.BytesIO(b"hello world")
        self.assertEqual(br._read_n_bytes(fp, 5), b"hello")

    def test_eof_short(self):
        fp = io.BytesIO(b"hi")
        self.assertEqual(br._read_n_bytes(fp, 100), b"hi")

    def test_zero(self):
        fp = io.BytesIO(b"hello")
        self.assertEqual(br._read_n_bytes(fp, 0), b"")


# ════════════════════════════════════════════════════════════════════════════
# Plain socket protocols
# ════════════════════════════════════════════════════════════════════════════

class TestFetchSpartan(unittest.TestCase):
    def test_success(self):
        fake = FakeSocket(b"2 text/gemini\r\nHello!")
        with patch("socket.create_connection", return_value=fake):
            code, meta, body = br.fetch_spartan("spartan://h/", b"", 5)
        self.assertEqual((code, meta, body), (2, "text/gemini", b"Hello!"))

    def test_redirect(self):
        fake = FakeSocket(b"3 /new\r\n")
        with patch("socket.create_connection", return_value=fake):
            code, meta, body = br.fetch_spartan("spartan://h/", b"", 5)
        self.assertEqual((code, meta, body), (3, "/new", b""))

    def test_input_5(self):
        fake = FakeSocket(b"5 prompt\r\n")
        with patch("socket.create_connection", return_value=fake):
            code, meta, _ = br.fetch_spartan("spartan://h/", b"", 5)
        self.assertEqual((code, meta), (5, "prompt"))

    def test_error_4(self):
        fake = FakeSocket(b"4 not found\r\n")
        with patch("socket.create_connection", return_value=fake):
            code, _, _ = br.fetch_spartan("spartan://h/", b"", 5)
        self.assertEqual(code, 4)

    def test_request_format(self):
        fake = FakeSocket(b"2 text/gemini\r\nx")
        with patch("socket.create_connection", return_value=fake):
            br.fetch_spartan("spartan://example.org/path", b"data", 5)
        self.assertTrue(fake.sent.startswith(b"example.org /path 4\r\n"))
        self.assertTrue(fake.sent.endswith(b"data"))

    def test_query_becomes_body(self):
        fake = FakeSocket(b"2 text/gemini\r\nx")
        with patch("socket.create_connection", return_value=fake):
            br.fetch_spartan("spartan://h/p?hello", b"", 5)
        self.assertIn(b"h /p 5\r\n", fake.sent)
        self.assertTrue(fake.sent.endswith(b"hello"))

    def test_wrong_scheme(self):
        with self.assertRaises(ValueError):
            br.fetch_spartan("gemini://h/", b"", 5)

    def test_missing_host(self):
        with self.assertRaises(ValueError):
            br.fetch_spartan("spartan:///path", b"", 5)


class TestFetchNex(unittest.TestCase):
    def test_success(self):
        fake = FakeSocket(b"Hello Nex!")
        with patch("socket.create_connection", return_value=fake):
            code, _, body = br.fetch_nex("nex://h/path", 5)
        self.assertEqual((code, body), (2, b"Hello Nex!"))

    def test_empty_response_is_error(self):
        fake = FakeSocket(b"")
        with patch("socket.create_connection", return_value=fake):
            code, _, _ = br.fetch_nex("nex://h/", 5)
        self.assertEqual(code, 4)

    def test_request_format(self):
        fake = FakeSocket(b"data")
        with patch("socket.create_connection", return_value=fake):
            br.fetch_nex("nex://host/foo/bar", 5)
        self.assertEqual(fake.sent, b"/foo/bar\r\n")

    def test_wrong_scheme(self):
        with self.assertRaises(ValueError):
            br.fetch_nex("gopher://h/", 5)


class TestFetchGopher(unittest.TestCase):
    def test_success(self):
        fake = FakeSocket(b"iHello\tfake\thost\t70\r\n.\r\n")
        with patch("socket.create_connection", return_value=fake):
            code, _, body = br.fetch_gopher("gopher://h/", 5)
        self.assertEqual(code, 2)
        self.assertIn(b"Hello", body)

    def test_query_request(self):
        fake = FakeSocket(b"data")
        with patch("socket.create_connection", return_value=fake):
            br.fetch_gopher("gopher://h/sel", 5, query="hello")
        self.assertEqual(fake.sent, b"sel\thello\r\n")

    def test_root_selector(self):
        fake = FakeSocket(b"data")
        with patch("socket.create_connection", return_value=fake):
            br.fetch_gopher("gopher://h/", 5)
        self.assertEqual(fake.sent, b"\r\n")

    def test_wrong_scheme(self):
        with self.assertRaises(ValueError):
            br.fetch_gopher("nex://h/", 5)


class TestFetchFinger(unittest.TestCase):
    def test_with_user_in_path(self):
        fake = FakeSocket(b"Login: alice\nName: Alice\n")
        with patch("socket.create_connection", return_value=fake):
            code, meta, _ = br.fetch_finger("finger://h/alice", 5)
        self.assertEqual((code, meta), (2, "alice"))
        self.assertEqual(fake.sent, b"alice\r\n")

    def test_with_user_in_userinfo(self):
        fake = FakeSocket(b"data")
        with patch("socket.create_connection", return_value=fake):
            _, meta, _ = br.fetch_finger("finger://alice@h/", 5)
        self.assertEqual(meta, "alice")

    def test_empty_user(self):
        fake = FakeSocket(b"server info")
        with patch("socket.create_connection", return_value=fake):
            code, _, _ = br.fetch_finger("finger://h/", 5)
        self.assertEqual(code, 2)
        self.assertEqual(fake.sent, b"\r\n")

    def test_empty_response_is_error(self):
        fake = FakeSocket(b"")
        with patch("socket.create_connection", return_value=fake):
            code, _, _ = br.fetch_finger("finger://h/alice", 5)
        self.assertEqual(code, 4)


# ════════════════════════════════════════════════════════════════════════════
# Kepler — plaintext + TLS
# ════════════════════════════════════════════════════════════════════════════

class TestFetchKeplerPlaintext(unittest.TestCase):
    def test_plaintext_success(self):
        fake = FakeSocket(b"20 text/gemini\r\nHello Kepler!")
        with patch("socket.create_connection", return_value=fake):
            code, meta, body, _ = br.fetch_kepler("kepler://h/", 5, {})
        self.assertEqual((code, meta, body), (20, "text/gemini", b"Hello Kepler!"))

    def test_userinfo_rejected(self):
        with self.assertRaises(ValueError):
            br.fetch_kepler("kepler://user@h/", 5, {})

    def test_uri_too_long(self):
        long_url = "kepler://h/" + "x" * 2000
        with self.assertRaises(ValueError):
            br.fetch_kepler(long_url, 5, {})

    def test_wrong_scheme(self):
        with self.assertRaises(ValueError):
            br.fetch_kepler("gemini://h/", 5, {})

    def test_request_format(self):
        fake = FakeSocket(b"20 text/gemini\r\nx")
        with patch("socket.create_connection", return_value=fake):
            br.fetch_kepler("kepler://h/page", 5, {}, last_cached=42, language="en-US")
        self.assertIn(b"kepler://h/page 42 en-US\r\n", fake.sent)

    def test_fragment_stripped_from_request(self):
        fake = FakeSocket(b"20 text/gemini\r\nx")
        with patch("socket.create_connection", return_value=fake):
            br.fetch_kepler("kepler://h/page#frag", 5, {}, language="en")
        self.assertNotIn(b"#frag", fake.sent)


class TestFetchKeplerTLS(TempConfigMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tempdir()

    def tearDown(self):
        self._teardown_tempdir()

    def test_keplers_success_new_host(self):
        cert = b"\x11" * 64
        tls_sock = FakeTLSSocket(b"20 text/gemini\r\nSecure!", cert=cert)
        ctx = FakeTLSContext(tls_sock)
        known = {}
        with patch("socket.create_connection", return_value=FakeSocket(b"")), \
             patch.object(br, "_make_tls_context", return_value=ctx):
            code, meta, body, _ = br.fetch_kepler("keplers://h/", 5, known)
        self.assertEqual((code, meta, body), (20, "text/gemini", b"Secure!"))
        # The new fingerprint should have been pinned and persisted.
        self.assertIn("h", known)
        expected_fp = hashlib.sha256(cert).hexdigest()
        self.assertEqual(known["h"], expected_fp)

    def test_keplers_known_host_matches(self):
        cert = b"\x22" * 64
        fp = hashlib.sha256(cert).hexdigest()
        tls_sock = FakeTLSSocket(b"20 text/gemini\r\nok", cert=cert)
        ctx = FakeTLSContext(tls_sock)
        known = {"h": fp}
        with patch("socket.create_connection", return_value=FakeSocket(b"")), \
             patch.object(br, "_make_tls_context", return_value=ctx):
            code, _, _, _ = br.fetch_kepler("keplers://h/", 5, known)
        # Don't even need to assert anything beyond "didn't raise"
        self.assertEqual(code, 20)

    def test_keplers_fingerprint_mismatch_non_interactive(self):
        cert = b"\x33" * 64
        tls_sock = FakeTLSSocket(b"20 text/gemini\r\nx", cert=cert)
        ctx = FakeTLSContext(tls_sock)
        known = {"h": "deadbeef" * 8}  # wrong fingerprint
        with patch("socket.create_connection", return_value=FakeSocket(b"")), \
             patch.object(br, "_make_tls_context", return_value=ctx), \
             patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaises(ssl.SSLError):
                br.fetch_kepler("keplers://h/", 5, known)


class TestFetchGeminiTLS(TempConfigMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tempdir()

    def tearDown(self):
        self._teardown_tempdir()

    def test_gemini_success_new_host(self):
        cert = b"\x44" * 64
        tls_sock = FakeTLSSocket(b"20 text/gemini\r\nHello Gemini!", cert=cert)
        ctx = FakeTLSContext(tls_sock)
        known = {}
        with patch("socket.create_connection", return_value=FakeSocket(b"")), \
             patch.object(br, "_make_tls_context", return_value=ctx):
            code, meta, body = br.fetch_gemini("gemini://h/", 5, known)
        self.assertEqual((code, meta, body), (20, "text/gemini", b"Hello Gemini!"))
        self.assertIn("h", known)

    def test_gemini_redirect(self):
        cert = b"\x55" * 64
        tls_sock = FakeTLSSocket(b"30 /new\r\n", cert=cert)
        ctx = FakeTLSContext(tls_sock)
        with patch("socket.create_connection", return_value=FakeSocket(b"")), \
             patch.object(br, "_make_tls_context", return_value=ctx):
            code, meta, body = br.fetch_gemini("gemini://h/old", 5, {})
        self.assertEqual((code, meta, body), (30, "/new", b""))

    def test_gemini_input(self):
        cert = b"\x66" * 64
        tls_sock = FakeTLSSocket(b"10 prompt\r\n", cert=cert)
        ctx = FakeTLSContext(tls_sock)
        with patch("socket.create_connection", return_value=FakeSocket(b"")), \
             patch.object(br, "_make_tls_context", return_value=ctx):
            code, meta, _ = br.fetch_gemini("gemini://h/", 5, {})
        self.assertEqual((code, meta), (10, "prompt"))

    def test_gemini_error(self):
        cert = b"\x77" * 64
        tls_sock = FakeTLSSocket(b"51 not found\r\n", cert=cert)
        ctx = FakeTLSContext(tls_sock)
        with patch("socket.create_connection", return_value=FakeSocket(b"")), \
             patch.object(br, "_make_tls_context", return_value=ctx):
            code, _, body = br.fetch_gemini("gemini://h/", 5, {})
        self.assertEqual(code, 51)
        self.assertEqual(body, b"")

    def test_gemini_request_format(self):
        cert = b"\x88" * 64
        tls_sock = FakeTLSSocket(b"20 text/gemini\r\nx", cert=cert)
        ctx = FakeTLSContext(tls_sock)
        with patch("socket.create_connection", return_value=FakeSocket(b"")), \
             patch.object(br, "_make_tls_context", return_value=ctx):
            br.fetch_gemini("gemini://h/path", 5, {})
        self.assertEqual(tls_sock.sent, b"gemini://h/path\r\n")

    def test_gemini_uri_too_long(self):
        long_url = "gemini://h/" + "x" * 2000
        with self.assertRaises(ValueError):
            br.fetch_gemini(long_url, 5, {})

    def test_gemini_wrong_scheme(self):
        with self.assertRaises(ValueError):
            br.fetch_gemini("kepler://h/", 5, {})


# ════════════════════════════════════════════════════════════════════════════
# Bookmarks / History / Known hosts persistence
# ════════════════════════════════════════════════════════════════════════════

class TestBookmarksAndHistory(TempConfigMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tempdir()

    def tearDown(self):
        self._teardown_tempdir()

    def test_load_bookmarks_missing(self):
        self.assertEqual(br._load_bookmarks(), {})

    def test_save_and_load_bookmarks(self):
        bms = {"home": "gemini://h/", "test": "kepler://k/"}
        br._save_bookmarks(bms)
        self.assertEqual(br._load_bookmarks(), bms)

    def test_load_bookmarks_corrupt(self):
        br._BOOKMARK_FILE.parent.mkdir(parents=True, exist_ok=True)
        br._BOOKMARK_FILE.write_text("not json")
        self.assertEqual(br._load_bookmarks(), {})

    def test_load_history_missing(self):
        self.assertEqual(br._load_history(), [])

    def test_save_and_load_history(self):
        hist = [f"gemini://h/{i}" for i in range(5)]
        br._save_history(hist)
        self.assertEqual(br._load_history(), hist)

    def test_history_respects_limit(self):
        hist = [f"gemini://h/{i}" for i in range(20)]
        br._save_history(hist, limit=5)
        self.assertEqual(br._load_history(limit=5), hist[-5:])

    def test_load_history_wrong_type(self):
        br._HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        br._HISTORY_FILE.write_text(json.dumps({"not": "a list"}))
        self.assertEqual(br._load_history(), [])


class TestKnownHosts(TempConfigMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tempdir()

    def tearDown(self):
        self._teardown_tempdir()

    def test_save_and_load(self):
        hosts = {"example.org": "abc123" * 10}
        br._save_known_hosts(hosts)
        self.assertEqual(br._load_known_hosts(), hosts)

    def test_load_missing(self):
        self.assertEqual(br._load_known_hosts(), {})


class TestTofuCheck(unittest.TestCase):
    def _mock_sock(self, fingerprint_bytes):
        m = MagicMock(spec=ssl.SSLSocket)
        m.getpeercert.return_value = fingerprint_bytes
        return m

    def test_new_host_accepted(self):
        sock = self._mock_sock(b"\x01" * 32)
        known = {}
        changed = br._tofu_check(sock, "h", known, accept_new_host=True)
        self.assertTrue(changed)
        self.assertIn("h", known)

    def test_new_host_rejected(self):
        sock = self._mock_sock(b"\x01" * 32)
        with self.assertRaises(ssl.SSLError):
            br._tofu_check(sock, "h", {}, accept_new_host=False)

    def test_known_host_matches(self):
        cert = b"\x02" * 32
        fp = hashlib.sha256(cert).hexdigest()
        sock = self._mock_sock(cert)
        known = {"h": fp}
        changed = br._tofu_check(sock, "h", known, accept_new_host=True)
        self.assertFalse(changed)

    def test_no_cert_raises(self):
        sock = self._mock_sock(None)
        with self.assertRaises(ssl.SSLError):
            br._tofu_check(sock, "h", {}, accept_new_host=True)

    def test_fingerprint_mismatch_non_interactive(self):
        sock = self._mock_sock(b"\x03" * 32)
        known = {"h": "deadbeef" * 8}
        with patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaises(ssl.SSLError):
                br._tofu_check(sock, "h", known, accept_new_host=True)


# ════════════════════════════════════════════════════════════════════════════
# Browser class — high-level behaviour
# ════════════════════════════════════════════════════════════════════════════

class TestBrowserNavigation(TempConfigMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tempdir()
        self._stdout_patch = patch("sys.stdout", new_callable=io.StringIO)
        self._stdout_patch.start()
        self.browser = br.Browser()

    def tearDown(self):
        self._stdout_patch.stop()
        self._teardown_tempdir()

    def test_initial_state(self):
        self.assertIsNone(self.browser.current_url)
        self.assertEqual(self.browser.current_links, [])
        self.assertEqual(self.browser.bookmarks, {})

    def test_home_property(self):
        self.assertEqual(self.browser.HOME, self.browser.config.home)

    def test_back_at_start(self):
        self.browser.go_back()  # should not crash

    def test_forward_at_end(self):
        self.browser.go_forward()

    def test_reload_no_page(self):
        self.browser.reload()

    def test_go_up_no_page(self):
        self.browser.go_up()

    def test_go_up_at_root(self):
        self.browser.current_url = "gemini://h/"
        self.browser.go_up()  # already at root; no crash

    def test_go_up_from_nested(self):
        self.browser.current_url = "gemini://h/a/b/c"
        with patch.object(self.browser, "navigate") as mock_nav:
            self.browser.go_up()
        mock_nav.assert_called_once()
        called_url = mock_nav.call_args[0][0]
        self.assertTrue(called_url.endswith("/a/b/"))

    def test_bookmark_add_no_page(self):
        self.browser.bookmark_add("test")
        self.assertEqual(self.browser.bookmarks, {})

    def test_bookmark_add_with_page(self):
        self.browser.current_url = "gemini://example.org/"
        self.browser.bookmark_add("example")
        self.assertIn("example", self.browser.bookmarks)
        self.assertEqual(self.browser.bookmarks["example"], "gemini://example.org/")

    def test_bookmark_add_gopher_search_blocked(self):
        self.browser.current_url = "gopher-search://h/q"
        self.browser.bookmark_add("nope")
        self.assertEqual(self.browser.bookmarks, {})

    def test_bookmark_delete(self):
        self.browser.bookmarks["x"] = "gemini://x/"
        self.browser.bookmark_delete("x")
        self.assertNotIn("x", self.browser.bookmarks)

    def test_bookmark_delete_missing(self):
        self.browser.bookmark_delete("nonexistent")  # no crash

    def test_bookmark_open_by_index(self):
        self.browser.bookmarks = {"a": "gemini://a/", "b": "gemini://b/"}
        with patch.object(self.browser, "navigate") as mock_nav:
            self.browser.bookmark_open("2")
        mock_nav.assert_called_once_with("gemini://b/")

    def test_bookmark_open_by_name(self):
        self.browser.bookmarks = {"home": "gemini://h/"}
        with patch.object(self.browser, "navigate") as mock_nav:
            self.browser.bookmark_open("home")
        mock_nav.assert_called_once_with("gemini://h/")

    def test_bookmark_open_unknown(self):
        with patch.object(self.browser, "navigate") as mock_nav:
            self.browser.bookmark_open("nope")
        mock_nav.assert_not_called()

    def test_history_clear_with_confirm(self):
        self.browser.history = ["a", "b", "c"]
        self.browser.hist_pos = 2
        with patch.object(self.browser, "_confirm", return_value=True):
            self.browser.history_clear()
        self.assertEqual(self.browser.history, [])
        self.assertEqual(self.browser.hist_pos, -1)

    def test_history_clear_declined(self):
        self.browser.history = ["a", "b"]
        with patch.object(self.browser, "_confirm", return_value=False):
            self.browser.history_clear()
        self.assertEqual(self.browser.history, ["a", "b"])

    def test_history_delete_by_index(self):
        self.browser.history = ["a", "b", "c"]
        self.browser.hist_pos = 2
        self.browser.history_delete("2")
        self.assertEqual(self.browser.history, ["a", "c"])

    def test_history_delete_by_url(self):
        self.browser.history = ["gemini://a/", "gemini://b/"]
        self.browser.hist_pos = 1
        self.browser.history_delete("gemini://a/")
        self.assertEqual(self.browser.history, ["gemini://b/"])

    def test_history_delete_invalid_index(self):
        self.browser.history = ["a"]
        self.browser.history_delete("99")
        self.assertEqual(self.browser.history, ["a"])

    def test_history_delete_empty(self):
        self.browser.history = []
        self.browser.history_delete("1")  # no crash

    def test_open_link_invalid_number(self):
        self.browser.open_link("notanumber")

    def test_open_link_out_of_range(self):
        self.browser.current_links = [("label", "gemini://h/")]
        self.browser.open_link("99")

    def test_open_link_supported_scheme(self):
        self.browser.current_links = [("label", "gemini://h/p")]
        with patch.object(self.browser, "navigate") as mock_nav:
            self.browser.open_link("1")
        mock_nav.assert_called_once_with("gemini://h/p")

    def test_open_link_unsupported_scheme(self):
        self.browser.current_links = [("label", "http://example.org/")]
        with patch.object(self.browser, "navigate") as mock_nav:
            self.browser.open_link("1")
        mock_nav.assert_not_called()

    def test_open_link_keplers_to_kepler_declined(self):
        self.browser.current_url = "keplers://secure/"
        self.browser.current_links = [("label", "kepler://insecure/")]
        with patch.object(self.browser, "_confirm", return_value=False), \
             patch.object(self.browser, "navigate") as mock_nav:
            self.browser.open_link("1")
        mock_nav.assert_not_called()

    def test_find_no_page(self):
        self.browser.find("anything")

    def test_find_empty_query(self):
        self.browser.current_render_lines = ["hello world"]
        self.browser.find("   ")

    def test_find_no_matches(self):
        self.browser.current_render_lines = ["hello world"]
        # No matches → no crash, no find mode entered
        with patch.object(br, "_find_mode") as mock_fm:
            self.browser.find("xyzzy")
        mock_fm.assert_not_called()

    def test_find_with_matches(self):
        self.browser.current_render_lines = ["hello world", "another hello"]
        with patch.object(br, "_find_mode") as mock_fm:
            self.browser.find("hello")
        mock_fm.assert_called_once()

    def test_finger_query_user_at_host(self):
        with patch.object(self.browser, "navigate") as mock_nav:
            self.browser.finger_query("alice@example.org")
        mock_nav.assert_called_once_with("finger://example.org/alice")

    def test_finger_query_host_only(self):
        with patch.object(self.browser, "navigate") as mock_nav:
            self.browser.finger_query("example.org")
        mock_nav.assert_called_once_with("finger://example.org")

    def test_finger_query_finger_url(self):
        with patch.object(self.browser, "navigate") as mock_nav:
            self.browser.finger_query("finger://h/u")
        mock_nav.assert_called_once_with("finger://h/u")

    def test_finger_query_empty(self):
        with patch.object(self.browser, "navigate") as mock_nav:
            self.browser.finger_query("   ")
        mock_nav.assert_not_called()

    def test_set_option_pager(self):
        self.browser.set_option("pager", "on")
        self.assertTrue(self.browser.pager_enabled)
        self.browser.set_option("pager", "off")
        self.assertFalse(self.browser.pager_enabled)

    def test_set_option_color(self):
        self.browser.set_option("color", "on")
        self.assertTrue(self.browser.config.color)
        self.browser.set_option("color", "off")
        self.assertFalse(self.browser.config.color)

    def test_set_option_timeout(self):
        self.browser.set_option("timeout", "30")
        self.assertEqual(self.browser.config.timeout, 30)

    def test_set_option_timeout_invalid(self):
        original = self.browser.config.timeout
        self.browser.set_option("timeout", "notanumber")
        self.assertEqual(self.browser.config.timeout, original)

    def test_set_option_timeout_negative(self):
        original = self.browser.config.timeout
        self.browser.set_option("timeout", "-5")
        self.assertEqual(self.browser.config.timeout, original)

    def test_set_option_home(self):
        self.browser.set_option("home", "gemini://example.org/")
        self.assertEqual(self.browser.config.home, "gemini://example.org/")

    def test_set_option_history_limit(self):
        self.browser.set_option("history_limit", "100")
        self.assertEqual(self.browser.config.history_limit, 100)

    def test_set_option_unknown(self):
        self.browser.set_option("nonexistent", "value")  # no crash

    def test_close_flushes_history(self):
        self.browser.history = ["gemini://a/", "gemini://b/"]
        self.browser.close()
        self.assertEqual(br._load_history(), ["gemini://a/", "gemini://b/"])


class TestBrowserSuccessHandling(TempConfigMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tempdir()
        self._stdout_patch = patch("sys.stdout", new_callable=io.StringIO)
        self._stdout_patch.start()
        self.browser = br.Browser()

    def tearDown(self):
        self._stdout_patch.stop()
        self._teardown_tempdir()

    def test_success_updates_history(self):
        self.browser._handle_success(
            "gemini://h/page", b"# Hello", "text/gemini", push_history=True,
        )
        self.assertEqual(self.browser.current_url, "gemini://h/page")
        self.assertIn("gemini://h/page", self.browser.history)
        self.assertEqual(self.browser.last_mime, "text/gemini")

    def test_success_no_history_push(self):
        original = list(self.browser.history)
        self.browser._handle_success(
            "gemini://h/page", b"# Hi", "text/gemini", push_history=False,
        )
        self.assertEqual(self.browser.history, original)

    def test_success_dedups_consecutive(self):
        self.browser._handle_success("gemini://h/a", b"x", "text/gemini", True)
        self.browser._handle_success("gemini://h/a", b"x", "text/gemini", True)
        self.assertEqual(self.browser.history.count("gemini://h/a"), 1)

    def test_navigate_forward_clears_future(self):
        for u in ["gemini://h/a", "gemini://h/b", "gemini://h/c"]:
            self.browser._handle_success(u, b"x", "text/gemini", True)
        self.browser.hist_pos = 0
        self.browser._handle_success("gemini://h/new", b"x", "text/gemini", True)
        self.assertEqual(self.browser.history[-1], "gemini://h/new")


class TestBrowserSchemeDispatch(TempConfigMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tempdir()
        self._stdout_patch = patch("sys.stdout", new_callable=io.StringIO)
        self._stdout_patch.start()
        self.browser = br.Browser()

    def tearDown(self):
        self._stdout_patch.stop()
        self._teardown_tempdir()

    def test_unsupported_scheme(self):
        with self.assertRaises(ValueError):
            self.browser._fetch_url("http://example.org/", b"")

    def test_supported_schemes_have_fetcher(self):
        for scheme in ("kepler", "keplers", "spartan", "gemini", "nex", "gopher", "finger"):
            self.assertIn(scheme, self.browser._fetchers)
            self.assertIn(scheme, self.browser._response_handlers)


# ════════════════════════════════════════════════════════════════════════════
# Response handlers (in isolation)
# ════════════════════════════════════════════════════════════════════════════

class TestResponseHandlers(TempConfigMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tempdir()
        self._stdout_patch = patch("sys.stdout", new_callable=io.StringIO)
        self._stdout_patch.start()
        self.browser = br.Browser()

    def tearDown(self):
        self._stdout_patch.stop()
        self._teardown_tempdir()

    def test_spartan_success(self):
        r = self.browser._handle_spartan(2, "text/gemini", b"hi", {}, "spartan://h/", True)
        self.assertTrue(r.done)

    def test_spartan_redirect(self):
        r = self.browser._handle_spartan(3, "/new", b"", {}, "spartan://h/old", True)
        self.assertFalse(r.done)
        self.assertEqual(r.redirect_to, "spartan://h/new")

    def test_spartan_error(self):
        r = self.browser._handle_spartan(4, "not found", b"", {}, "spartan://h/", True)
        self.assertTrue(r.done)

    def test_spartan_input_5_with_input(self):
        with patch.object(self.browser, "_prompt_input", return_value="search"):
            r = self.browser._handle_spartan(5, "Query?", b"", {}, "spartan://h/", True)
        self.assertFalse(r.done)
        self.assertEqual(r.data, b"search")
        self.assertTrue(r.reset_redirects)

    def test_spartan_input_5_cancelled(self):
        with patch.object(self.browser, "_prompt_input", return_value=None):
            r = self.browser._handle_spartan(5, "Query?", b"", {}, "spartan://h/", True)
        self.assertTrue(r.done)

    def test_spartan_unknown_code(self):
        r = self.browser._handle_spartan(9, "huh", b"", {}, "spartan://h/", True)
        self.assertTrue(r.done)

    def test_kepler_success(self):
        r = self.browser._handle_kepler(20, "text/gemini", b"hi", {}, "kepler://h/", True)
        self.assertTrue(r.done)

    def test_kepler_redirect(self):
        r = self.browser._handle_kepler(30, "/new", b"", {}, "kepler://h/old", True)
        self.assertFalse(r.done)
        self.assertTrue(r.redirect_to.endswith("/new"))

    def test_kepler_empty_redirect(self):
        r = self.browser._handle_kepler(30, "", b"", {}, "kepler://h/", True)
        self.assertTrue(r.done)

    def test_kepler_keplers_to_kepler_redirect_declined(self):
        with patch.object(self.browser, "_confirm", return_value=False):
            r = self.browser._handle_kepler(
                30, "kepler://other/", b"", {}, "keplers://h/", True,
            )
        self.assertTrue(r.done)

    def test_kepler_input_10(self):
        with patch.object(self.browser, "_prompt_input", return_value="hello"):
            r = self.browser._handle_kepler(10, "Prompt?", b"", {}, "kepler://h/p", True)
        self.assertFalse(r.done)
        self.assertIn("hello", r.redirect_to)

    def test_kepler_input_11_sensitive(self):
        with patch.object(self.browser, "_prompt_input") as mock_in:
            mock_in.return_value = "secret"
            self.browser._handle_kepler(11, "Password?", b"", {}, "kepler://h/p", True)
        args, kwargs = mock_in.call_args
        self.assertTrue(kwargs.get("sensitive") or (len(args) > 1 and args[1]))

    def test_kepler_input_cancelled(self):
        with patch.object(self.browser, "_prompt_input", return_value=None):
            r = self.browser._handle_kepler(10, "Prompt?", b"", {}, "kepler://h/p", True)
        self.assertTrue(r.done)

    def test_kepler_error_51(self):
        r = self.browser._handle_kepler(51, "not found", b"", {}, "kepler://h/", True)
        self.assertTrue(r.done)

    def test_kepler_unchanged_70(self):
        r = self.browser._handle_kepler(70, "12345", b"", {"expires": 12345}, "kepler://h/", True)
        self.assertTrue(r.done)

    def test_kepler_cert_required_60(self):
        r = self.browser._handle_kepler(60, "cert please", b"", {}, "kepler://h/", True)
        self.assertTrue(r.done)

    def test_kepler_unknown_code(self):
        r = self.browser._handle_kepler(99, "???", b"", {}, "kepler://h/", True)
        self.assertTrue(r.done)

    def test_gemini_success(self):
        r = self.browser._handle_gemini(20, "text/gemini", b"hi", {}, "gemini://h/", True)
        self.assertTrue(r.done)

    def test_gemini_redirect(self):
        r = self.browser._handle_gemini(30, "/new", b"", {}, "gemini://h/old", True)
        self.assertFalse(r.done)
        self.assertEqual(r.redirect_to, "gemini://h/new")

    def test_gemini_input(self):
        with patch.object(self.browser, "_prompt_input", return_value="q"):
            r = self.browser._handle_gemini(10, "Search", b"", {}, "gemini://h/s", True)
        self.assertFalse(r.done)
        self.assertIn("?q", r.redirect_to)

    def test_gemini_input_cancelled(self):
        with patch.object(self.browser, "_prompt_input", return_value=None):
            r = self.browser._handle_gemini(10, "Search", b"", {}, "gemini://h/s", True)
        self.assertTrue(r.done)

    def test_gemini_temporary_error_4x(self):
        r = self.browser._handle_gemini(40, "temp", b"", {}, "gemini://h/", True)
        self.assertTrue(r.done)

    def test_gemini_permanent_error_5x(self):
        r = self.browser._handle_gemini(51, "not found", b"", {}, "gemini://h/", True)
        self.assertTrue(r.done)

    def test_gemini_unknown_code(self):
        r = self.browser._handle_gemini(99, "???", b"", {}, "gemini://h/", True)
        self.assertTrue(r.done)

    def test_nex_success(self):
        r = self.browser._handle_nex(2, "", b"hi", {}, "nex://h/", True)
        self.assertTrue(r.done)

    def test_nex_error(self):
        r = self.browser._handle_nex(4, "err", b"", {}, "nex://h/", True)
        self.assertTrue(r.done)

    def test_nex_unknown_code(self):
        r = self.browser._handle_nex(9, "???", b"", {}, "nex://h/", True)
        self.assertTrue(r.done)

    def test_gopher_success(self):
        r = self.browser._handle_gopher(2, "", b"hi", {}, "gopher://h/", True)
        self.assertTrue(r.done)

    def test_gopher_unknown_code(self):
        r = self.browser._handle_gopher(9, "???", b"", {}, "gopher://h/", True)
        self.assertTrue(r.done)

    def test_finger_success(self):
        r = self.browser._handle_finger(2, "alice", b"info", {}, "finger://h/alice", True)
        self.assertTrue(r.done)

    def test_finger_error(self):
        r = self.browser._handle_finger(4, "err", b"", {}, "finger://h/", True)
        self.assertTrue(r.done)


# ════════════════════════════════════════════════════════════════════════════
# HandlerResult
# ════════════════════════════════════════════════════════════════════════════

class TestHandlerResult(unittest.TestCase):
    def test_defaults(self):
        r = br.HandlerResult()
        self.assertTrue(r.done)
        self.assertIsNone(r.redirect_to)
        self.assertIsNone(r.new_url)
        self.assertFalse(r.reset_redirects)
        self.assertEqual(r.data, b"")

    def test_redirect_construction(self):
        r = br.HandlerResult(done=False, redirect_to="gemini://h/")
        self.assertFalse(r.done)
        self.assertEqual(r.redirect_to, "gemini://h/")


# ════════════════════════════════════════════════════════════════════════════
# _fetch loop — integration tests for redirects, loops, input round-trips
# ════════════════════════════════════════════════════════════════════════════

class TestFetchLoop(TempConfigMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tempdir()
        self._stdout_patch = patch("sys.stdout", new_callable=io.StringIO)
        self._stdout_patch.start()
        self.browser = br.Browser()

    def tearDown(self):
        self._stdout_patch.stop()
        self._teardown_tempdir()

    def test_simple_redirect_chain(self):
        """A → 3 /B → 2 OK should land on B."""
        responses = {
            "spartan://h/a": (3, "/b", b"", {}),
            "spartan://h/b": (2, "text/gemini", b"# Final", {}),
        }
        def fake_fetch(url, data=b""):
            return responses[url]
        with patch.object(self.browser, "_fetch_url", side_effect=fake_fetch):
            self.browser._fetch("spartan://h/a")
        self.assertEqual(self.browser.current_url, "spartan://h/b")

    def test_redirect_loop_detected(self):
        """A → B → A should abort, not infinite-loop."""
        responses = {
            "spartan://h/a": (3, "/b", b"", {}),
            "spartan://h/b": (3, "/a", b"", {}),
        }
        def fake_fetch(url, data=b""):
            return responses[url]
        with patch.object(self.browser, "_fetch_url", side_effect=fake_fetch):
            # Should return cleanly without recursion error
            self.browser._fetch("spartan://h/a")
        # Browser state remains uncommitted (no success was reached)
        self.assertIsNone(self.browser.current_url)

    def test_too_many_redirects(self):
        """A long redirect chain should bail when limit is exceeded."""
        # Build a chain longer than _MAX_REDIRECTS for spartan
        n_links = br._MAX_REDIRECTS + 5
        def fake_fetch(url, data=b""):
            # Each URL redirects to the next
            idx = int(url.rsplit("/", 1)[1])
            return (3, f"/{idx + 1}", b"", {})
        with patch.object(self.browser, "_fetch_url", side_effect=fake_fetch):
            self.browser._fetch("spartan://h/0")
        self.assertIsNone(self.browser.current_url)

    def test_network_error_caught(self):
        with patch.object(self.browser, "_fetch_url", side_effect=OSError("boom")):
            self.browser._fetch("spartan://h/")
        self.assertIsNone(self.browser.current_url)

    def test_ssl_error_caught(self):
        with patch.object(self.browser, "_fetch_url", side_effect=ssl.SSLError("bad cert")):
            self.browser._fetch("keplers://h/")
        self.assertIsNone(self.browser.current_url)

    def test_value_error_caught(self):
        with patch.object(self.browser, "_fetch_url", side_effect=ValueError("bad url")):
            self.browser._fetch("spartan://h/")
        self.assertIsNone(self.browser.current_url)

    def test_spartan_input_round_trip(self):
        """Code 5 prompts for input, then re-fetches with body."""
        call_log = []

        def fake_fetch(url, data=b""):
            call_log.append((url, data))
            if data == b"":
                return (5, "Search?", b"", {})
            return (2, "text/gemini", b"# Results", {})

        with patch.object(self.browser, "_fetch_url", side_effect=fake_fetch), \
             patch.object(self.browser, "_prompt_input", return_value="query"):
            self.browser._fetch("spartan://h/search")

        self.assertEqual(self.browser.current_url, "spartan://h/search")
        self.assertEqual(len(call_log), 2)
        self.assertEqual(call_log[0][1], b"")
        self.assertEqual(call_log[1][1], b"query")

    def test_kepler_input_round_trip(self):
        """Code 10 prompts, then re-fetches the URL with query encoded."""
        call_log = []

        def fake_fetch(url, data=b""):
            call_log.append(url)
            if "?" not in url:
                return (10, "Search?", b"", {})
            return (20, "text/gemini", b"# Results", {})

        with patch.object(self.browser, "_fetch_url", side_effect=fake_fetch), \
             patch.object(self.browser, "_prompt_input", return_value="hello"):
            self.browser._fetch("kepler://h/search")

        self.assertEqual(len(call_log), 2)
        self.assertIn("hello", call_log[1])

    def test_input_resets_redirect_counter(self):
        """After an input round-trip, the redirect budget should reset."""
        # Sequence: input → redirect → redirect → ... → success.
        # If the input didn't reset, we'd run out of budget.
        state = {"count": 0}

        def fake_fetch(url, data=b""):
            state["count"] += 1
            if data == b"":
                return (5, "Input?", b"", {})
            # After input, do a few redirects then succeed.
            if state["count"] < 5:
                return (3, f"/step{state['count']}", b"", {})
            return (2, "text/gemini", b"# Done", {})

        with patch.object(self.browser, "_fetch_url", side_effect=fake_fetch), \
             patch.object(self.browser, "_prompt_input", return_value="data"):
            self.browser._fetch("spartan://h/")

        self.assertEqual(self.browser.current_url and
                         self.browser.current_url.startswith("spartan://h/"), True)


# ════════════════════════════════════════════════════════════════════════════
# Tab completer
# ════════════════════════════════════════════════════════════════════════════

class TestCompleter(TempConfigMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tempdir()
        self._stdout_patch = patch("sys.stdout", new_callable=io.StringIO)
        self._stdout_patch.start()
        self.browser = br.Browser()
        self.completer = br.BrowserCompleter(self.browser)

    def tearDown(self):
        self._stdout_patch.stop()
        self._teardown_tempdir()

    def _collect(self, text, line_buffer):
        with patch("readline.get_line_buffer", return_value=line_buffer):
            matches = []
            i = 0
            while True:
                m = self.completer.complete(text, i)
                if m is None:
                    break
                matches.append(m)
                i += 1
        return matches

    def test_complete_command_prefix(self):
        matches = self._collect("bo", "bo")
        self.assertIn("bookmark", matches)
        self.assertIn("bookmarks", matches)

    def test_complete_no_match(self):
        self.assertEqual(self._collect("zzz", "zzz"), [])

    def test_complete_set_options(self):
        matches = self._collect("p", "set p")
        self.assertIn("pager", matches)

    def test_complete_set_all_options(self):
        matches = self._collect("", "set ")
        # Empty text returns nothing (by design — avoids spam)
        self.assertEqual(matches, [])

    def test_complete_go_with_bookmarks(self):
        self.browser.bookmarks = {"home": "gemini://example.org/"}
        matches = self._collect("gemini", "go gemini")
        self.assertIn("gemini://example.org/", matches)

    def test_complete_delbm(self):
        self.browser.bookmarks = {"home": "gemini://h/", "work": "kepler://w/"}
        matches = self._collect("h", "delbm h")
        self.assertIn("home", matches)
        self.assertNotIn("work", matches)


# ════════════════════════════════════════════════════════════════════════════
# REPL command dispatch
# ════════════════════════════════════════════════════════════════════════════

class TestREPLCommands(TempConfigMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tempdir()
        self._stdout_patch = patch("sys.stdout", new_callable=io.StringIO)
        self._stdout_patch.start()
        self.browser = br.Browser()
        self.commands = br._make_command_table(self.browser)

    def tearDown(self):
        self._stdout_patch.stop()
        self._teardown_tempdir()

    def test_command_table_completeness(self):
        # Every name in BrowserCompleter.COMMANDS except quit aliases
        # should resolve in the dispatch table.
        quits = {"quit", "q", "exit", "bye"}
        expected = set(br.BrowserCompleter.COMMANDS) - quits
        # The dispatch table covers all of these
        missing = expected - set(self.commands)
        self.assertFalse(missing, f"Missing dispatch entries: {missing}")

    def test_help_command(self):
        # Should not crash
        self.commands["help"]("", "")
        self.commands["?"]("", "")

    def test_go_with_url(self):
        with patch.object(self.browser, "navigate") as mock_nav:
            self.commands["go"]("gemini://h/", "")
        mock_nav.assert_called_once_with("gemini://h/")

    def test_go_empty(self):
        with patch.object(self.browser, "navigate") as mock_nav:
            self.commands["go"]("", "")
        mock_nav.assert_not_called()

    def test_go_invalid(self):
        with patch.object(self.browser, "navigate") as mock_nav:
            self.commands["go"]("just-a-word", "")
        mock_nav.assert_not_called()

    def test_back_command(self):
        with patch.object(self.browser, "go_back") as mock:
            self.commands["back"]("", "")
        mock.assert_called_once()

    def test_forward_command(self):
        with patch.object(self.browser, "go_forward") as mock:
            self.commands["forward"]("", "")
        mock.assert_called_once()

    def test_reload_command(self):
        with patch.object(self.browser, "reload") as mock:
            self.commands["reload"]("", "")
        mock.assert_called_once()

    def test_up_command(self):
        with patch.object(self.browser, "go_up") as mock:
            self.commands["up"]("", "")
        mock.assert_called_once()

    def test_finger_command(self):
        with patch.object(self.browser, "finger_query") as mock:
            self.commands["finger"]("alice@h", "")
        mock.assert_called_once_with("alice@h")

    def test_home_command(self):
        with patch.object(self.browser, "navigate") as mock:
            self.commands["home"]("", "")
        mock.assert_called_once_with(self.browser.HOME)

    def test_links_command(self):
        with patch.object(self.browser, "show_links") as mock:
            self.commands["links"]("filter", "")
        mock.assert_called_once_with("filter")

    def test_find_command(self):
        with patch.object(self.browser, "find") as mock:
            self.commands["find"]("hello", "world")
        mock.assert_called_once_with("hello world")

    def test_find_empty(self):
        with patch.object(self.browser, "find") as mock:
            self.commands["find"]("", "")
        mock.assert_not_called()

    def test_source_command(self):
        with patch.object(self.browser, "show_source") as mock:
            self.commands["source"]("", "")
        mock.assert_called_once()

    def test_url_command(self):
        with patch.object(self.browser, "show_url") as mock:
            self.commands["url"]("", "")
        mock.assert_called_once()

    def test_save_command(self):
        with patch.object(self.browser, "save_page") as mock:
            self.commands["save"]("filename", "")
        mock.assert_called_once_with("filename")

    def test_history_command(self):
        with patch.object(self.browser, "show_history") as mock:
            self.commands["history"]("", "")
        mock.assert_called_once()

    def test_delh_command(self):
        with patch.object(self.browser, "history_delete") as mock:
            self.commands["delh"]("3", "")
        mock.assert_called_once_with("3")

    def test_delh_empty(self):
        with patch.object(self.browser, "history_delete") as mock:
            self.commands["delh"]("", "")
        mock.assert_not_called()

    def test_clearhistory_command(self):
        with patch.object(self.browser, "history_clear") as mock:
            self.commands["clearhistory"]("", "")
        mock.assert_called_once()

    def test_bookmark_command(self):
        with patch.object(self.browser, "bookmark_add") as mock:
            self.commands["bookmark"]("name", "")
        mock.assert_called_once_with("name")

    def test_bookmarks_command(self):
        with patch.object(self.browser, "bookmark_picker") as mock:
            self.commands["bookmarks"]("", "")
        mock.assert_called_once()

    def test_open_with_arg(self):
        with patch.object(self.browser, "bookmark_open") as mock:
            self.commands["open"]("2", "")
        mock.assert_called_once_with("2")

    def test_open_no_arg_uses_picker(self):
        with patch.object(self.browser, "bookmark_picker") as mock:
            self.commands["open"]("", "")
        mock.assert_called_once()

    def test_delbm_command(self):
        with patch.object(self.browser, "bookmark_delete") as mock:
            self.commands["delbm"]("name", "")
        mock.assert_called_once_with("name")

    def test_delbm_empty(self):
        with patch.object(self.browser, "bookmark_delete") as mock:
            self.commands["delbm"]("", "")
        mock.assert_not_called()

    def test_set_command(self):
        with patch.object(self.browser, "set_option") as mock:
            self.commands["set"]("pager", "on")
        mock.assert_called_once_with("pager", "on")

    def test_set_missing_args(self):
        with patch.object(self.browser, "set_option") as mock:
            self.commands["set"]("pager", "")
        mock.assert_not_called()

    def test_clear_command(self):
        with patch.object(br, "clear_screen") as mock:
            self.commands["clear"]("", "")
        mock.assert_called_once()

    def test_aliases_share_implementation(self):
        # back / b / prev all point to the same callable
        self.assertIs(self.commands["back"], self.commands["b"])
        self.assertIs(self.commands["back"], self.commands["prev"])
        self.assertIs(self.commands["forward"], self.commands["f"])
        self.assertIs(self.commands["forward"], self.commands["fwd"])


class TestRunREPL(TempConfigMixin, unittest.TestCase):
    """End-to-end-ish tests for run_repl, driving it via stdin."""

    def setUp(self):
        self._setup_tempdir()
        self._stdout_patch = patch("sys.stdout", new_callable=io.StringIO)
        self._stdout_patch.start()

    def tearDown(self):
        self._stdout_patch.stop()
        self._teardown_tempdir()

    def _run_with_inputs(self, inputs):
        """Drive run_repl with a scripted sequence of input lines."""
        it = iter(inputs)

        def fake_input():
            try:
                return next(it)
            except StopIteration:
                raise EOFError

        with patch("builtins.input", side_effect=fake_input):
            br.run_repl()

    def test_quit_clean_exit(self):
        self._run_with_inputs(["quit"])
        # If we got here, run_repl exited cleanly.

    def test_eof_clean_exit(self):
        self._run_with_inputs([])

    def test_empty_lines_ignored(self):
        self._run_with_inputs(["", "  ", "quit"])

    def test_unknown_command_treated_as_url(self):
        with patch.object(br.Browser, "navigate") as mock_nav:
            self._run_with_inputs(["gemini://example.org/", "quit"])
        mock_nav.assert_called()

    def test_digit_input_opens_link(self):
        with patch.object(br.Browser, "open_link") as mock_open:
            self._run_with_inputs(["3", "quit"])
        mock_open.assert_called_once_with("3")

    def test_help_does_not_crash(self):
        self._run_with_inputs(["help", "quit"])

    def test_keyboard_interrupt_continues(self):
        """Ctrl-C at the prompt should not exit the REPL."""
        responses = iter([KeyboardInterrupt(), "quit"])

        def fake_input():
            r = next(responses)
            if isinstance(r, BaseException):
                raise r
            return r

        with patch("builtins.input", side_effect=fake_input):
            br.run_repl()  # should exit on "quit", not on the interrupt

    def test_start_url_navigated(self):
        with patch.object(br.Browser, "navigate") as mock_nav, \
             patch("builtins.input", side_effect=EOFError):
            br.run_repl(start_url="gemini://example.org/")
        mock_nav.assert_called_once_with("gemini://example.org/")

    def test_start_url_invalid(self):
        # Invalid start URL should print a warning and continue to REPL
        with patch("builtins.input", side_effect=EOFError):
            br.run_repl(start_url="just-a-word")


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)