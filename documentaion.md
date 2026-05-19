# babelTUI

> A multi-protocol terminal browser for the **smolnet** — speaks Kepler, Gemini, Spartan, Nex, Gopher, and Finger fluently, with a clean text UI, TOFU security, and zero runtime dependencies.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Pure stdlib](https://img.shields.io/badge/dependencies-stdlib%20only-green.svg)](https://docs.python.org/3/library/)
[![Protocols](https://img.shields.io/badge/protocols-7-purple.svg)](#supported-protocols)
[![Single file](https://img.shields.io/badge/distribution-single%20file-orange.svg)](#installation)

---

## Table of Contents

1. [Overview](#overview)
2. [Philosophy & Design Goals](#philosophy--design-goals)
3. [Supported Protocols](#supported-protocols)
4. [Installation](#installation)
5. [Quick Start](#quick-start)
6. [The REPL Interface](#the-repl-interface)
7. [Command Reference](#command-reference)
8. [Command-Line Flags](#command-line-flags)
9. [Configuration](#configuration)
10. [Data Files & Persistence](#data-files--persistence)
11. [Security Model](#security-model)
12. [Rendering Engine](#rendering-engine)
13. [Protocol Implementation Notes](#protocol-implementation-notes)
14. [Architecture](#architecture)
15. [Keyboard Reference](#keyboard-reference)
16. [Worked Examples](#worked-examples)
17. [Troubleshooting](#troubleshooting)
18. [Performance & Limits](#performance--limits)
19. [Compatibility](#compatibility)
20. [Project Status & Roadmap](#project-status--roadmap)
21. [Contributing](#contributing)
22. [License](#license)

---

## Overview

**babelTUI** is a single-file, dependency-free terminal browser written in modern Python. It provides a unified interactive interface to seven small-internet ("smolnet") protocols, with the following headline features:

- 🔐 **TOFU certificate pinning** for TLS-secured schemes
- 🎨 **Rich gemtext rendering** with semantic colour, wrapping, and link extraction
- 📚 **Persistent bookmarks and history** with interactive pickers
- 🔍 **In-page incremental search** with match highlighting
- ⌨️ **Tab completion** for commands, bookmarks, and recent URLs
- 🔄 **Automatic Gopher menu detection** with conversion to gemtext
- 📥 **Binary content handling** with safe save prompts
- 🌐 **Spartan input prompts** and **Kepler 1x/3x/7x** status handling
- 📜 **Atomic, permission-restricted state files** for safety on shared systems

The entire implementation lives in one `.py` file and depends only on the Python standard library — no `pip install`, no virtual environments, no surprises.

---

## Philosophy & Design Goals

babelTUI is designed around four principles:

1. **Single-file portability.** The browser is one Python script. Drop it on any machine with Python 3.10+ and it works. There is no install step, no package, no compiled extension.
2. **Standard library only.** All networking, TLS, rendering, persistence, terminal control, and tab completion is implemented using stdlib modules. This keeps the dependency surface zero and the supply-chain risk minimal.
3. **Protocol fidelity.** Each protocol is implemented from its specification, including edge cases like Kepler's optional metadata triple, Spartan's request-body input flow, and Gopher's full item-type table.
4. **Boring, predictable security.** TOFU fingerprint pinning, automatic permission hardening on state files, atomic writes, and explicit warnings for plaintext downgrades. No magic, no surprises.

---

## Supported Protocols

| Scheme              | Default Port | Transport     | Spec / Notes                                            |
| ------------------- | ------------ | ------------- | ------------------------------------------------------- |
| `kepler://`         | 2009         | TCP           | Plaintext Kepler                                        |
| `keplers://`        | 10009        | TLS ≥ 1.2     | Encrypted Kepler with TOFU pinning                      |
| `gemini://`         | 1965         | TLS           | Full Gemini protocol with TOFU pinning                  |
| `spartan://`        | 300          | TCP           | Status codes 2/3/4/5, with input bodies for code 5      |
| `nex://`            | 1900         | TCP           | MIME guessed from path extension                        |
| `gopher://`         | 70           | TCP           | Auto-detects menus, converts to gemtext                 |
| `gopher-search://`  | 70           | TCP           | Synthetic scheme for type-7 search items                |
| `finger://`         | 79           | TCP           | Decorated rendering of standard finger fields           |

`telnet://` is recognised in Gopher menus and rendered as a link, but no session launcher is bundled.

---

## Installation

### Requirements

- **Python 3.10 or newer** (the code uses PEP 604 union types and structural pattern features available in 3.10+)
- A POSIX terminal or Windows console with ANSI escape support (Windows Terminal, ConEmu, modern PowerShell)
- TLS 1.2+ for `keplers://`/`gemini://` (typically provided by the OpenSSL bundled with your Python)

### Install Methods

**Run directly from a clone:**

```bash
git clone https://github.com/<your-user>/babelTUI.git
cd babelTUI
chmod +x babeltui.py
./babeltui.py
```

**Install to `$PATH`:**

```bash
install -m 755 babeltui.py ~/.local/bin/babeltui
babeltui
```

**Pipe-from-URL (advanced, audit before doing this):**

```bash
curl -fsSL https://example.org/babeltui.py -o ~/.local/bin/babeltui
chmod +x ~/.local/bin/babeltui
```

### Optional Setup

- Set `$PAGER` to your preferred pager (defaults to `less -R`, falls back to `more`).
- Set `$LANG` correctly so the Kepler language tag (per §8.1.2) is sent. The browser will read `LANG`, split on `.`, and convert `_` to `-` (e.g. `en_US.UTF-8` → `en-US`). `C`/`POSIX` are sent as `?`.

---

## Quick Start

```bash
# Launch the interactive REPL
./babeltui.py

# Open a specific URL on startup
./babeltui.py gemini://geminiprotocol.net/

# Finger a user (shorthand)
./babeltui.py user@example.org

# Start with pager enabled and colors disabled
./babeltui.py --pager --no-color

# Override home temporarily
./babeltui.py --home gemini://geminiprotocol.net/
```

Once inside the REPL, type `help` (or `?`) at any time for the full command reference.

---

## The REPL Interface

### Prompt Anatomy

The prompt encodes information about your current state:

```
keplers:example.org/path [3/7] ❯
└─┬───┘ └─────────┬────┘ └─┬─┘
  │              │         │
  scheme         netloc+path   history position / total
```

- The scheme is colour-coded: `keplers` is green (encrypted), other schemes are cyan.
- Long URLs are intelligently truncated, preserving the netloc and the tail of the path.
- The history counter shows your current position, useful when navigating with `back` / `forward`.

When no page is loaded, the prompt is simply `browser ❯`.

### Input Modes

babelTUI has four input modes:

| Mode             | Trigger                              | Exit                |
| ---------------- | ------------------------------------ | ------------------- |
| **Command**      | Default REPL state                   | `quit`              |
| **Find**         | `find <term>` or `/<term>`           | `q` or `ESC`        |
| **Picker**       | `history`, `bookmarks`               | `q`, `ESC`, `Enter` |
| **Input prompt** | Server-issued (Gemini 1x, Spartan 5) | Type or `Ctrl-C`    |

Find and picker modes use raw single-keypress input — no Enter required for navigation.

### Tab Completion

Tab completion is context-sensitive:

- At the start of a line: completes commands (`go`, `bookmark`, `history`, …)
- After `go`, `visit`, `g`, `open`, `ob`: completes against bookmark URLs and recent history
- After `delbm`, `rmbm`: completes against bookmark names
- After `set`: completes against configurable options

---

## Command Reference

<details open>
<summary><b>Navigation</b></summary>

| Command                           | Aliases                  | Description                                  |
| --------------------------------- | ------------------------ | -------------------------------------------- |
| `go <url>`                        | `visit`, `navigate`, `g` | Navigate to any supported URL                |
| `<number>`                        | —                        | Follow link N on the current page            |
| `back`                            | `b`, `prev`              | Go back one entry in history                 |
| `forward`                         | `f`, `fwd`, `next`       | Go forward one entry in history              |
| `up`                              | `..`                     | Ascend one path segment                      |
| `reload`                          | `r`, `refresh`           | Re-fetch the current page (bypasses 7x)      |
| `home`                            | —                        | Open the configured home URL                 |
| `finger <user@host>`              | —                        | Issue a finger query                         |

**URL normalisation rules** (applied to bare input that isn't a known command):

- Anything containing `://` is taken as-is.
- `user@host` is rewritten to `finger://host/user`.
- A bare host with a `.` or `:`, or the literal `localhost`, is prefixed with `spartan://`.
- Anything else raises an "ambiguous input" error.

</details>

<details>
<summary><b>Page Operations</b></summary>

| Command                  | Aliases       | Description                                    |
| ------------------------ | ------------- | ---------------------------------------------- |
| `links [pattern]`        | `l`, `ls`     | List all links; optional substring filter      |
| `find <term>`            | `/`           | Interactive in-page search with highlight      |
| `source`                 | —             | Show the raw response body                     |
| `save [filename]`        | —             | Save the current page to disk                  |
| `url`                    | —             | Print the full URL of the current page         |

`save` without an argument derives a filename from the last path segment of the URL; existing files trigger an overwrite prompt.

</details>

<details>
<summary><b>History & Bookmarks</b></summary>

| Command                  | Aliases               | Description                                    |
| ------------------------ | --------------------- | ---------------------------------------------- |
| `history`                | `hist`                | Open the interactive history picker            |
| `delh <n>`               | —                     | Delete history entry by index or exact URL     |
| `clearhistory`           | `clearhist`           | Purge all history (with confirmation)          |
| `bookmark [name]`        | `bm`, `mark`          | Bookmark the current page                      |
| `bookmarks`              | `bms`, `marks`        | Open the interactive bookmark picker           |
| `open <n\|name>`         | `ob`                  | Open a bookmark by index or name               |
| `delbm <name>`           | `rmbm`                | Delete a bookmark                              |

History size is capped by `history_limit` (default 500). When the cap is exceeded, the oldest entries are discarded. History is saved to disk every 10 navigations and on a clean exit.

</details>

<details>
<summary><b>Configuration</b></summary>

| Command                          | Description                                              |
| -------------------------------- | -------------------------------------------------------- |
| `set pager on\|off`              | Toggle paged output via `$PAGER` / `less` / `more`       |
| `set color on\|off`              | Toggle ANSI colour output                                |
| `set home <url>`                 | Change the home URL (validated)                          |
| `set timeout <secs>`             | Change the per-connection timeout (positive integer)     |
| `set history_limit <n>`          | Change the maximum history size (non-negative integer)   |

All `set` operations persist immediately to `~/.config/babeltui/config.json`. Boolean values accept `on`/`off`, `true`/`false`, `yes`/`no`, or `1`/`0`.

</details>

<details>
<summary><b>Miscellaneous</b></summary>

| Command                          | Description                                 |
| -------------------------------- | ------------------------------------------- |
| `clear`                          | Clear the terminal                          |
| `help` / `?` / `h`               | Display the help screen                     |
| `quit` / `q` / `exit` / `bye`    | Flush state and exit                        |

</details>

---

## Command-Line Flags

| Flag                       | Effect                                                  | Persists? |
| -------------------------- | ------------------------------------------------------- | --------- |
| `url` (positional)         | Open this URL on startup                                | No        |
| `--no-color`               | Disable ANSI colour output                              | Yes       |
| `--pager`                  | Pipe page content through `$PAGER` (or `less`/`more`)   | Yes       |
| `--home <url>`             | Override the configured home URL                        | Yes       |
| `--timeout <seconds>`      | Override the configured connection timeout              | Yes       |
| `--history-limit <n>`      | Override the configured history cap                     | Yes       |

> ⚠ **Note:** CLI flags that override configuration are written back to the config file. To use a flag for one session only, edit the config afterwards or set it back via `set`.

---

## Configuration

### Config File Schema

`~/.config/babeltui/config.json`:

```json
{
  "home": "spartan://mozz.us/",
  "timeout": 15,
  "history_limit": 500,
  "color": true,
  "pager": false
}
```

| Key             | Type    | Default                  | Notes                                                          |
| --------------- | ------- | ------------------------ | -------------------------------------------------------------- |
| `home`          | string  | `"spartan://mozz.us/"`   | URL opened by the `home` command                               |
| `timeout`       | int     | `15`                     | Per-connection socket timeout, in seconds                      |
| `history_limit` | int     | `500`                    | Maximum number of history entries retained                     |
| `color`         | bool    | `true`                   | Enable ANSI colour output                                      |
| `pager`         | bool    | `false`                  | Pipe rendered pages through a pager                            |

Unknown keys are silently ignored on load, so the file is forward-compatible. Missing keys fall back to their defaults.

### Environment Variables

| Variable | Effect                                                                |
| -------- | --------------------------------------------------------------------- |
| `PAGER`  | Pager command to use when paging is enabled (default `less -R`)       |
| `LANG`   | Source for the Kepler language tag sent per spec §8.1.2               |

---

## Data Files & Persistence

All persistent state lives under `~/.config/babeltui/`:

| File                | Purpose                                              | Permissions | Atomic? |
| ------------------- | ---------------------------------------------------- | ----------- | ------- |
| `config.json`       | User preferences                                     | `0600`      | ✓       |
| `bookmarks.json`    | Saved bookmarks (`name → URL`)                       | `0600`      | ✓       |
| `history.json`      | Recent navigation history                            | `0600`      | ✓       |
| `known_hosts.json`  | TOFU certificate fingerprints (`host → SHA-256`)     | `0600`      | ✓       |

### Atomic Writes

Every persistent file is written through a write-then-rename sequence: data goes to `<file>.tmp`, is chmodded to `0600`, then atomically renamed over the target via `os.replace`. This means an interrupted write (`Ctrl-C`, power loss, OOM kill) leaves either the old or new file intact — never a half-written one.

### Manual Inspection

All four files are plain JSON, indented for readability, and use UTF-8 with non-ASCII characters preserved (`ensure_ascii=False`). You can edit them by hand while the browser is closed, e.g. to bulk-import bookmarks:

```json
{
  "Gemini Home": "gemini://geminiprotocol.net/",
  "Mozz Spartan": "spartan://mozz.us/"
}
```

---

## Security Model

### TOFU Certificate Pinning

For TLS-secured schemes (`keplers://`, `gemini://`), babelTUI uses **Trust-On-First-Use** rather than the public CA system. This is the convention for smolnet protocols, which are typically served from self-signed certificates.

The TOFU flow:

1. **First contact:** the server's certificate SHA-256 fingerprint is recorded in `known_hosts.json` keyed by hostname. The user is not prompted (this is the "trust on first use" part — TOFU only protects subsequent connections).
2. **Subsequent connections:** the live fingerprint is compared against the stored one. Match → silent success.
3. **Mismatch in an interactive session:** the user sees a red warning with both fingerprints and an explicit yes/no prompt. Default is "no".
4. **Mismatch in a non-interactive session** (stdin not a TTY): always fatal.

> ⚠ **Caveat:** chain verification and expiry checks are intentionally disabled (`CERT_NONE`). The fingerprint pin is the single source of trust. This is appropriate for smolnet hosts but is **not** the security model used for HTTPS. Do not reuse this code path for general-purpose web traffic.

### Plaintext-Downgrade Warnings

babelTUI warns and requires explicit confirmation before:

- Following a link from a `keplers://` page to a plaintext `kepler://` URL.
- Following a server-issued redirect from `keplers://` to `kepler://`.

This implements Kepler specification §4.7.1.

### Input Privacy

For Kepler status code `11` (sensitive input), the prompt uses `getpass.getpass()`:

- Input is **not echoed** to the terminal.
- Input is **not written to readline history**.
- Input is **not visible in `ps`** (the password never enters the URL on the command line).

For non-sensitive Gemini/Kepler 1x prompts, input is read normally but is still percent-encoded into the URL using a strict safe-set (`safe=b""`), so all special characters are escaped.

### State File Permissions

All persistent files are chmodded to `0600` (owner read/write only) on every write. On platforms where `chmod` is unsupported (some Windows configurations), this step is silently skipped.

### Defensive Limits

| Limit                     | Value      | Rationale                                  |
| ------------------------- | ---------- | ------------------------------------------ |
| Kepler URI length         | 1024 bytes | Spec §2.2.2                                |
| Gemini URI length         | 1024 bytes | Spec compliance                            |
| Kepler header read        | 8192 bytes | Bound runaway server responses             |
| Gemini header read        | 4096 bytes | Bound runaway server responses             |
| Spartan header read       | 4096 bytes | Bound runaway server responses             |
| Max redirects (general)   | 10         | Catch redirect loops early                 |
| Max redirects (Kepler)    | 5          | Tighter cap per Kepler convention          |

A visited-set is also maintained per fetch invocation to detect simple A→B→A loops, with one important exception: if the next request has a non-empty body (Spartan code-5 input), the visited check is skipped, because a request with input is semantically distinct from a previous bodyless fetch of the same URL.

---

## Rendering Engine

### Gemtext

The gemtext renderer (used for `text/gemini` and most Nex/Gopher content) produces a two-part output: a list of styled display lines and a list of `(label, url)` link tuples. Recognised line types:

| Prefix     | Rendered as                                              |
| ---------- | -------------------------------------------------------- |
| `#`        | Yellow bold heading with underline                       |
| `##`       | Bright-green bold subheading                             |
| `###`      | Green sub-subheading                                     |
| `=> url`   | Numbered link, with full URL on a dim continuation line  |
| `* item`   | Bullet point with cyan glyph                             |
| `> quote`  | Indented dim quote with vertical bar                     |
| ` ``` `    | Toggles preformatted block; framed in dim border         |
| (other)    | Word-wrapped to terminal width with 2-space indent       |

Links to supported schemes are styled in bright blue; external (unsupported) schemes show in yellow as a hint that selecting them will only print the URL rather than fetch it.

Word wrapping uses `textwrap.wrap` with `break_long_words=True` and `break_on_hyphens=False`, which preserves readability for URLs and identifiers while still hard-breaking any token longer than the terminal width.

### Gopher

Gopher responses are inspected with a heuristic to decide menu vs. text:

1. UTF-8 decoded with replacement.
2. Last non-empty line must be `.` (the canonical terminator).
3. At least 70% of non-empty lines must start with a valid item-type character and contain a tab.

If detected as a menu, the body is rewritten to gemtext, mapping item types to appropriate link schemes:

| Item type | Meaning              | Mapped to                       |
| --------- | -------------------- | ------------------------------- |
| `i`       | Info text            | Plain line (no link)            |
| `0`, `1`  | Text / directory     | `gopher://`                     |
| `7`       | Search server        | `gopher-search://` with 🔍 hint |
| `h`       | HTML/web link        | URL from selector (`URL:` prefix stripped) |
| `8`, `T`  | Telnet / TN3270      | `telnet://`                     |
| Others    | Binary / image / etc | `gopher://` with 📎 and type description |

### Finger

Finger responses get light-touch decoration: known field names (`Login`, `Name`, `Mail`, `Shell`, `Directory`, `Office`, `Phone`, `Plan`, `Project`, `On since`, `Last login`, `No mail`, `New mail`) are highlighted in cyan bold. The header line shows `user@host` in bright cyan.

### Binary Content

If a body can't be decoded as text (or has an explicit non-`text/*` MIME type), the user is offered a save prompt with a sensible default filename derived from the URL. Existing files trigger an overwrite confirmation.

### Pager Integration

When `pager` is enabled (or `--pager` is passed), rendered content is piped to `$PAGER` (or `less -R` / `more` if `$PAGER` is unset). `-R` is important: it preserves ANSI colour codes so the styled output remains readable in the pager. Broken pipes (user quitting the pager mid-stream) are silently handled.

---

## Protocol Implementation Notes

### Kepler

- Plaintext (`kepler://`) and TLS (`keplers://`) variants share a parser.
- The request line is `<URL> <last_cached> <language>\r\n` per §3.1.2.
- The success status line allows an optional numeric metadata triple (`content_length`, `last_updated`, `expires`) before the MIME type. The parser consumes up to three leading numeric tokens, then treats the rest as the MIME type. This means both `20 text/gemini` and `20 1024 1700000000 -1 text/gemini` are handled correctly.
- Status code 11 triggers a hidden-input prompt (sensitive input).
- Status codes 70–79 ("unchanged") are detected and reported; since the browser doesn't cache yet, the user is told to `reload` to force a fetch.
- Status codes 60–69 (client certificates) are reported with a "not yet supported" notice.

### Gemini

- Strict 2-digit status codes per spec.
- TOFU pinning with optional TLS 1.2+ minimum.
- Status 1x triggers an input prompt; the response is percent-encoded into the query string.
- Status 3x triggers a redirect with the standard 10-hop cap.

### Spartan

- Request format: `<host> <path> <content_length>\r\n[body]`.
- Hostnames are IDNA-encoded; paths are normalised through `unquote_to_bytes` then `quote_from_bytes` to produce canonical percent-encoding.
- Status 5 ("input required") prompts the user, encodes the response as UTF-8 bytes, and re-issues the request with that body. The redirect counter is reset for this transition because submitting input is new user intent.

### Nex

- Trivial request: `<path>\r\n`.
- Empty response → synthetic error code 4.
- MIME type is guessed from the path extension; a path ending in `/` or having no extension is assumed to be `text/gemini` (the common case for index pages).

### Gopher

- Selector is path-minus-leading-slash; type-7 items add a tab-separated query.
- Auto-detection of menu vs. text uses the heuristic described above.
- Type-8/T items render as `telnet://` links but don't launch a session.

### Finger

- Request is either the path-as-user or the URL's userinfo component.
- Empty response → synthetic error code 4.

---

## Architecture

<details>
<summary><b>Click to expand the architecture deep-dive</b></summary>

### File Layout

The entire browser is one Python file, organised into clearly-labelled sections by comment banners:

1. **URL scheme registration** — patches `urllib.parse` so smolnet schemes participate in RFC 3986 relative resolution.
2. **Constants** — ports, limits, file paths.
3. **Atomic JSON I/O** — write-then-rename helpers.
4. **Config dataclass** — typed configuration with `from_dict` / `to_dict`.
5. **Language detection** — Kepler §8.1.2 implementation.
6. **TOFU helpers** — known-hosts persistence and fingerprint comparison.
7. **ANSI colour helpers** — a small set of named colour functions.
8. **Terminal helpers** — width/height, clear-screen, horizontal rule.
9. **Pager support** — `$PAGER` discovery and pipe handling.
10. **Bookmark / history persistence** — JSON load/save.
11. **Network helpers** — socket utilities, TLS context, TOFU check.
12. **Protocol fetchers** — one function per scheme.
13. **Renderers** — gemtext, finger, Gopher-to-gemtext, plain text.
14. **Find mode** — single-keypress interactive search.
15. **Interactive picker** — generic for history and bookmarks.
16. **Tab completion** — readline integration.
17. **Browser class** — REPL state, dispatch tables, fetch loop.
18. **REPL** — command table, main loop, banner.
19. **Entry point** — argument parsing.

### Dispatch Tables

`Browser.__init__` builds two dictionaries:

- `_fetchers`: scheme → fetch wrapper. Wraps the protocol-specific function into a uniform `(code, meta, body, extras)` signature.
- `_response_handlers`: scheme → handler. Interprets the response and decides what happens next.

Adding a new protocol is therefore a matter of:

1. Writing a `fetch_<scheme>()` function.
2. Writing a wrapper that returns the uniform tuple.
3. Writing a `_handle_<scheme>()` method that returns a `HandlerResult`.
4. Registering both in the dispatch tables.

### The Fetch Loop

`Browser._fetch()` is a single iterative loop that drives the entire fetch state machine. Handlers communicate with the loop by returning a `HandlerResult` dataclass:

```python
@dataclass
class HandlerResult:
    done: bool = True
    redirect_to: Optional[str] = None
    new_url: Optional[str] = None
    reset_redirects: bool = False
    data: bytes = b""
```

| Field             | Purpose                                                                  |
| ----------------- | ------------------------------------------------------------------------ |
| `done`            | Stop the loop if `True`                                                  |
| `redirect_to`     | URL to fetch next                                                        |
| `new_url`         | Replace `current_url` (used by input substitution)                       |
| `reset_redirects` | Reset depth counter and visited set (new user intent, e.g. input submit) |
| `data`            | Request body bytes for the next fetch (e.g. Spartan code-5 payload)      |

This cleanly separates protocol semantics (in the handler) from loop control (in `_fetch`), supports redirect loops and input substitution uniformly, and keeps each handler readable in isolation.

### URL Scheme Registration

At import time, the script extends `urllib.parse.uses_relative` and `uses_netloc` to include all smolnet schemes. Without this, `urljoin()` returns the bare relative reference for unknown schemes, breaking server redirects like `30 /index.gmi`.

This pokes at module internals, which is acknowledged in the source — they've been stable since Python 2.x and are effectively API. `resolve_url()` also contains a defensive manual fallback in case a future stdlib version changes behaviour.

### Renderer Pipeline

The renderer always produces two artefacts that are stored on `Browser`:

- `current_render_lines`: pre-styled strings ready to print (used by find mode and the pager).
- `current_links`: a list of `(label, url)` tuples (used by numeric link selection and `links`).

Find mode operates on `current_render_lines`, using a regex to strip ANSI for matching but preserving the styled originals for display.

### Single-Keypress Input

`getch()` provides cross-platform single-keypress reading:

- On Windows, uses `msvcrt.getwch` and maps the extended-key prefix bytes to ANSI escape sequences for uniformity.
- On POSIX, switches the TTY to raw mode via `termios` + `tty` and reads escape sequences directly, using `select` to disambiguate `ESC` from `ESC [ ...`.

This is what powers find mode and the interactive pickers — no Enter required for navigation.

</details>

---

## Keyboard Reference

### Find Mode

| Key                | Action                |
| ------------------ | --------------------- |
| `n`, `↓`, `→`      | Next match            |
| `p`, `↑`, `←`      | Previous match        |
| `q`, `ESC`, `Ctrl-C` | Exit find mode      |

### Interactive Picker (history, bookmarks)

| Key                | Action                                       |
| ------------------ | -------------------------------------------- |
| `↑` / `↓`          | Move cursor                                  |
| `0`–`9`            | Build a jump-to number                       |
| `Backspace`        | Erase one digit from the jump buffer         |
| `Enter`            | Open the cursor entry (or the jump target)   |
| `q`, `ESC`, `Ctrl-C` | Cancel                                     |

---

## Worked Examples

### Basic Browsing

```text
browser ❯ go gemini://geminiprotocol.net/
gemini:geminiprotocol.net/ [1/1] ❯ 3                  # follow link #3
gemini:geminiprotocol.net/news/ [2/2] ❯ back
gemini:geminiprotocol.net/ [1/2] ❯ forward
gemini:geminiprotocol.net/news/ [2/2] ❯ up
gemini:geminiprotocol.net/ [3/3] ❯
```

### Finger

```text
browser ❯ finger user@example.org
browser ❯ finger://example.org/admin
browser ❯ user@example.org      # bare input also works
```

### Bookmarks

```text
gemini:geminiprotocol.net/ [3/3] ❯ bookmark Gemini Home
  ✓ Bookmarked: Gemini Home → gemini://geminiprotocol.net/
gemini:geminiprotocol.net/ [3/3] ❯ bookmarks
  # interactive picker opens; cursor or jump-by-number, Enter to open
```

### In-Page Search

```text
gemini:example.org/spec [4/4] ❯ find specification
  Found 7 match(es) — entering find mode…
  # screen redraws with current match highlighted
  # n/p (or arrows) to step, q to exit
```

### Spartan Input

```text
spartan:mozz.us/echo [1/1] ❯ go spartan://mozz.us/echo
  ⟳  spartan://mozz.us/echo
  5  Enter text to echo

  ?  Enter text to echo
  ❯ hello, world
  20  text/gemini

  hello, world
```

### Save a Page

```text
gemini:example.org/doc.gmi [5/5] ❯ save
  doc.gmi exists. Overwrite? [y/N]: y
  ✓  Saved 4,237 bytes → doc.gmi
```

### Filter Links

```text
gemini:example.org/ [6/6] ❯ links spec
  Links on this page:

  [3] Gemini specification
       gemini://geminiprotocol.net/docs/specification.gmi
  [11] Spartan specification
       spartan://mozz.us/spartan-spec
```

---

## Troubleshooting

### "Certificate fingerprint mismatch"

Either:
- The server legitimately rotated its certificate, in which case accept the new fingerprint when prompted.
- Something is wrong (MITM, DNS hijack, expired cert replaced silently) — in which case **don't** accept it. You can manually remove the host from `~/.config/babeltui/known_hosts.json` to re-establish trust on next contact.

### "Too many redirects"

The browser caps redirects at 10 (5 for Kepler). If you hit this, the server likely has a redirect loop; try the URL in another client to confirm.

### "Empty response from \<protocol\> server"

The server closed the connection without sending a status line. Common causes: protocol mismatch (e.g. talking Gemini to a Spartan port), firewall interference, or server crash.

### Colours not appearing

- Check that your terminal supports ANSI (`echo -e '\033[31mred\033[0m'` should appear red).
- Confirm `set color on` is set, or remove `"color": false` from `config.json`.
- Make sure you're not redirecting output: babelTUI auto-disables colour when stdout isn't a TTY.

### Tab completion not working

- Ensure your build of Python has `readline` (most do; some minimal Linux distros and macOS system Python may need `pyreadline3` or `libedit` configuration).
- On macOS, the code attempts `bind ^I rl_complete` for the `libedit` backend; if you're using GNU readline, this still falls through correctly.

### Pager garbles output

Set `PAGER` to `less -R` (or just unset it; the auto-detection will pick this up). Pagers without `-R`-style raw-control passthrough won't render ANSI colour correctly.

### Config file got corrupted

All four state files are loaded with `try/except`; a corrupt file is silently treated as empty. Delete the offending file in `~/.config/babeltui/` and restart.

---

## Performance & Limits

| Aspect                  | Behaviour                                                      |
| ----------------------- | -------------------------------------------------------------- |
| **Memory**              | Whole response body held in memory; no streaming render        |
| **Connection timeout**  | Configurable, default 15 s                                     |
| **Buffer size**         | 4096 bytes for socket reads                                    |
| **History save**        | Every 10 navigations + on clean exit                           |
| **TLS version**         | System default for Gemini; ≥ TLS 1.2 enforced for `keplers://` |
| **Concurrent fetches**  | None — strictly synchronous, one fetch at a time               |

There is no on-disk cache. Kepler 7x ("unchanged") responses are detected and reported but not honoured; the user must `reload` to force a fresh fetch.

---

## Compatibility

### Python Versions

- **3.10** — minimum supported.
- **3.11**, **3.12**, **3.13** — tested.
- **3.9 and earlier** — unsupported (PEP 604 union types are used).

### Operating Systems

| OS               | Status        | Notes                                                  |
| ---------------- | ------------- | ------------------------------------------------------ |
| Linux            | First-class   | Primary development target                             |
| macOS            | First-class   | `libedit`-readline handled                             |
| FreeBSD / OpenBSD | Supported    | Works with system Python                               |
| Windows 10/11    | Supported     | Use Windows Terminal or another ANSI-capable console   |
| Termux (Android) | Supported     | Works with the Termux Python package                   |

### Terminal Emulators

Any emulator with ANSI escape support works. Tested with: xterm, alacritty, kitty, foot, GNOME Terminal, Konsole, iTerm2, Windows Terminal, tmux/screen.

---

## Project Status & Roadmap

### Not Yet Implemented

| Feature                                | Status            |
| -------------------------------------- | ----------------- |
| Client certificates (Kepler 60–62, Gemini 6x) | Reported but not handled |
| Response caching (Kepler 7x)           | Detected, not honoured |
| Telnet/TN3270 session launching        | Rendered as links only |
| Mouse support                          | Not planned       |
| Multi-tab browsing                     | Not planned (out of scope for a single-file TUI) |
| Custom themes                          | Not planned (would inflate the source) |

### Implemented & Stable

- All seven protocols listed in [Supported Protocols](#supported-protocols)
- TOFU pinning with interactive mismatch handling
- Spartan input bodies (code 5)
- Kepler full status table including 1x input, 3x redirect, 7x unchanged
- Interactive history and bookmark pickers
- In-page find mode with highlighting
- Atomic, permission-hardened persistence
- Tab completion for commands, URLs, and bookmark names

---

## Contributing

Bug reports, protocol-conformance fixes, and renderer improvements are welcome. To preserve the project's design goals, please ensure your contribution:

- ✅ **Adds no runtime dependencies** outside the Python standard library.
- ✅ **Stays in one file.** No splitting into a package; the single-file property is a feature.
- ✅ **Matches existing style.** PEP 8, type hints, dataclasses where appropriate, descriptive constant names.
- ✅ **Handles errors defensively.** No bare `except`; catch the narrowest exception that makes sense.
- ✅ **Updates this README** if you add user-facing functionality.

### Reporting Bugs

When filing an issue, please include:

1. Python version (`python --version`)
2. Operating system and terminal emulator
3. The exact URL or command that triggered the bug
4. The error message (if any), with `--no-color` if it includes ANSI escapes that mangle in your issue tracker

### Suggesting Protocols

New smolnet protocols are considered if they have a published specification and at least one running public server. See the [Architecture](#architecture) section for the dispatch-table pattern to follow.

---

## License

See `LICENSE` in the repository root.

---

<p align="center">
  <i>babelTUI — one terminal, seven protocols, zero dependencies.</i>
</p>