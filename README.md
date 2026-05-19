# 🌌 babelTUI

```
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
```

### *One terminal. Seven protocols. Zero dependencies. No JavaScript. No ads. No tracking. No corporate dashboards screaming for your attention.*

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Pure stdlib](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](https://docs.python.org/3/library/)
[![Single file](https://img.shields.io/badge/distribution-one_file-orange.svg)](#-installation)
[![Protocols](https://img.shields.io/badge/protocols-7-purple.svg)](#-supported-protocols)
[![Kepler 0.1c](https://img.shields.io/badge/kepler-0.1c-9cf.svg)](https://github.com/kevinboone/kepler-protocol)
[![Smolnet](https://img.shields.io/badge/smolnet-approved-ff69b4.svg)](#)
[![License](https://img.shields.io/badge/license-see_LICENSE-lightgrey.svg)](LICENSE)

---

## 💫 What is this?

**babelTUI** is a terminal browser for the parts of the internet that *still feel like the internet*.

You know the ones. Hand-rolled gemtext capsules. Gopherholes with ASCII art that's been there since 1998. Finger servers that just tell you what someone's up to today. Spartan boards where the rules fit on one line. And now — the new kid on the block — Kepler capsules, taking everything Gemini got right and patching the things that didn't scale.

That's the **smolnet** — sometimes "smallnet", always weird in the good way — and babelTUI speaks **seven** of its dialects from a single Python file you can drop on any machine and run.

No npm. No virtualenv. No Electron. No 400MB of `node_modules`. Just `python babeltui.py` and you're in.

---

## ⚡ Why babelTUI?

Because the existing browser landscape looks like this:

- **The Big Web™** — 200 trackers, six cookie banners, and a popup begging you to install an app, all to read 400 words.
- **One-protocol smolnet clients** — great, but now you've got `amfora` for Gemini, `lagrange` for Gemini-but-prettier, `bombadillo` for Gopher, a separate finger client, *and nothing yet for Kepler*.
- **babelTUI** — speaks `kepler://`, `keplers://`, `gemini://`, `spartan://`, `nex://`, `gopher://`, and `finger://` from one prompt. Click a Gopher menu item that points to a Gemini capsule and it just *works*.

### The pitch in five bullets

- 🪶 **Featherweight** — single Python file, no dependencies, runs anywhere Python 3.10 does
- 🔐 **TOFU pinning** — Trust-On-First-Use certificate verification, the way smolnet does TLS
- 🎨 **Pretty gemtext** — semantic ANSI colour, proper headings, bullet lists, framed code blocks
- 🧭 **Real navigation** — back/forward history, bookmarks, in-page search, link-by-number
- ⌨️ **Tab completion** — for commands, bookmarks, *and* recent URLs
- 🦊 **Cross-protocol linking** — Gopher menus get auto-rewritten to gemtext; everything just composes

---

## 🌐 Supported Protocols

| Scheme              | Port  | Transport  | Vibe                                                          |
| ------------------- | ----- | ---------- | ------------------------------------------------------------- |
| `kepler://`         | 2009  | TCP        | Plaintext [Kepler](https://github.com/kevinboone/kepler-protocol) — Gemini's scalable successor |
| `keplers://`        | 10009 | TLS ≥ 1.2  | Encrypted Kepler — full TOFU pinning                          |
| `gemini://`         | 1965  | TLS        | The big one. The capsule scene. You know.                     |
| `spartan://`        | 300   | TCP        | "Gemini but I don't even need TLS." Beautifully minimal.      |
| `nex://`            | 1900  | TCP        | Even simpler than Spartan. Just paths and bytes.              |
| `gopher://`         | 70    | TCP        | The original. Still standing. Still good.                     |
| `finger://`         | 79    | TCP        | "What's that person up to?" Since 1971.                       |

`gopher-search://` is a synthetic scheme used internally for Gopher type-7 search items. `telnet://` links in Gopher menus are rendered (and labelled) but won't launch a session — that's your job.

### 🛰️ A word on Kepler

[Kepler](https://github.com/kevinboone/kepler-protocol) is a **brand new** smolnet protocol — version 0.1c was published in May 2026 by Kevin Boone. It's derived from Gemini but fixes three things that were always going to hurt Gemini at scale:

- **Caching.** Kepler responses carry `content_length`, `last_updated`, and `expires` metadata in the status line, and the protocol has a proper "unchanged" status code (7x). Search crawlers no longer need to re-download every file in every capsule on every index pass.
- **Optional plaintext.** Gemini mandates TLS, which locks out retro-computing folks (try doing TLS on a 6502). Kepler offers both `kepler://` (plain) and `keplers://` (TLS), so a Z80 in your loft can serve a capsule too.
- **Language tags.** Clients send the user's preferred language with every request, so servers can return localised content without User-Agent sniffing or query-string hacks.

babelTUI implements Kepler 0.1c per spec, including the full status code table (1x input, 2x success with optional metadata triple, 3x redirect, 4x temporary failure, 5x permanent failure, 6x client cert, 7x unchanged), the language tag from `$LANG` per §8.1.2, and the §4.7.1 plaintext-downgrade warnings when following a link from a `keplers://` page to a `kepler://` one.

Live test capsules from the spec author:

- `kepler://larsthebear.me/` — plaintext
- `keplers://larsthebear.me/` — TLS

Point babelTUI at either and you're talking Kepler. The bones of a scalable smolnet, in a protocol you can implement in an afternoon.

---

## 📦 Installation

There's no installer. There's no package. There's a Python file. That's the install.

```bash
git clone https://github.com/<your-user>/babelTUI.git
cd babelTUI
chmod +x babeltui.py
./babeltui.py
```

Want it on your `$PATH`?

```bash
install -m 755 babeltui.py ~/.local/bin/babeltui
babeltui
```

### Requirements

- **Python 3.10+** (we use modern type-hint syntax)
- A terminal that understands ANSI (basically all of them in 2026)
- That's the entire list

No `pip install`. No virtualenv. No `requirements.txt`. The dependency section of this README is *empty on purpose.*

---

## 🚀 Quick Start

```bash
# Launch the REPL
./babeltui.py

# Open straight to a URL
./babeltui.py gemini://geminiprotocol.net/

# Try out the new Kepler protocol
./babeltui.py keplers://larsthebear.me/

# Finger someone (shorthand — no scheme needed)
./babeltui.py user@example.org

# Pager mode for the long-form posts
./babeltui.py --pager
```

Inside, type `help` or `?` to see everything. Or just start poking — `go`, `back`, `bookmark`, and numbers-to-follow-links cover 90% of browsing.

---

## 🎮 A Tour in 30 Seconds

```text
⟳  gemini://geminiprotocol.net/
20  text/gemini

▸ Project Gemini
════════════════════

Gemini is a new internet protocol which:

• Is heavier than gopher
• Is lighter than the web
• Will not replace either

[1] → Specification
gemini://geminiprotocol.net/docs/specification.gmi
[2] → Software
gemini://geminiprotocol.net/software/

gemini:geminiprotocol.net/ [1/1] ❯ 1
⟳  gemini://geminiprotocol.net/docs/specification.gmi
gemini:geminiprotocol.net/docs/specification.gmi [2/2] ❯ bookmark Gemini Spec
✓ Bookmarked: Gemini Spec → gemini://geminiprotocol.net/docs/specification.gmi
gemini:geminiprotocol.net/docs/specification.gmi [2/2] ❯ find TOFU
Found 4 match(es) — entering find mode…
```

That's it. That's the browser.

---

## 🧙 The Cool Stuff

<details open>
<summary><b>🔐 TOFU certificate pinning</b></summary>

The smolnet doesn't play the Public CA game. Capsules are self-signed and proud of it. babelTUI pins SHA-256 fingerprints on first contact and screams in bright red if they ever change without your say-so.

```
⚠  WARNING: Certificate fingerprint mismatch for example.org!
Expected: 5f3a...
Actual:   91b2...
Accept new certificate? [y/N]:
```

In non-interactive sessions, mismatch is *always* fatal. No silent downgrades.
</details>

<details>
<summary><b>🛰️ Full Kepler 0.1c support</b></summary>

Both `kepler://` (plaintext) and `keplers://` (TLS) are first-class citizens. The success-line metadata triple (`content_length` / `last_updated` / `expires`) is parsed defensively — minimal `20 text/gemini` and verbose `20 1024 1700000000 -1 text/gemini` both work. Status 7x ("unchanged") is detected and reported. Status 11 ("sensitive input") uses `getpass` so passwords never echo. Your language from `$LANG` is sent on every request per §8.1.2.

Per §4.7.1, following a link from `keplers://` to `kepler://` requires explicit confirmation — no silent downgrades from encrypted to plaintext.
</details>

<details>
<summary><b>🌉 Cross-protocol linking that actually works</b></summary>

A Gopher menu that links to a Gemini capsule? Click the number. Done. A gemtext page linking to a finger query? Same. A Kepler capsule with a `nex://` link to a Nex server? Yes, even that.

The seven protocols share one address bar, one history, one bookmark store. You don't switch tools to switch nets.
</details>

<details>
<summary><b>📑 Auto-rewriting Gopher menus to gemtext</b></summary>

Gopher menus are *technically* tab-separated tables of item-type characters. babelTUI detects them via heuristic (item-type prefix + tabs + terminating `.`) and quietly rewrites them as gemtext before rendering — so they get the same gorgeous link rendering and word-wrapping as any modern capsule.

Bonus: type-7 (search) items get a 🔍 hint, binaries get a 📎, and `URL:` selectors get unwrapped into real links.
</details>

<details>
<summary><b>🔍 Interactive find mode</b></summary>

`find <term>` (or just `/term`) puts you in a single-keypress search mode. Arrow keys to step matches. `n`/`p` if you're an old-school vim user. `q` to bail. Matches stay highlighted, current match is brighter.
</details>

<details>
<summary><b>🗂️ Interactive history and bookmark pickers</b></summary>

`history` and `bookmarks` open a full-screen picker with viewport scrolling, cursor navigation, *and* jump-by-number. Type `4`, `2`, `Enter` to open entry 42. Or arrow your way there. Whatever works.
</details>

<details>
<summary><b>📥 Sensible binary handling</b></summary>

PDF? Image? Tarball? babelTUI notices, tells you the size, asks if you want to save it, picks a default filename from the URL, and warns before overwriting. No accidental ten-megabyte data dumps to your terminal.
</details>

<details>
<summary><b>🛡️ Atomic, permission-hardened state</b></summary>

Bookmarks, history, known hosts, config — all written to `~/.config/babeltui/` with `chmod 0600` and via write-then-rename so an interrupted save can never corrupt your data. Yank the power cord mid-bookmark. Try us.
</details>

<details>
<summary><b>⌨️ Context-aware tab completion</b></summary>

Tab after `go`? Completes against your bookmarks and recent history. Tab after `delbm`? Completes against bookmark names only. Tab on an empty line? Lists commands. The completer knows what you're up to.
</details>

---

## 🎛️ Commands

Just the essentials here. Full reference lives in [`DOCUMENTATION.md`](DOCUMENTATION.md).

### Navigation
```text
go <url>            # navigate (aliases: visit, g)
<number>            # follow link N
back / b            # history back
forward / f         # history forward
up / ..             # go up one path segment
reload / r          # re-fetch current page
home                # configured home URL
finger user@host    # finger someone
```

### Page operations
```text
links [filter]      # list links, optionally filtered
find <term>  /  /   # interactive search
source              # raw response body
save [filename]     # save current page
url                 # show full URL
```

### Bookmarks & history
```text
bookmark [name]     # bookmark current page
bookmarks           # interactive bookmark picker
open <n|name>       # open bookmark by index or name
delbm <name>        # delete bookmark
history             # interactive history picker
delh <n>            # delete single history entry
clearhistory        # purge all history (with confirmation)
```

### Settings
```text
set pager on|off
set color on|off
set home <url>
set timeout <seconds>
set history_limit <n>
```

### Meta
```text
help / ?            # full help screen
clear               # clear terminal
quit / q / exit
```

---

## 🎨 Customisation

Configuration lives at `~/.config/babeltui/config.json`. Defaults:

```json
{
"home": "spartan://mozz.us/",
"timeout": 15,
"history_limit": 500,
"color": true,
"pager": false
}
```

Edit by hand or use `set <option> <value>` in the REPL — either way works. CLI flags (`--home`, `--timeout`, `--no-color`, `--pager`, `--history-limit`) override and persist.

Want all your shell sessions to start in your favourite capsule?

```bash
alias smol='babeltui keplers://larsthebear.me/'
```

---

## 🔒 Security

This is a smolnet client, so the threat model is appropriately smolnet:

- **TLS** is verified by **TOFU fingerprint pin**, not the CA system. First contact records the SHA-256; mismatches need explicit confirmation. CA chain validation is intentionally off because self-signed is the norm out here.
- **Plaintext downgrades** (`keplers://` → `kepler://`) require confirmation. Per Kepler spec §4.7.1.
- **Sensitive input** (Kepler code 11) uses `getpass` — no echo, no readline history, no shell history.
- **State files** are written with `0600` permissions on every save, atomically.
- **URI lengths** are bounded per spec (1024 bytes for Kepler and Gemini); headers are bounded defensively.
- **Redirect loops** are caught (10 hops general, 5 for Kepler).

Read the source. It's one file. You can audit the entire thing in an afternoon.

---

## 🛠️ Project Layout

```
babelTUI/
├── babeltui.py         ← the entire browser
├── README.md           ← you are here
├── DOCUMENTATION.md    ← the full reference
└── LICENSE
```

That's the whole repo. *That's the point.*

---

## 🤝 Contributing

Patches welcome, with a few non-negotiables to keep this thing what it is:

- 🚫 **No runtime dependencies outside the standard library.** If your patch needs `requests` or `rich`, it's a different project.
- 🚫 **No splitting into a package.** The single-file property is a feature.
- ✅ **Match the existing style** — PEP 8, type hints, dataclasses where they fit, named constants.
- ✅ **Defensive error handling** — no bare `except`, catch the narrowest exception that makes sense.
- ✅ **Update docs** if you touch user-facing behaviour.

New protocol support is the warmest kind of contribution, provided:

- The protocol has a published specification
- At least one public server runs it
- It can be plumbed through the existing fetcher/handler dispatch tables (see `DOCUMENTATION.md` § Architecture)

### Filing bugs

Include:

1. Python version (`python --version`)
2. OS and terminal
3. The URL or command that broke it
4. Error output (use `--no-color` if pasting into a tracker that mangles ANSI)

---

## 📚 Further Reading

### Protocol specifications

- **Kepler** — [github.com/kevinboone/kepler-protocol](https://github.com/kevinboone/kepler-protocol) (Kevin Boone, 0.1c May 2026)
- **Gemini** — `gemini://geminiprotocol.net/`
- **Spartan** — `spartan://mozz.us/spartan-spec`
- **Nex** — `nex://nex.nightfall.city/`
- **Gopher** — RFC 1436, plus `gopher://gopher.floodgap.com/` for the living scene
- **Finger** — RFC 1288

### Live capsules to try

- `keplers://larsthebear.me/` — the reference Kepler server
- `gemini://geminiprotocol.net/` — the Gemini mothership
- `spartan://mozz.us/` — default home, lovely board
- `gopher://gopher.floodgap.com/` — the ur-Gopher

You can browse all of these *from babelTUI itself*. That feels right, doesn't it.

### Related software

- **[molly-brown-k](https://github.com/kevinboone/molly-brown-k)** — Kevin Boone's Kepler-capable fork of Molly Brown. ~30 lines of diff over the original Gemini server. If you want to *host* a Kepler capsule, start here.
- **[Caztor](https://github.com/kevinboone/caztor)** — another client with early Kepler support, in case you want to compare implementations.

---

## 🌟 Acknowledgements

Special thanks to **Kevin Boone** for designing the Kepler protocol and putting the spec into the public domain (CC0). Building a client for a protocol whose author explicitly *invites* implementations is a rare joy — every ambiguity has an answer in the spec, every design decision has a rationale in the explanatory notes.

To everyone who keeps a capsule lit, who maintains a Gopher server "just because", who answers their finger queries with `.plan` files like it's still the nineties — this is for you.

The smolnet isn't a nostalgia project. It's a *current*, *evolving* working alternative to a web that lost the plot. Kepler launching in 2026 with first-class caching support is proof: this corner of the net is still building. babelTUI is just one more way in.

---

## 📜 License

See [`LICENSE`](LICENSE) in the repository root.

Kepler protocol specification © Kevin Boone, released under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/).

---

<p align="center">
<i>The web went corporate. The smolnet stayed weird.</i><br>
<i>babelTUI is for the weird.</i>
</p>

<p align="center">
⌨️ 🌐 📜
</p>

