#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kemus — PC virtuel leger, inspire KDE Plasma / NixOS / Fedora.
Serveur HTTP Python stdlib UNIQUEMENT (zero dependance pip).
Port par defaut : 3922 (permanent).

Lancement :
    python3 kemus.py                  # http://localhost:3922
    KEMUS_PORT=4000 python3 kemus.py  # port custom
    kemus                             # apres pip install kemus

Tous les fichiers du "PC" vivent dans ~/.kemus/home/ (VFS reel persistant).
"""
import os
import sys
import json
import time
import shutil
import socket
import hashlib
import secrets
import subprocess
import traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote
from pathlib import Path

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
KEMUS_HOME = Path.home() / ".kemus"
KEMUS_HOME.mkdir(parents=True, exist_ok=True)
VFS_ROOT = KEMUS_HOME / "home"
VFS_ROOT.mkdir(parents=True, exist_ok=True)
LOG_FILE = KEMUS_HOME / "kemus.log"
PORT = int(os.environ.get("KEMUS_PORT", "3922"))
HOST = os.environ.get("KEMUS_HOST", "127.0.0.1")
VERSION = "2.0.0"
DISTRO = "KemusOS"

# Sessions: token -> {"cwd": str, "history": [str]}
SESSIONS: dict[str, dict] = {}

# ----------------------------------------------------------------------------
# Seed filesystem (first boot)
# ----------------------------------------------------------------------------
def seed_vfs():
    files = {
        "README.txt": (
            "Bienvenue dans Kemus - PC virtuel leger.\n"
            "Inspire de KDE Plasma, NixOS et Fedora.\n\n"
            "Terminal : tapez 'help' pour la liste des commandes.\n"
            "Tentez aussi : neofetch, ls, cat hello.py\n"
        ),
        "hello.py": 'print("Hello from Kemus!")\n',
        "Documents/notes.md": (
            "# Mes notes\n\n"
            "- Essayer neofetch dans le terminal\n"
            "- Explorer le menu Kickoff (icone flocon)\n"
            "- Fenetres drag & drop, redimensionnables\n"
        ),
        "Documents/welcome.txt": "Fichier de bienvenue - editez-moi !\n",
        "Projects/hello.py": 'print("Project 1")\n',
        "Pictures/.keep": "",
        "Music/.keep": "",
        "Downloads/.keep": "",
    }
    for rel, content in files.items():
        p = VFS_ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(content, encoding="utf-8")


# ----------------------------------------------------------------------------
# Security: resolve and sandbox a path under VFS_ROOT
# ----------------------------------------------------------------------------
def safe_path(rel: str, cwd: str) -> Path:
    """Resolve rel against cwd, then ensure final path is inside VFS_ROOT."""
    if rel is None or rel == "":
        rel = "."
    # Build absolute virtual path
    if rel.startswith("/"):
        virtual = rel.lstrip("/")
    else:
        virtual = os.path.normpath(os.path.join(cwd, rel))
    virtual = virtual.lstrip("/")
    target = (VFS_ROOT / virtual).resolve()
    try:
        target.relative_to(VFS_ROOT.resolve())
    except ValueError:
        raise PermissionError(f"Acces refuse (hors sandbox) : {rel}")
    return target


def rel_from_root(p: Path) -> str:
    try:
        rel = p.relative_to(VFS_ROOT.resolve())
    except ValueError:
        return "/"
    s = str(rel)
    return "/" + s if s != "." else "/"


# ----------------------------------------------------------------------------
# Terminal command interpreter (returns text, updates cwd)
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# Kemus 2.0 — Terminal reel : pipes, redirections, && || ; , env vars
# ----------------------------------------------------------------------------
STDIN_COMMANDS = {"grep", "head", "tail", "wc", "sort", "uniq", "tee", "less", "more", "cat", "xargs", "bc"}
ENV_COMMANDS = {"export", "unset", "env", "set"}


def _expand_env(tok: str, env: dict) -> str:
    """Expand $VAR and ${VAR} in a token."""
    if "$" not in tok:
        return tok
    out = ""
    i = 0
    while i < len(tok):
        c = tok[i]
        if c == "$":
            if i + 1 < len(tok) and tok[i + 1] == "{":
                j = tok.find("}", i + 2)
                if j > 0:
                    name = tok[i + 2:j]
                    out += str(env.get(name, ""))
                    i = j + 1
                    continue
            # bare $VAR : letters/digits/_ until non-match
            j = i + 1
            while j < len(tok) and (tok[j].isalnum() or tok[j] == "_"):
                j += 1
            name = tok[i + 1:j]
            out += str(env.get(name, ""))
            i = j
        else:
            out += c
            i += 1
    return out


def _split_control(raw: str) -> list[tuple[str, str]]:
    """Split raw on ; && || respecting quotes. Returns [(op, segment), ...] with op in {'', ';', '&&', '||'}."""
    pieces = []
    cur = ""
    quote = None
    i = 0
    pending_op = ""
    while i < len(raw):
        c = raw[i]
        if quote:
            cur += c
            if c == quote:
                quote = None
        else:
            if c in ('"', "'"):
                quote = c
                cur += c
            elif c == "|" and i + 1 < len(raw) and raw[i + 1] == "|":
                pieces.append((pending_op, cur))
                pending_op = "||"
                cur = ""
                i += 2
                continue
            elif c == "&" and i + 1 < len(raw) and raw[i + 1] == "&":
                pieces.append((pending_op, cur))
                pending_op = "&&"
                cur = ""
                i += 2
                continue
            elif c == ";":
                pieces.append((pending_op, cur))
                pending_op = ";"
                cur = ""
                i += 1
                continue
            else:
                cur += c
        i += 1
    if cur.strip() or pending_op:
        pieces.append((pending_op, cur))
    return pieces


def _split_pipes(segment: str) -> list[str]:
    """Split a segment on | (single pipe) respecting quotes."""
    out = []
    cur = ""
    quote = None
    i = 0
    while i < len(segment):
        c = segment[i]
        if quote:
            cur += c
            if c == quote:
                quote = None
        else:
            if c in ('"', "'"):
                quote = c
                cur += c
            elif c == "|":
                out.append(cur)
                cur = ""
            else:
                cur += c
        i += 1
    if cur:
        out.append(cur)
    return [s.strip() for s in out if s.strip()]


def _extract_redirs(tokens: list[str]) -> tuple[list[str], str | None, bool, str | None]:
    """Extract > file, >> file, < file from tokens. Returns (cmd_args, redir_out, append, redir_in)."""
    cmd_args = []
    redir_out = None
    append = False
    redir_in = None
    i = 0
    while i < len(tokens):
        a = tokens[i]
        if a in (">", ">>") and i + 1 < len(tokens):
            redir_out = tokens[i + 1]
            append = (a == ">>")
            i += 2
        elif a in ("<", "<<") and i + 1 < len(tokens):
            redir_in = tokens[i + 1]
            i += 2
        elif a.startswith(">>"):
            redir_out = a[2:]
            append = True
            i += 1
        elif a.startswith(">"):
            redir_out = a[1:]
            i += 1
        elif a.startswith("<"):
            redir_in = a[1:]
            i += 1
        else:
            cmd_args.append(a)
            i += 1
    return cmd_args, redir_out, append, redir_in


def run_command(raw: str, cwd: str, stdin: str = "", env: dict | None = None) -> tuple[str, str, dict]:
    """Execute a command line with pipes, redirections, &&, ||, ;, env vars.

    Returns (output_text, new_cwd, new_env).
    """
    if env is None:
        env = {}
    line = raw.strip()
    if not line:
        return "", cwd, env
    pieces = _split_control(line)
    out = ""
    cur_stdin = stdin
    last_rc = 0
    for op, segment in pieces:
        if op == "&&" and last_rc != 0:
            continue
        if op == "||" and last_rc == 0:
            continue
        text, cwd, env, rc = _exec_segment(segment, cwd, cur_stdin, env)
        out += text
        last_rc = rc
        cur_stdin = ""  # stdin only feeds the very first command
    return out, cwd, env


def _exec_segment(segment: str, cwd: str, stdin: str, env: dict) -> tuple[str, str, dict, int]:
    stages = _split_pipes(segment)
    if not stages:
        return "", cwd, env, 0
    cur_stdin = stdin
    rc = 0
    for stage in stages:
        text, cwd, env, rc = _exec_one(stage, cwd, cur_stdin, env)
        cur_stdin = text
    return cur_stdin, cwd, env, rc


def _exec_one(line: str, cwd: str, stdin: str, env: dict) -> tuple[str, str, dict, int]:
    """Execute a single command (no pipes, no control), with redirections."""
    # Tokenize then expand env
    raw_tokens = _tokenize(line)
    if not raw_tokens:
        return "", cwd, env, 0
    tokens = [_expand_env(t, env) for t in raw_tokens]
    cmd_args, redir_out, append_out, redir_in = _extract_redirs(tokens)
    if not cmd_args:
        return "", cwd, env, 0
    cmd = cmd_args[0]
    args = cmd_args[1:]
    # Input redirection overrides stdin
    if redir_in:
        try:
            stdin = safe_path(redir_in, cwd).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"kemus: {e}\n", cwd, env, 1
    # Dispatch
    rc = 0
    text = ""
    handler = COMMANDS.get(cmd)
    try:
        if cmd in STDIN_COMMANDS:
            text, cwd = handler(args, cwd, stdin)
        elif cmd in ENV_COMMANDS:
            text, cwd, env = handler(args, cwd, env)
        elif handler is not None:
            text, cwd = handler(args, cwd)
        elif cmd in ("python3", "python"):
            text, cwd = _cmd_python(args, cwd)
        else:
            text = f"kemus: commande introuvable : {cmd}\nTapez 'help' ou 'man'.\n"
            rc = 127
        # Detect error rc from output prefix (only if rc not already set explicitly)
        if rc == 0 and (text.startswith("kemus:") or text.startswith(f"{cmd}:")):
            rc = 1
    except PermissionError as e:
        text = f"kemus: {e}\n"
        rc = 1
    except FileNotFoundError as e:
        text = f"kemus: {cmd}: {e}\n"
        rc = 1
    except Exception as e:
        text = f"kemus: {cmd}: {e}\n"
        rc = 1
    # Output redirection
    if redir_out:
        try:
            p = safe_path(redir_out, cwd)
            p.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append_out else "w"
            with open(p, mode, encoding="utf-8") as f:
                f.write(text)
            return "", cwd, env, rc
        except Exception as e:
            return f"kemus: {e}\n", cwd, env, 1
    return text, cwd, env, rc


# ============================================================================
# Nouvelles commandes v2 (pipes, stdin, env)
# ============================================================================

def _cmd_grep(args, cwd, stdin):
    if not args:
        return ("usage: grep PATTERN [file]\n", cwd)
    pattern = args[0]
    files = args[1:]
    invert = False
    case_insensitive = False
    if pattern.startswith("-"):
        flags = pattern
        pattern = args[1] if len(args) > 1 else ""
        files = args[2:]
        if "v" in flags:
            invert = True
        if "i" in flags:
            case_insensitive = True
    if files:
        text = ""
        for f in files:
            try:
                p = safe_path(f, cwd)
                if p.is_file():
                    text += p.read_text(encoding="utf-8", errors="replace")
                else:
                    text += f"grep: {f}: No such file\n"
            except PermissionError as e:
                text += f"grep: {e}\n"
    else:
        text = stdin
    out_lines = []
    pat = pattern.lower() if case_insensitive else pattern
    for line in text.splitlines():
        hay = line.lower() if case_insensitive else line
        match = pat in hay
        if invert:
            match = not match
        if match:
            out_lines.append(line)
    return "\n".join(out_lines) + ("\n" if out_lines else ""), cwd


def _cmd_head(args, cwd, stdin):
    n = 10
    files = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-n") and len(a) > 2:
            n = int(a[2:])
        elif a == "-n" and i + 1 < len(args):
            n = int(args[i + 1]); i += 1
        elif a.startswith("-") and a[1:].isdigit():
            n = int(a[1:])
        else:
            files.append(a)
        i += 1
    text = ""
    if files:
        for f in files:
            try:
                p = safe_path(f, cwd)
                if p.is_file():
                    text += p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
    else:
        text = stdin
    lines = text.splitlines()
    return "\n".join(lines[:n]) + ("\n" if lines[:n] else ""), cwd


def _cmd_tail(args, cwd, stdin):
    n = 10
    files = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-n") and len(a) > 2:
            n = int(a[2:])
        elif a == "-n" and i + 1 < len(args):
            n = int(args[i + 1]); i += 1
        elif a.startswith("-") and a[1:].isdigit():
            n = int(a[1:])
        else:
            files.append(a)
        i += 1
    text = ""
    if files:
        for f in files:
            try:
                p = safe_path(f, cwd)
                if p.is_file():
                    text += p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
    else:
        text = stdin
    lines = text.splitlines()
    return "\n".join(lines[-n:]) + ("\n" if lines[-n:] else ""), cwd


def _cmd_wc(args, cwd, stdin):
    files = []
    count_lines = "l" in "".join([a for a in args if a.startswith("-")]) or not args
    count_words = "w" in "".join([a for a in args if a.startswith("-")]) or not args
    count_chars = "c" in "".join([a for a in args if a.startswith("-")]) or not args
    for a in args:
        if not a.startswith("-"):
            files.append(a)
    if files:
        text = ""
        for f in files:
            try:
                p = safe_path(f, cwd)
                if p.is_file():
                    text += p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
    else:
        text = stdin
    nl = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    nw = len(text.split())
    nc = len(text)
    parts = []
    if count_lines: parts.append(str(nl))
    if count_words: parts.append(str(nw))
    if count_chars: parts.append(str(nc))
    label = " ".join(files) if files else ""
    return ("  " + " ".join(parts) + (" " + label if label else "") + "\n"), cwd


def _cmd_sort(args, cwd, stdin):
    files = [a for a in args if not a.startswith("-")]
    reverse = any("r" in a for a in args if a.startswith("-"))
    text = ""
    if files:
        for f in files:
            try:
                p = safe_path(f, cwd)
                if p.is_file():
                    text += p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
    else:
        text = stdin
    lines = sorted(text.splitlines(), reverse=reverse)
    return "\n".join(lines) + ("\n" if lines else ""), cwd


def _cmd_uniq(args, cwd, stdin):
    files = [a for a in args if not a.startswith("-")]
    text = ""
    if files:
        for f in files:
            try:
                p = safe_path(f, cwd)
                if p.is_file():
                    text += p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
    else:
        text = stdin
    out = []
    last = None
    for line in text.splitlines():
        if line != last:
            out.append(line)
            last = line
    return "\n".join(out) + ("\n" if out else ""), cwd


def _cmd_tee(args, cwd, stdin):
    append = any("a" in a for a in args if a.startswith("-"))
    files = [a for a in args if not a.startswith("-")]
    for f in files:
        try:
            p = safe_path(f, cwd)
            p.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with open(p, mode, encoding="utf-8") as fh:
                fh.write(stdin)
        except Exception:
            pass
    return stdin, cwd


def _cmd_less(args, cwd, stdin):
    files = [a for a in args if not a.startswith("-")]
    if files:
        out = []
        for f in files:
            try:
                p = safe_path(f, cwd)
                if p.is_file():
                    out.append(p.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass
        return "\n".join(out), cwd
    return stdin, cwd


def _cmd_more(args, cwd, stdin):
    return _cmd_less(args, cwd, stdin)


def _cmd_xargs(args, cwd, stdin):
    if not args:
        return ("xargs: usage: xargs COMMAND\n", cwd)
    target_cmd = args[0]
    target_args = args[1:]
    lines = [l for l in stdin.split() if l]
    out = ""
    for item in lines:
        full_args = target_args + [item]
        handler = COMMANDS.get(target_cmd)
        if handler:
            try:
                text, _ = handler(full_args, cwd)
                out += text
            except Exception as e:
                out += f"xargs: {e}\n"
    return out, cwd


def _cmd_env(args, cwd, env):
    if args:
        # env VAR=val command...
        for a in args:
            if "=" in a:
                k, v = a.split("=", 1)
                env[k] = v
            else:
                break
        return "", cwd, env
    out = ""
    for k in sorted(env.keys()):
        out += f"{k}={env[k]}\n"
    out += f"PWD={cwd}\n"
    out += f"USER=kemus\n"
    out += f"HOME=/\n"
    out += f"SHELL=/bin/kemus-sh\n"
    return out, cwd, env


def _cmd_export(args, cwd, env):
    for a in args:
        if "=" in a:
            k, v = a.split("=", 1)
            env[k] = v
    return "", cwd, env


def _cmd_unset(args, cwd, env):
    for k in args:
        env.pop(k, None)
    return "", cwd, env


def _cmd_set(args, cwd, env):
    return _cmd_env(args, cwd, env)


def _cmd_which(args, cwd):
    if not args:
        return ("usage: which COMMAND\n", cwd)
    out = ""
    builtins = list(COMMANDS.keys()) + ["python3"]
    for c in args:
        if c in builtins:
            out += f"/usr/bin/{c}\n"
        else:
            out += f"which: no {c} in PATH\n"
    return out, cwd


def _cmd_man(args, cwd):
    if not args:
        return ("What manual page do you want?\nusage: man COMMAND\n", cwd)
    out = ""
    for c in args:
        text = _fetch_man(c)
        out += text + "\n"
    return out, cwd


def _cmd_hostname(args, cwd):
    return socket.gethostname() + "\n", cwd


def _cmd_history_real(args, cwd):
    # Placeholder; real history is rendered client-side
    return "(historique cote client — fleche haut/bas)\n", cwd


def _cmd_uptime(args, cwd):
    up = int(time.time() - START_TIME)
    return f"up {up//60}m {up%60}s, 1 user, load average: 0.05, 0.02, 0.00\n", cwd


def _cmd_curl(args, cwd):
    if not args:
        return ("curl: try 'curl URL' or 'curl -s URL'\n", cwd)
    url = None
    silent = False
    output_file = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-s" or a == "--silent":
            silent = True
        elif a == "-o" and i + 1 < len(args):
            output_file = args[i + 1]; i += 1
        elif a.startswith("http://") or a.startswith("https://"):
            url = a
        elif not a.startswith("-"):
            url = a
        i += 1
    if not url:
        return "curl: no URL\n", cwd
    if not _is_safe_url(url):
        return f"curl: blocked (SSRF protection)\n", cwd
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"Kemus/{VERSION}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type", "")
        if output_file:
            p = safe_path(output_file, cwd)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            return "", cwd
        if "text" in ctype or "json" in ctype or "html" in ctype or "xml" in ctype:
            return data.decode("utf-8", errors="replace"), cwd
        return f"<binary {len(data)} bytes, content-type: {ctype}>\n", cwd
    except Exception as e:
        return f"curl: {e}\n", cwd


def _cmd_wget(args, cwd):
    if not args:
        return ("wget: usage: wget URL [OUTPUT_FILE]\n", cwd)
    url = None
    output_file = None
    for a in args:
        if a.startswith("http://") or a.startswith("https://") or (not a.startswith("-") and "." in a):
            url = a
        elif not a.startswith("-"):
            output_file = a
    if not url:
        return "wget: no URL\n", cwd
    if not _is_safe_url(url):
        return f"wget: blocked (SSRF protection)\n", cwd
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"Kemus/{VERSION}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        if not output_file:
            output_file = url.rstrip("/").split("/")[-1] or "index.html"
        p = safe_path(output_file, cwd)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return f"--{time.strftime('%Y-%m-%d %H:%M:%S')}--  {url}\n=> '{output_file}' [{len(data)} octets]\n", cwd
    except Exception as e:
        return f"wget: {e}\n", cwd


def _cmd_ping(args, cwd):
    if not args:
        return ("ping: usage: ping HOST\n", cwd)
    host = args[0]
    out = f"PING {host} (127.0.0.1) 56(84) bytes of data.\n"
    for i in range(4):
        out += f"64 bytes from {host}: icmp_seq={i+1} ttl=64 time=0.0{i} ms\n"
        time.sleep(0.05)
    out += f"\n--- {host} ping statistics ---\n4 packets transmitted, 4 received, 0% packet loss\n"
    return out, cwd


def _cmd_file(args, cwd):
    if not args:
        return ("file: usage: file PATH\n", cwd)
    out = ""
    for a in args:
        try:
            p = safe_path(a, cwd)
            if p.is_dir():
                out += f"{a}: directory\n"
            elif p.is_file():
                head = p.read_bytes()[:200]
                if head.startswith(b"#!"):
                    shebang = head.split(b"\n", 1)[0].decode("utf-8", errors="replace")
                    out += f"{a}: {shebang}\n"
                elif head[:4] == b"\x89PNG":
                    out += f"{a}: PNG image\n"
                elif head[:3] == b"\xff\xd8\xff":
                    out += f"{a}: JPEG image\n"
                elif b"<" in head and b">" in head:
                    out += f"{a}: HTML or XML document\n"
                else:
                    out += f"{a}: ASCII text\n"
            else:
                out += f"{a}: cannot open\n"
        except Exception as e:
            out += f"{a}: {e}\n"
    return out, cwd


def _cmd_stat(args, cwd):
    if not args:
        return ("stat: usage: stat PATH\n", cwd)
    out = ""
    for a in args:
        try:
            p = safe_path(a, cwd)
            st = p.stat()
            out += f"  File: {a}\n"
            out += f"  Size: {st.st_size}\tBlocks: {st.st_blocks}\tIO Block: 4096\tregular file\n"
            out += f"Modify: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}\n"
        except Exception as e:
            out += f"{a}: {e}\n"
    return out, cwd


def _cmd_chmod(args, cwd):
    if len(args) < 2:
        return ("chmod: usage: chmod MODE FILE\n", cwd)
    return f"chmod: simulation - permissions '{args[0]}' appliquees a {args[1]}\n", cwd


def _cmd_chown(args, cwd):
    return "chown: simulation - propriete modifiee\n", cwd


def _cmd_ln(args, cwd):
    if len(args) < 2:
        return ("ln: usage: ln [-s] TARGET LINK\n", cwd)
    return f"ln: lien simule {args[-2]} -> {args[-1]}\n", cwd


def _cmd_tar(args, cwd):
    return "tar: archive simulee\n", cwd


def _cmd_diff(args, cwd):
    if len(args) < 2:
        return ("diff: usage: diff FILE1 FILE2\n", cwd)
    try:
        a = safe_path(args[0], cwd).read_text(encoding="utf-8", errors="replace").splitlines()
        b = safe_path(args[1], cwd).read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return f"diff: {e}\n", cwd
    out = []
    if a == b:
        return "", cwd
    for i in range(max(len(a), len(b))):
        la = a[i] if i < len(a) else None
        lb = b[i] if i < len(b) else None
        if la != lb:
            if la is not None:
                out.append(f"< {la}")
            if lb is not None:
                out.append(f"> {lb}")
    return "\n".join(out) + "\n", cwd


def _cmd_seq(args, cwd):
    if not args:
        return ("seq: usage: seq N | seq START END | seq START STEP END\n", cwd)
    nums = [int(x) for x in args if x.lstrip("-").isdigit()]
    if len(nums) == 1:
        return "\n".join(str(i) for i in range(1, nums[0] + 1)) + "\n", cwd
    if len(nums) == 2:
        return "\n".join(str(i) for i in range(nums[0], nums[1] + 1)) + "\n", cwd
    if len(nums) == 3:
        start, step, end = nums
        out = []
        i = start
        while (step > 0 and i <= end) or (step < 0 and i >= end):
            out.append(str(i))
            i += step
        return "\n".join(out) + "\n", cwd
    return "", cwd


def _cmd_bc(args, cwd, stdin):
    expr = " ".join(args) or stdin.strip()
    if not expr:
        return "", cwd
    expr = expr.replace("^", "**")
    try:
        if not _is_safe_expr(expr):
            return "bc: erreur d'expression\n", cwd
        r = eval(expr, {"__builtins__": {}}, {})
        return str(r) + "\n", cwd
    except Exception as e:
        return f"bc: {e}\n", cwd


def _is_safe_expr(expr):
    import re
    return bool(re.match(r'^[\d\s+\-*/.()%]+$', expr))


def _cmd_printf(args, cwd):
    if not args:
        return ("printf: usage: printf FORMAT [ARGS]\n", cwd)
    fmt = args[0]
    rest = args[1:]
    out = fmt
    for i, val in enumerate(rest):
        out = out.replace(f"%s", val, 1) if "%s" in out else out
        out = out.replace(f"%d", val, 1) if "%d" in out else out
    out = out.replace("\\n", "\n").replace("\\t", "\t")
    return out, cwd


def _cmd_yes(args, cwd):
    text = " ".join(args) if args else "y"
    return (text + "\n") * 5, cwd


def _cmd_basename(args, cwd):
    if not args:
        return ("basename: usage: basename PATH\n", cwd)
    return os.path.basename(args[0].rstrip("/")) + "\n", cwd


def _cmd_dirname(args, cwd):
    if not args:
        return ("dirname: usage: dirname PATH\n", cwd)
    return os.path.dirname(args[0].rstrip("/")) + "\n", cwd


def _cmd_realpath(args, cwd):
    if not args:
        return ("realpath: usage: realpath PATH\n", cwd)
    try:
        p = safe_path(args[0], cwd)
        return str(p) + "\n", cwd
    except Exception as e:
        return f"realpath: {e}\n", cwd


# === v2 catalog/man/search/workflow helpers ===
def _is_safe_url(url: str) -> bool:
    """SSRF protection: block localhost, private IPs, file://, non-http(s)."""
    if not url.startswith(("http://", "https://")):
        return False
    import urllib.parse
    p = urllib.parse.urlparse(url)
    host = p.hostname or ""
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "metadata.google.internal"):
        return False
    if host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = socket.gethostbyname(host)
        # block private ranges
        parts = [int(x) for x in ip.split(".")]
        if parts[0] == 10: return False
        if parts[0] == 172 and 16 <= parts[1] <= 31: return False
        if parts[0] == 192 and parts[1] == 168: return False
        if parts[0] == 127: return False
        if parts[0] == 169 and parts[1] == 254: return False
    except Exception:
        pass
    return True


_OFFLINE_MAN = {
    "ls": "LS(1) - list directory contents\n\nSYNOPSIS\n    ls [OPTION]... [FILE]...\n\nDESCRIPTION\n    List information about FILEs (current dir by default).\n    -l  long format\n    -a  all entries (incl. hidden)\n    -R  recursive\n",
    "cd": "CD(1) - change working directory\n\nSYNOPSIS\n    cd [DIR]\n\nDESCRIPTION\n    Change shell working directory to DIR. Without DIR, go to $HOME.\n",
    "cat": "CAT(1) - concatenate files and print on stdout\n\nSYNOPSIS\n    cat [FILE]...\n\nDESCRIPTION\n    Reads each FILE in sequence and writes to stdout.\n    Use 'cat > file' to write from stdin.\n",
    "grep": "GREP(1) - print lines matching a pattern\n\nSYNOPSIS\n    grep [-iv] PATTERN [FILE]...\n\nDESCRIPTION\n    Search for PATTERN in FILE or stdin.\n    -i  case-insensitive\n    -v  invert match\n",
    "echo": "ECHO(1) - display a line of text\n\nSYNOPSIS\n    echo [STRING]... [> FILE]\n\nDESCRIPTION\n    Print STRING(s) to stdout. Supports > and >> redirections.\n",
    "mkdir": "MKDIR(1) - make directories\n\nSYNOPSIS\n    mkdir [DIR]...\n",
    "python": "PYTHON(1) - Python interpreter (subprocess, timeout 10s)\n\nSYNOPSIS\n    python [-c CODE | FILE.py]\n",
    "neofetch": "NEOFETCH - show system information with ASCII logo\n",
    "curl": "CURL(1) - transfer data from URL\n\nSYNOPSIS\n    curl [-s] [-o FILE] URL\n\nNOTE\n    SSRF protection active (no localhost / private IPs).\n",
    "wget": "WGET(1) - download file from URL\n\nSYNOPSIS\n    wget URL [OUTPUT_FILE]\n",
    "man": "MAN(1) - interface to reference manuals\n\nSYNOPSIS\n    man COMMAND\n",
    "nix": "NIX(1) - simulated Nix package manager\n\nSYNOPSIS\n    nix install|remove|search|list PKG\n",
    "apt": "APT(8) - simulated Debian package manager\n",
    "dnf": "DNF(8) - simulated Fedora package manager\n",
    "pacman": "PACMAN(8) - simulated Arch package manager\n",
    "tree": "TREE(1) - list contents of directories in tree-like format\n",
    "find": "FIND(1) - search for files in a directory hierarchy\n",
    "wc": "WC(1) - print newline, word, and byte counts\n",
    "head": "HEAD(1) - output the first part of files\n",
    "tail": "TAIL(1) - output the last part of files\n",
    "sort": "SORT(1) - sort lines of text\n",
    "uniq": "UNIQ(1) - report or omit repeated lines\n",
    "tee": "TEE(1) - read from stdin and write to stdout and files\n",
    "export": "EXPORT - set environment variable\n",
    "env": "ENV(1) - run a program in a modified environment, or print environment\n",
    "export": "EXPORT - set environment variable (shell builtin)\n",
    "stat": "STAT(1) - display file or file system status\n",
    "file": "FILE(1) - determine file type\n",
    "which": "WHICH(1) - locate a command\n",
    "history": "HISTORY - shell command history (use Arrow Up/Down)\n",
    "uname": "UNAME(1) - print system information\n",
    "uptime": "UPTIME(1) - show how long the system has been running\n",
}


def _fetch_man(name: str) -> str:
    """Try offline dict first, then online man7.org."""
    name = name.lower().strip()
    if name in _OFFLINE_MAN:
        return _OFFLINE_MAN[name]
    # Try man7.org online
    import urllib.request, re
    for sect in ("1", "8", "2", "3", "5", "7"):
        url = f"https://man7.org/linux/man-pages/man{sect}/{name}.{sect}.html"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"Kemus/{VERSION}"})
            with urllib.request.urlopen(req, timeout=6) as r:
                html = r.read().decode("utf-8", errors="replace")
            # Strip HTML, keep NAME + SYNOPSIS sections
            text = re.sub(r"<[^>]+>", "", html)
            text = re.sub(r"&lt;", "<", text)
            text = re.sub(r"&gt;", ">", text)
            text = re.sub(r"&amp;", "&", text)
            text = re.sub(r"\n\s*\n", "\n", text)
            # Find NAME section
            idx = text.find("NAME")
            if idx > 0:
                end = text.find("SYNOPSIS", idx)
                if end < 0:
                    end = idx + 500
                snippet = text[idx:end].strip()
                return f"{name.upper()}({sect}) - {snippet}\n[source: man7.org]\n"
        except Exception:
            continue
    return f"man: No manual entry for {name}\n"


def _search_ddg(query: str) -> list:
    """Search DuckDuckGo lite, return list of {title, url, snippet}."""
    import urllib.request, urllib.parse, re
    if not query.strip():
        return []
    url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Kemus"})
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="replace")
        results = []
        # Lite DDG: links in <a class="result-link" href="...">title</a>, snippet in next <td>
        for m in re.finditer(r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL):
            raw_url = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            # Decode DDG redirect
            if "uddg=" in raw_url:
                mm = re.search(r"uddg=([^&]+)", raw_url)
                if mm:
                    raw_url = urllib.parse.unquote(mm.group(1))
            results.append({"title": title[:120], "url": raw_url, "snippet": ""})
        # Extract snippets (result-snippet class on lite)
        snips = re.findall(r'<td[^>]+class="result-snippet"[^>]*>(.*?)</td>', html, re.DOTALL)
        for i, s in enumerate(snips):
            if i < len(results):
                results[i]["snippet"] = re.sub(r"<[^>]+>", "", s).strip()[:200]
        if not results:
            # Fallback: any <a href> in result table
            for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL):
                title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                if title and "duckduckgo" not in m.group(1):
                    results.append({"title": title[:120], "url": m.group(1), "snippet": ""})
                if len(results) >= 15:
                    break
        return results[:20]
    except Exception as e:
        return [{"error": f"Recherche impossible : {e}"}]


def _proxy_url(target_url: str) -> tuple[str, str]:
    """Fetch URL server-side and return (content_type, body_bytes). SSRF-checked."""
    if not _is_safe_url(target_url):
        return "text/plain", b"URL bloquee par protection SSRF."
    import urllib.request
    try:
        req = urllib.request.Request(target_url, headers={"User-Agent": f"Mozilla/5.0 (X11; Linux x86_64) Kemus/{VERSION}"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = r.read(1024 * 1024)  # 1MB max
            ctype = r.headers.get("Content-Type", "text/html")
        # Rewrite links so they go through /api/proxy?url=...
        if "text/html" in ctype:
            html = data.decode("utf-8", errors="replace")
            import re as _re
            # Rewrite href and src (absolute URLs)
            html = _re.sub(r'(href|src)="(https?://[^"]+)"', r'\1="/api/proxy?url=\2"', html)
            # Add base style so external sites look readable
            html = "<style>* { max-width: 100%; } body { font-family: sans-serif; padding: 8px; color: #222; background: #fff; } img { max-width: 100%; height: auto; }</style>" + html
            data = html.encode("utf-8")
        return ctype, data
    except Exception as e:
        return "text/plain", f"Proxy error: {e}".encode("utf-8")


# === Workflow YAML parser + runner ===
def _parse_workflow_yaml(text: str) -> dict:
    """Minimal YAML parser for our workflow format.
    Supports: name, on, jobs: { jobname: { steps: [{name, run}] } }."""
    lines = text.splitlines()
    wf = {"name": "", "on": "manual", "jobs": {}}
    state = "root"  # root | jobs | jobname | steps
    cur_job = None
    cur_step = None
    cur_field = None
    block_lines = []
    for raw in lines:
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if state == "root":
            if line.startswith("name:"):
                wf["name"] = line[5:].strip().strip('"\'')
            elif line.startswith("on:"):
                wf["on"] = line[3:].strip().strip('"\'')
            elif line == "jobs:":
                state = "jobs"
        elif state == "jobs":
            if indent == 2 and line.endswith(":"):
                cur_job = line[:-1].strip()
                wf["jobs"][cur_job] = {"steps": []}
                state = "jobname"
        elif state == "jobname":
            if indent == 2 and line.endswith(":") and line[:-1].strip() != cur_job:
                # new job
                cur_job = line[:-1].strip()
                wf["jobs"][cur_job] = {"steps": []}
            elif indent == 4 and line == "steps:":
                state = "steps"
            elif indent == 4 and line.startswith("runs-on:"):
                wf["jobs"][cur_job]["runs-on"] = line[8:].strip()
        elif state == "steps":
            if indent == 4 and line == "steps:":
                continue
            if indent == 4 and line.endswith(":") and not line.startswith("-"):
                # new job (back to jobs state)
                cur_job = line[:-1].strip()
                wf["jobs"][cur_job] = {"steps": []}
                state = "jobname"
                continue
            if indent >= 6 and line.startswith("- "):
                # new step
                if cur_step:
                    if block_lines:
                        cur_step["run"] = "\n".join(block_lines)
                        block_lines = []
                    wf["jobs"][cur_job]["steps"].append(cur_step)
                rest = line[2:].strip()
                if rest.startswith("name:"):
                    cur_step = {"name": rest[5:].strip().strip('"\''), "run": ""}
                elif rest.startswith("run:"):
                    val = rest[4:].strip()
                    if val == "|":
                        cur_step = {"name": "(step)", "run": ""}
                        state = "block"
                        continue
                    else:
                        cur_step = {"name": "(step)", "run": val}
                else:
                    cur_step = {"name": rest, "run": ""}
            elif indent >= 8 and cur_step:
                if line.startswith("name:"):
                    cur_step["name"] = line[5:].strip().strip('"\'')
                elif line.startswith("run:"):
                    val = line[4:].strip()
                    if val == "|":
                        state = "block"
                        continue
                    else:
                        cur_step["run"] = val
        elif state == "block":
            if indent >= 8:
                block_lines.append(raw.strip())
            else:
                if cur_step:
                    cur_step["run"] = "\n".join(block_lines)
                    block_lines = []
                    wf["jobs"][cur_job]["steps"].append(cur_step)
                    cur_step = None
                state = "steps"
                # re-process this line in steps state
                if indent >= 6 and line.startswith("- "):
                    if cur_step:
                        wf["jobs"][cur_job]["steps"].append(cur_step)
                    rest = line[2:].strip()
                    if rest.startswith("name:"):
                        cur_step = {"name": rest[5:].strip().strip('"\''), "run": ""}
                    elif rest.startswith("run:"):
                        val = rest[4:].strip()
                        if val == "|":
                            cur_step = {"name": "(step)", "run": ""}
                            state = "block"
                        else:
                            cur_step = {"name": "(step)", "run": val}
    # Final flush
    if cur_step:
        if block_lines:
            cur_step["run"] = "\n".join(block_lines)
        wf["jobs"][cur_job]["steps"].append(cur_step) if cur_job else None
    return wf


def _seed_workflows():
    wf_dir = KEMUS_HOME / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    samples = {
        "build.yml": (
            "name: Build Kemus\n"
            "on: push\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: kemus-latest\n"
            "    steps:\n"
            "      - name: Preparer\n"
            "        run: echo \"Demarrage du build...\"\n"
            "      - name: Lister les sources\n"
            "        run: ls -la\n"
            "      - name: Tests Python\n"
            "        run: |\n"
            "          python3 -c \"print('1+1 =', 1+1)\"\n"
            "          python3 -c \"import sys; print('Python', sys.version_info[:2])\"\n"
            "      - name: Recapitulatif\n"
            "        run: |\n"
            "          echo \"Build OK\"\n"
            "          neofetch | head -5\n"
        ),
        "tests.yml": (
            "name: Tests terminal\n"
            "on: manual\n"
            "jobs:\n"
            "  test:\n"
            "    steps:\n"
            "      - name: Pipeline ls | grep\n"
            "        run: ls | grep Documents\n"
            "      - name: Compter les fichiers\n"
            "        run: find . | wc -l\n"
            "      - name: Generer un fichier\n"
            "        run: echo \"hello workflow\" > workflow.txt\n"
            "      - name: Afficher le contenu\n"
            "        run: cat workflow.txt\n"
        ),
        "neofetch.yml": (
            "name: Diagnostic systeme\n"
            "on: manual\n"
            "jobs:\n"
            "  diag:\n"
            "    steps:\n"
            "      - name: neofetch\n"
            "        run: neofetch\n"
            "      - name: df\n"
            "        run: df -h\n"
            "      - name: free\n"
            "        run: free -m\n"
        ),
    }
    for fname, content in samples.items():
        p = wf_dir / fname
        if not p.exists():
            p.write_text(content, encoding="utf-8")


def _list_workflows() -> list:
    wf_dir = KEMUS_HOME / "workflows"
    if not wf_dir.exists():
        return []
    out = []
    for p in sorted(wf_dir.glob("*.yml")):
        try:
            text = p.read_text(encoding="utf-8")
            wf = _parse_workflow_yaml(text)
            out.append({
                "file": p.name,
                "name": wf.get("name", p.name),
                "on": wf.get("on", "manual"),
                "jobs": list(wf.get("jobs", {}).keys()),
                "step_count": sum(len(j.get("steps", [])) for j in wf.get("jobs", {}).values()),
                "raw": text,
            })
        except Exception as e:
            out.append({"file": p.name, "error": str(e)})
    return out


# === Settings persistence ===
SETTINGS_FILE = KEMUS_HOME / "settings.json"
INSTALLED_FILE = KEMUS_HOME / "installed.json"

DEFAULT_SETTINGS = {
    "theme": "breeze-dark",
    "accent": "#3daee9",
    "wallpaper": "nixos",
    "term_font_size": 13,
    "term_font": "JetBrains Mono",
}

THEMES = {
    "breeze-dark": {"bg": "#0b0f19", "panel": "rgba(20,26,40,0.92)", "panel_solid": "#141a28", "accent": "#3daee9", "text": "#e6e9ef", "win_bg": "#1c2230", "term_bg": "#0a0d14"},
    "breeze-light": {"bg": "#eff0f1", "panel": "rgba(252,252,252,0.92)", "panel_solid": "#fcfcfc", "accent": "#1d99f3", "text": "#232627", "win_bg": "#ffffff", "term_bg": "#ffffff"},
    "nixos": {"bg": "#1a2138", "panel": "rgba(28,36,52,0.92)", "panel_solid": "#1c2434", "accent": "#5277c3", "text": "#e6e9ef", "win_bg": "#21293c", "term_bg": "#0d121d"},
    "fedora": {"bg": "#101725", "panel": "rgba(20,30,48,0.92)", "panel_solid": "#141e30", "accent": "#294172", "text": "#e6e9ef", "win_bg": "#1c2638", "term_bg": "#0a0d14"},
    "dracula": {"bg": "#282a36", "panel": "rgba(40,42,54,0.92)", "panel_solid": "#282a36", "accent": "#bd93f9", "text": "#f8f8f2", "win_bg": "#343746", "term_bg": "#21222c"},
    "solarized-dark": {"bg": "#002b36", "panel": "rgba(7,54,66,0.92)", "panel_solid": "#073642", "accent": "#2aa198", "text": "#eee8d5", "win_bg": "#073642", "term_bg": "#002b36"},
    "tokyo-night": {"bg": "#1a1b26", "panel": "rgba(26,27,38,0.92)", "panel_solid": "#1a1b26", "accent": "#7aa2f7", "text": "#c0caf5", "win_bg": "#24283b", "term_bg": "#16161e"},
    "nord": {"bg": "#2e3440", "panel": "rgba(46,52,64,0.92)", "panel_solid": "#2e3440", "accent": "#88c0d0", "text": "#e5e9f0", "win_bg": "#3b4252", "term_bg": "#2e3440"},
}


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            s = {**DEFAULT_SETTINGS, **s}
            return s
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(s: dict):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


# === App store catalog ===
STORE_CATALOG = [
    # Dev
    {"id": "code", "name": "Kode", "category": "Dev", "icon": "code", "desc": "Editeur de code avance", "exec": "openEditor"},
    {"id": "markdown", "name": "Markdown Preview", "category": "Dev", "icon": "doc", "desc": "Editeur Markdown avec preview live", "exec": "openMarkdown"},
    {"id": "workflow", "name": "Workflow Studio", "category": "Dev", "icon": "play", "desc": "Runner YAML temps reel (SSE)", "exec": "openWorkflow"},
    {"id": "terminal", "name": "Terminal", "category": "Dev", "icon": "term", "desc": "Vrai shell avec pipes, redirections, env", "exec": "openTerminal"},
    {"id": "git", "name": "Git GUI", "category": "Dev", "icon": "git", "desc": "Interface git (simulee)", "exec": "openGit"},
    {"id": "regex", "name": "Regex Tester", "category": "Dev", "icon": "search", "desc": "Test d'expressions regulieres", "exec": "openRegex"},
    # Internet
    {"id": "browser", "name": "KemusBrowser", "category": "Internet", "icon": "globe", "desc": "Navigateur reel (proxy DuckDuckGo)", "exec": "openBrowser"},
    {"id": "weather", "name": "Meteo", "category": "Internet", "icon": "cloud", "desc": "Previsions meteo (simule)", "exec": "openWeather"},
    {"id": "news", "name": "Actualites", "category": "Internet", "icon": "news", "desc": "Fil d'actualites (simule)", "exec": "openNews"},
    {"id": "mail", "name": "Courriel", "category": "Internet", "icon": "mail", "desc": "Client mail (simule)", "exec": "openMail"},
    # System
    {"id": "settings", "name": "Pro Reglages", "category": "System", "icon": "gear", "desc": "8 themes, accent, wallpaper, terminal", "exec": "openSettings"},
    {"id": "monitor", "name": "Process Monitor", "category": "System", "icon": "cpu", "desc": "Monitor CPU/memoire/processus", "exec": "openMonitor"},
    {"id": "disks", "name": "Disk Usage", "category": "System", "icon": "disk", "desc": "Analyse disque", "exec": "openDisks"},
    {"id": "logs", "name": "Logs Viewer", "category": "System", "icon": "log", "desc": "Tail du log Kemus en live", "exec": "openLogs"},
    {"id": "neofetch", "name": "Systeme Info", "category": "System", "icon": "monitor", "desc": "neofetch ASCII", "exec": "openTerminalWith_neofetch"},
    {"id": "packages", "name": "Package Manager", "category": "System", "icon": "box", "desc": "apt/dnf/nix unifie", "exec": "openPackages"},
    {"id": "firewall", "name": "Firewall", "category": "System", "icon": "shield", "desc": "Pare-feu (simule)", "exec": "openFirewall"},
    {"id": "network", "name": "Network", "category": "System", "icon": "wifi", "desc": "Configuration reseau", "exec": "openNetwork"},
    {"id": "sensors", "name": "Sensors", "category": "System", "icon": "thermo", "desc": "Capteurs temperature", "exec": "openSensors"},
    {"id": "battery", "name": "Battery", "category": "System", "icon": "bat", "desc": "Etat batterie", "exec": "openBattery"},
    {"id": "updates", "name": "Updates", "category": "System", "icon": "refresh", "desc": "Mises a jour systeme", "exec": "openUpdates"},
    {"id": "backup", "name": "Backup", "category": "System", "icon": "save", "desc": "Sauvegarde VFS", "exec": "openBackup"},
    {"id": "diagnostics", "name": "Diagnostics", "category": "System", "icon": "stetho", "desc": "Diagnostic complet", "exec": "openDiagnostics"},
    # Utilities
    {"id": "calc", "name": "Calculatrice", "category": "Utilities", "icon": "calc", "desc": "Calculatrice standard", "exec": "openCalc"},
    {"id": "files", "name": "Fichiers", "category": "Utilities", "icon": "folder", "desc": "Gestionnaire de fichiers Dolphin-like", "exec": "openFiles"},
    {"id": "notes", "name": "Notes", "category": "Utilities", "icon": "note", "desc": "Notes rapides", "exec": "openNotes"},
    {"id": "tasks", "name": "Taches", "category": "Utilities", "icon": "check", "desc": "Liste de taches", "exec": "openTasks"},
    {"id": "calendar", "name": "Calendrier", "category": "Utilities", "icon": "cal", "desc": "Calendrier mensuel", "exec": "openCalendar"},
    {"id": "clock", "name": "Horloge", "category": "Utilities", "icon": "clock", "desc": "Horloge analogique", "exec": "openClock"},
    {"id": "color", "name": "Color Picker", "category": "Utilities", "icon": "palette", "desc": "Selecteur de couleurs", "exec": "openColorPicker"},
    {"id": "screenshot", "name": "Screenshot", "category": "Utilities", "icon": "cam", "desc": "Capture d'ecran (simule)", "exec": "openScreenshot"},
    {"id": "dictionary", "name": "Dictionnaire", "category": "Utilities", "icon": "book", "desc": "Definitions via DuckDuckGo", "exec": "openDictionary"},
    {"id": "translator", "name": "Traducteur", "category": "Utilities", "icon": "translate", "desc": "Traduction (simule)", "exec": "openTranslator"},
    {"id": "man", "name": "Man Pages", "category": "Utilities", "icon": "man", "desc": "Pages de manuel Linux", "exec": "openMan"},
    {"id": "help", "name": "Aide", "category": "Utilities", "icon": "help", "desc": "Aide Kemus", "exec": "openHelp"},
    # Multimedia
    {"id": "imageviewer", "name": "Image Viewer", "category": "Multimedia", "icon": "image", "desc": "Visualisation d'images", "exec": "openImageViewer"},
    {"id": "music", "name": "Music Player", "category": "Multimedia", "icon": "music", "desc": "Lecteur audio (simule)", "exec": "openMusic"},
    {"id": "video", "name": "Video Player", "category": "Multimedia", "icon": "video", "desc": "Lecteur video (simule)", "exec": "openVideo"},
    {"id": "pdf", "name": "PDF Viewer", "category": "Multimedia", "icon": "pdf", "desc": "Lecture PDF (simule)", "exec": "openPdf"},
    # Games
    {"id": "mines", "name": "Demineur", "category": "Games", "icon": "bomb", "desc": "Minesweeper", "exec": "openMines"},
    {"id": "snake", "name": "Snake", "category": "Games", "icon": "snake", "desc": "Jeu du serpent", "exec": "openSnake"},
    {"id": "2048", "name": "2048", "category": "Games", "icon": "grid", "desc": "Puzzle 2048", "exec": "open2048"},
    {"id": "tetris", "name": "Tetris", "category": "Games", "icon": "blocks", "desc": "Tetris classique (coming soon)", "exec": "openComingSoon"},
]


def load_installed() -> list:
    if INSTALLED_FILE.exists():
        try:
            return json.loads(INSTALLED_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Default installed
    return ["terminal", "files", "editor", "browser", "calc", "settings", "help", "neofetch", "workflow", "store", "man", "monitor", "logs"]


def save_installed(items: list):
    INSTALLED_FILE.write_text(json.dumps(items, indent=2), encoding="utf-8")


# ============================================================================
# Commandes v1 (originales, conservees intactes)
# ============================================================================
# (run_command v1 supprime - remplace par la version pipeline ci-dessus)


def _tokenize(s: str) -> list[str]:
    """Simple shell tokenizer supporting ' and \"."""
    out = []
    cur = ""
    i = 0
    quote = None
    while i < len(s):
        c = s[i]
        if quote:
            if c == quote:
                quote = None
            else:
                cur += c
        else:
            if c in ('"', "'"):
                quote = c
            elif c.isspace():
                if cur:
                    out.append(cur)
                    cur = ""
            else:
                cur += c
        i += 1
    if cur:
        out.append(cur)
    return out


# --- Redirection helper ---
def _split_redirect(args):
    """Pull out > file or >> file from args. Returns (cmd_args, redir_target_or_None, append_bool)."""
    redir = None
    append = False
    cmd_args = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in (">", ">>") and i + 1 < len(args):
            redir = args[i + 1]
            append = (a == ">>")
            i += 2
        elif a.startswith(">"):
            redir = a.lstrip(">")
            append = a.startswith(">>")
            i += 1
        else:
            cmd_args.append(a)
            i += 1
    return cmd_args, redir, append


# ---- Commands ----
def _cmd_help(args, cwd):
    text = (
        "Commandes Kemus v2.0 :\n"
        "\n  --- Systeme de fichiers ---\n"
        "  pwd ls ll cd cat mkdir touch rm mv cp find tree\n"
        "  file stat ln realpath basename dirname diff\n"
        "\n  --- Pipeline & texte ---\n"
        "  cmd | cmd          pipe (ex: ls | grep py | wc -l)\n"
        "  cmd > file         redirection sortie  (>> append)\n"
        "  cmd < file         redirection entree\n"
        "  cmd && cmd         conditionnel (si succes)\n"
        "  cmd || cmd         conditionnel (si echec)\n"
        "  cmd ; cmd          sequence\n"
        "  grep head tail wc sort uniq tee less more xargs\n"
        "  printf seq yes bc\n"
        "\n  --- Environnement ---\n"
        "  export VAR=val     set env var\n"
        "  env                list env vars\n"
        "  unset VAR          remove env var\n"
        "  echo $VAR          expand variable\n"
        "\n  --- Systeme ---\n"
        "  pwd date whoami uname hostname uptime ps top df free\n"
        "  neofetch clear history which man\n"
        "\n  --- Reseau (reel, SSRF-safe) ---\n"
        "  curl URL [-s] [-o file]\n"
        "  wget URL [outfile]\n"
        "  ping HOST\n"
        "\n  --- Programmation ---\n"
        "  python [-c CODE | file.py]\n"
        "  edit <file>\n"
        "\n  --- Paquets (simules) ---\n"
        "  apt|dnf|pacman|nix install|remove|search|list PKG\n"
        "\n  --- Divers ---\n"
        "  chmod chown tar ping\n"
        "  exit logout\n"
        "\n  Total : 55+ commandes. Tapez 'man CMD' pour la doc.\n"
    )
    return text, cwd


def _cmd_pwd(args, cwd):
    return cwd + "\n", cwd


def _cmd_ls(args, cwd):
    long_fmt = False
    paths = []
    for a in args:
        if a.startswith("-"):
            if "l" in a:
                long_fmt = True
        else:
            paths.append(a)
    if not paths:
        paths = ["."]
    out_lines = []
    for p in paths:
        try:
            target = safe_path(p, cwd)
        except PermissionError as e:
            out_lines.append(f"ls: {e}")
            continue
        if not target.exists():
            out_lines.append(f"ls: cannot access '{p}': No such file or directory")
            continue
        if target.is_dir():
            items = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            if long_fmt:
                out_lines.append(f"total {len(items)}")
                for it in items:
                    st = it.stat()
                    kind = "d" if it.is_dir() else "-"
                    perms = "rwxr-xr-x"
                    size = st.st_size
                    mtime = time.strftime("%b %d %H:%M", time.localtime(st.st_mtime))
                    out_lines.append(f"{kind}{perms}  kemus  kemus  {size:>6} {mtime} {it.name}")
            else:
                # one-per-line (compatible pipe, comme `ls` reel vers non-tty)
                for it in items:
                    n = it.name
                    if it.is_dir():
                        out_lines.append(n + "/")
                    else:
                        out_lines.append(n)
        else:
            out_lines.append(p)
    return "\n".join(out_lines) + "\n", cwd


def _cmd_cd(args, cwd):
    if not args or args[0] == "~":
        return "/", "/"
    target = safe_path(args[0], cwd)
    if not target.exists():
        raise FileNotFoundError(f"{args[0]}: No such file or directory")
    if not target.is_dir():
        return f"cd: {args[0]}: Not a directory\n", cwd
    return rel_from_root(target) or "/", rel_from_root(target) or "/"


def _cmd_cat(args, cwd, stdin=""):
    if not args:
        return stdin, cwd
    out = []
    for a in args:
        p = safe_path(a, cwd)
        if not p.exists():
            out.append(f"cat: {a}: No such file or directory")
            continue
        if p.is_dir():
            out.append(f"cat: {a}: Is a directory")
            continue
        out.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(out) + ("\n" if out and not out[-1].endswith("\n") else ""), cwd


def _cmd_echo(args, cwd):
    cmd_args, redir, append = _split_redirect(args)
    text = " ".join(cmd_args) + "\n"
    if redir:
        p = safe_path(redir, cwd)
        mode = "a" if append else "w"
        with open(p, mode, encoding="utf-8") as f:
            f.write(text)
        return "", cwd
    return text, cwd


def _cmd_mkdir(args, cwd):
    if not args:
        return "mkdir: usage: mkdir <dir>\n", cwd
    for a in args:
        if a.startswith("-"):
            continue
        p = safe_path(a, cwd)
        p.mkdir(parents=True, exist_ok=False)
    return "", cwd


def _cmd_touch(args, cwd):
    if not args:
        return "touch: usage: touch <file>\n", cwd
    for a in args:
        p = safe_path(a, cwd)
        if not p.exists():
            p.write_text("", encoding="utf-8")
        else:
            os.utime(p)
    return "", cwd


def _cmd_rm(args, cwd):
    recursive = False
    targets = []
    for a in args:
        if a.startswith("-"):
            if "r" in a or "R" in a:
                recursive = True
        else:
            targets.append(a)
    if not targets:
        return "rm: usage: rm [-r] <path>\n", cwd
    for a in targets:
        p = safe_path(a, cwd)
        if not p.exists():
            return f"rm: cannot remove '{a}': No such file or directory\n", cwd
        if p.is_dir():
            if not recursive:
                return f"rm: cannot remove '{a}': Is a directory (use -r)\n", cwd
            shutil.rmtree(p)
        else:
            p.unlink()
    return "", cwd


def _cmd_mv(args, cwd):
    if len(args) < 2:
        return "mv: usage: mv <src> <dst>\n", cwd
    src = safe_path(args[0], cwd)
    dst = safe_path(args[1], cwd)
    if not src.exists():
        return f"mv: cannot stat '{args[0]}': No such file or directory\n", cwd
    if dst.is_dir():
        dst = dst / src.name
    shutil.move(str(src), str(dst))
    return "", cwd


def _cmd_cp(args, cwd):
    if len(args) < 2:
        return "cp: usage: cp <src> <dst>\n", cwd
    src = safe_path(args[0], cwd)
    dst = safe_path(args[1], cwd)
    if not src.exists():
        return f"cp: cannot stat '{args[0]}': No such file or directory\n", cwd
    if dst.is_dir():
        dst = dst / src.name
    if src.is_dir():
        shutil.copytree(str(src), str(dst))
    else:
        shutil.copy2(str(src), str(dst))
    return "", cwd


def _cmd_find(args, cwd):
    base = safe_path(args[0] if args else ".", cwd)
    if not base.exists():
        return f"find: '{args[0] if args else '.'}': No such file or directory\n", cwd
    out = []
    for root, dirs, files in os.walk(base):
        for d in dirs:
            out.append(str(Path(root).relative_to(VFS_ROOT) / d))
        for f in files:
            out.append(str(Path(root).relative_to(VFS_ROOT) / f))
    return "\n".join(out) + "\n", cwd


def _cmd_tree(args, cwd):
    base = safe_path(args[0] if args else ".", cwd)
    if not base.exists() or not base.is_dir():
        return f"tree: {args[0] if args else '.'}: not a directory\n", cwd
    lines = [rel_from_root(base) or "."]
    for root, dirs, files in os.walk(base):
        rel = Path(root).relative_to(base)
        depth = 0 if str(rel) == "." else len(rel.parts)
        for d in sorted(dirs):
            lines.append("  " * depth + "├── " + d + "/")
        for f in sorted(files):
            lines.append("  " * depth + "├── " + f)
    return "\n".join(lines) + "\n", cwd


def _cmd_date(args, cwd):
    return time.strftime("%a %b %d %H:%M:%S CET %Y") + "\n", cwd


def _cmd_whoami(args, cwd):
    return "kemus\n", cwd


def _cmd_uname(args, cwd):
    if args and args[0] in ("-a", "--all"):
        return f"{DISTRO} {VERSION} {DISTRO}-Kernel-1.0.0 #1 SMP x86_64 GNU/Linux\n", cwd
    return f"{DISTRO}\n", cwd


def _cmd_neofetch(args, cwd):
    logo = (
        "        ▗▄▄▄       \n"
        "       ▗███▚       kemus@nixos\n"
        "       ▟███▙       ----------\n"
        "      ▟█████▙      OS: KemusOS 1.0 x86_64\n"
        "     ▟███████▙     Kernel: Kemus-Kernel 1.0.0\n"
        "    ▟█████████▙    Uptime: " + _uptime_str() + "\n"
        "   ▟███████████▙   Shell: kemus-sh 1.0\n"
        "  ▟█████████████▙  DE: Plasma (Kemus Edition)\n"
        " ▟███████████████▙ WM: KWin-virtual\n"
        "                    Terminal: kemus-term\n"
        "                    CPU: Virtual x86_64 (1 core)\n"
        "                    Memory: 256 MiB\n"
        "                    Disk: " + _disk_usage() + "\n"
        "                    Theme: Breeze-Dark + NixSnow\n"
        "                    Resolution: " + _resolution_str() + "\n"
    )
    return logo, cwd


def _cmd_ps(args, cwd):
    return (
        "  PID USER     COMMAND\n"
        "    1 kemus    /sbin/init\n"
        "   42 kemus    kwin-virtual\n"
        "   43 kemus    plasmashell\n"
        "   44 kemus    kemus-term\n"
        "   45 kemus    dolphin-virtual\n"
        "   46 kemus    python3 kemus.py\n"
    ), cwd


def _cmd_top(args, cwd):
    return (
        "top - " + time.strftime("%H:%M:%S") + " up " + _uptime_str() + ", 1 user\n"
        "Tasks: 6 total, 1 running, 5 sleeping\n"
        "%Cpu(s):  3.2 us,  1.1 sy,  0.0 ni, 95.7 id\n"
        "MiB Mem : 256.0 total, 180.2 free, 35.8 used, 40.0 buff/cache\n"
        "MiB Swap:   0.0 total,   0.0 free,   0.0 used\n"
        "\n"
        "  PID USER      PR  NI    VIRT    RES    CPU COMMAND\n"
        "    1 kemus     20   0    128M    12M     0.0 /sbin/init\n"
        "   46 kemus     20   0    256M    45M     1.5 python3 kemus.py\n"
        "   43 kemus     20   0    190M    38M     0.7 plasmashell\n"
    ), cwd


def _cmd_df(args, cwd):
    return (
        "Filesystem      Size  Used Avail Use% Mounted on\n"
        "kemus-vfs       512M  64M   448M  13% /home/kemus\n"
        "tmpfs           256M  0     256M   0% /tmp\n"
    ), cwd


def _cmd_free(args, cwd):
    return (
        "              total        used        free      shared  buff/cache   available\n"
        "Mem:            256          36         180           8          40         200\n"
        "Swap:             0           0           0\n"
    ), cwd


def _cmd_history(args, cwd):
    # History is client-side; return placeholder so server-side call is harmless
    return "(historique cote client)\n", cwd


def _cmd_clear(args, cwd):
    return "\x1bc", cwd  # ANSI clear — frontend detects


def _cmd_edit(args, cwd):
    if not args:
        return "edit: usage: edit <file>\n", cwd
    p = safe_path(args[0], cwd)
    rel = rel_from_root(p)
    return f"\x1bOPEN_EDITOR:{rel}\x1b", cwd


def _cmd_python(args, cwd):
    if not args:
        return "python: usage: python [-c CODE | file.py]\n", cwd
    if args[0] == "-c":
        code = " ".join(args[1:])
    else:
        p = safe_path(args[0], cwd)
        if not p.exists():
            return f"python: can't open file '{args[0]}': No such file\n", cwd
        code = p.read_text(encoding="utf-8")
    # Run in subprocess, restricted cwd = VFS_ROOT
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(VFS_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = proc.stdout + proc.stderr
        return out, cwd
    except subprocess.TimeoutExpired:
        return "python: timeout (>10s)\n", cwd
    except Exception as e:
        return f"python: {e}\n", cwd


def _make_pkg_mgr(name):
    installed = {"kde-plasma", "firefox", "kdevelop", "dolphin", "konsole", "neofetch", "git", "vim", "nano"}

    def handler(args, cwd):
        if not args:
            return f"{name}: usage: {name} <install|remove|search|list> [pkg]\n", cwd
        sub = args[0]
        if sub == "install" and len(args) > 1:
            pkg = args[1]
            if pkg in installed:
                return f"{name}: {pkg} est deja installe (plus recent).\n", cwd
            installed.add(pkg)
            return (
                f"Resolution des dependances...\n"
                f"Telechargement de {pkg}-1.0-x86_64...\n"
                f"Verification des cles...\n"
                f"Installation de {pkg}... fait.\n"
                f"\x1bTOAST:{pkg} installe avec succes\x1b\n"
            ), cwd
        if sub == "remove" and len(args) > 1:
            pkg = args[1]
            if pkg in installed:
                installed.discard(pkg)
                return f"Suppression de {pkg}... fait.\n", cwd
            return f"{name}: {pkg} n'est pas installe.\n", cwd
        if sub == "search" and len(args) > 1:
            return f"Resultats pour '{args[1]}':\n  {args[1]}-1.0 (virtual) - paquet simule\n", cwd
        if sub == "list":
            return "Paquets installes:\n  " + "\n  ".join(sorted(installed)) + "\n", cwd
        if sub in ("update", "upgrade"):
            return f"{name}: 0 paquets a mettre a jour. Tout est a jour.\n", cwd
        return f"{name}: sous-commande inconnue : {sub}\n", cwd

    return handler


def _cmd_exit(args, cwd):
    return "\x1bCLOSE_TERM\x1b", cwd


def _uptime_str():
    return "1 min"


def _disk_usage():
    try:
        total = 512 * 1024 * 1024
        used = sum(p.stat().st_size for p in VFS_ROOT.rglob("*") if p.is_file())
        return f"{used // 1024}K / 512M"
    except Exception:
        return "—"


def _resolution_str():
    return "1366x768 (virtual)"


COMMANDS = {
    "help": _cmd_help,
    "pwd": _cmd_pwd,
    "ls": _cmd_ls,
    "ll": lambda a, c: _cmd_ls(["-l"] + a, c),
    "cd": _cmd_cd,
    "cat": _cmd_cat,
    "echo": _cmd_echo,
    "mkdir": _cmd_mkdir,
    "touch": _cmd_touch,
    "rm": _cmd_rm,
    "mv": _cmd_mv,
    "cp": _cmd_cp,
    "find": _cmd_find,
    "tree": _cmd_tree,
    "date": _cmd_date,
    "whoami": _cmd_whoami,
    "uname": _cmd_uname,
    "neofetch": _cmd_neofetch,
    "ps": _cmd_ps,
    "top": _cmd_top,
    "df": _cmd_df,
    "free": _cmd_free,
    "history": _cmd_history_real,
    "clear": _cmd_clear,
    "edit": _cmd_edit,
    "python": _cmd_python,
    "python3": _cmd_python,
    "apt": _make_pkg_mgr("apt"),
    "dnf": _make_pkg_mgr("dnf"),
    "pacman": lambda a, c: _make_pkg_mgr("pacman")(["-S" if a and a[0] in ("-S", "-Syu") else (a[0][1:] if a and a[0].startswith("-") else "list")] + a[1:], c),
    "nix": _make_pkg_mgr("nix"),
    # === v2 : nouvelles commandes reelles ===
    "grep": _cmd_grep,
    "head": _cmd_head,
    "tail": _cmd_tail,
    "wc": _cmd_wc,
    "sort": _cmd_sort,
    "uniq": _cmd_uniq,
    "tee": _cmd_tee,
    "less": _cmd_less,
    "more": _cmd_more,
    "xargs": _cmd_xargs,
    "env": _cmd_env,
    "export": _cmd_export,
    "unset": _cmd_unset,
    "set": _cmd_set,
    "which": _cmd_which,
    "man": _cmd_man,
    "hostname": _cmd_hostname,
    "uptime": _cmd_uptime,
    "curl": _cmd_curl,
    "wget": _cmd_wget,
    "ping": _cmd_ping,
    "file": _cmd_file,
    "stat": _cmd_stat,
    "chmod": _cmd_chmod,
    "chown": _cmd_chown,
    "ln": _cmd_ln,
    "tar": _cmd_tar,
    "diff": _cmd_diff,
    "seq": _cmd_seq,
    "bc": _cmd_bc,
    "printf": _cmd_printf,
    "yes": _cmd_yes,
    "basename": _cmd_basename,
    "dirname": _cmd_dirname,
    "realpath": _cmd_realpath,
    "exit": _cmd_exit,
    "logout": _cmd_exit,
}


# ----------------------------------------------------------------------------
# Session management
# ----------------------------------------------------------------------------
def get_or_create_session(token: str | None) -> str:
    if token and token in SESSIONS:
        return token
    new = secrets.token_hex(16)
    SESSIONS[new] = {"cwd": "/", "history": [], "env": {}, "created": time.time()}
    return new


# ----------------------------------------------------------------------------
# HTTP Handler
# ----------------------------------------------------------------------------
class KemusHandler(BaseHTTPRequestHandler):
    server_version = f"Kemus/{VERSION}"
    sys_version = ""

    def log_message(self, fmt, *args):
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.address_string()} {fmt % args}\n")
        except Exception:
            pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200, headers=None):
        self._send(code, json.dumps(obj), headers=headers)

    def _cookie_token(self):
        c = self.headers.get("Cookie", "")
        for part in c.split(";"):
            part = part.strip()
            if part.startswith("kemus_session="):
                return part.split("=", 1)[1]
        return None

    def do_GET(self):
        try:
            u = urlparse(self.path)
            path = u.path
            if path == "/" or path == "/index.html":
                self._send(200, HTML_PAGE, ctype="text/html; charset=utf-8")
                return
            if path == "/api/health":
                self._json({"ok": True, "version": VERSION, "uptime": time.time() - START_TIME})
                return
            if path == "/api/desktop":
                self._json({
                    "distro": DISTRO,
                    "version": VERSION,
                    "user": "kemus",
                    "host": socket.gethostname(),
                    "port": PORT,
                })
                return
            if path == "/api/fs":
                token = get_or_create_session(self._cookie_token())
                cwd = SESSIONS[token]["cwd"]
                q = parse_qs(u.query)
                rel = q.get("path", [cwd])[0]
                try:
                    p = safe_path(rel, cwd)
                except PermissionError as e:
                    self._json({"error": str(e)}, 403)
                    return
                if not p.exists():
                    self._json({"error": "not found"}, 404)
                    return
                if not p.is_dir():
                    self._json({"error": "not a directory"}, 400)
                    return
                items = []
                for it in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                    items.append({
                        "name": it.name,
                        "type": "dir" if it.is_dir() else "file",
                        "size": it.stat().st_size if it.is_file() else 0,
                    })
                self._json({"path": rel_from_root(p), "items": items}, headers={"Set-Cookie": f"kemus_session={token}; Path=/; HttpOnly"})
                return
            if path == "/api/file":
                q = parse_qs(u.query)
                rel = q.get("path", [None])[0]
                if not rel:
                    self._json({"error": "path required"}, 400)
                    return
                token = get_or_create_session(self._cookie_token())
                cwd = SESSIONS[token]["cwd"]
                try:
                    p = safe_path(rel, cwd)
                except PermissionError as e:
                    self._json({"error": str(e)}, 403)
                    return
                if not p.exists() or not p.is_file():
                    self._json({"error": "not found"}, 404)
                    return
                content = p.read_text(encoding="utf-8", errors="replace")
                self._json({"path": rel, "content": content})
                return
            # ====== v2 endpoints ======
            if path == "/api/themes":
                self._json({"themes": THEMES, "default": DEFAULT_SETTINGS})
                return
            if path == "/api/settings":
                self._json(load_settings())
                return
            if path == "/api/installed":
                self._json({"installed": load_installed(), "catalog": STORE_CATALOG})
                return
            if path == "/api/store":
                self._json({"catalog": STORE_CATALOG})
                return
            if path == "/api/search":
                q = parse_qs(u.query)
                query = q.get("q", [""])[0]
                self._json({"query": query, "results": _search_ddg(query)})
                return
            if path == "/api/man":
                q = parse_qs(u.query)
                name = q.get("cmd", [""])[0]
                self._json({"cmd": name, "text": _fetch_man(name)})
                return
            if path == "/api/proxy":
                q = parse_qs(u.query)
                target = q.get("url", [""])[0]
                ctype, body = _proxy_url(target)
                self._send(200, body, ctype=ctype)
                return
            if path == "/api/workflows":
                self._json({"workflows": _list_workflows()})
                return
            if path == "/api/workflow/run":
                # SSE streaming
                q = parse_qs(u.query)
                fname = q.get("name", [""])[0]
                self._run_workflow_sse(fname)
                return
            self._send(404, "Not Found", ctype="text/plain")
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    def _run_workflow_sse(self, fname: str):
        """Stream workflow execution as Server-Sent Events."""
        wf_dir = KEMUS_HOME / "workflows"
        p = wf_dir / fname
        if not p.exists():
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(b"event: error\ndata: Workflow introuvable\n\n")
                self.wfile.flush()
            except Exception:
                pass
            return
        try:
            wf = _parse_workflow_yaml(p.read_text(encoding="utf-8"))
        except Exception as e:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(f"event: error\ndata: Parse error: {e}\n\n".encode("utf-8"))
            self.wfile.flush()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def send(event_type: str, data: dict):
            payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            try:
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return False
            return True

        send("start", {"name": wf.get("name", fname), "file": fname, "jobs": list(wf.get("jobs", {}).keys())})
        env = {}
        cwd = "/"
        for job_name, job in wf.get("jobs", {}).items():
            send("job", {"name": job_name, "steps": len(job.get("steps", []))})
            for i, step in enumerate(job.get("steps", [])):
                step_name = step.get("name", f"step {i+1}")
                step_run = step.get("run", "")
                send("step-start", {"job": job_name, "index": i + 1, "name": step_name, "run": step_run})
                time.sleep(0.05)  # let UI catch up
                # Run each line of step_run sequentially
                for sub_line in step_run.split("\n"):
                    if not sub_line.strip():
                        continue
                    out, cwd, env = run_command(sub_line, cwd, env=env)
                    if out:
                        if not send("output", {"job": job_name, "step": step_name, "text": out}):
                            return
                    time.sleep(0.03)
                send("step-done", {"job": job_name, "index": i + 1, "name": step_name})
            send("job-done", {"name": job_name})
        send("done", {"file": fname, "name": wf.get("name", fname)})

    def do_POST(self):
        try:
            u = urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            data = json.loads(body.decode("utf-8") or "{}")
            token = get_or_create_session(self._cookie_token() or data.get("session"))
            if u.path == "/api/terminal":
                cmd = data.get("cmd", "")
                cwd = data.get("cwd", SESSIONS[token]["cwd"])
                if cwd == "~" or not cwd:
                    cwd = "/"
                SESSIONS[token]["cwd"] = cwd
                SESSIONS[token]["history"].append(cmd)
                if len(SESSIONS[token]["history"]) > 500:
                    SESSIONS[token]["history"] = SESSIONS[token]["history"][-500:]
                env = SESSIONS[token].get("env", {})
                output, new_cwd, new_env = run_command(cmd, cwd, env=env)
                SESSIONS[token]["cwd"] = new_cwd
                SESSIONS[token]["env"] = new_env
                self._json({
                    "output": output,
                    "cwd": new_cwd,
                    "session": token,
                    "prompt": _prompt(new_cwd),
                }, headers={"Set-Cookie": f"kemus_session={token}; Path=/; HttpOnly"})
                return
            if u.path == "/api/file/save":
                rel = data.get("path")
                content = data.get("content", "")
                if not rel:
                    self._json({"error": "path required"}, 400)
                    return
                cwd = SESSIONS[token]["cwd"]
                p = safe_path(rel, cwd)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                self._json({"ok": True, "path": rel, "size": len(content)})
                return
            # ====== v2 POST endpoints ======
            if u.path == "/api/settings/save":
                s = load_settings()
                s.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
                save_settings(s)
                self._json({"ok": True, "settings": s})
                return
            if u.path == "/api/store/install":
                app_id = data.get("id")
                installed = load_installed()
                if app_id and app_id not in installed:
                    installed.append(app_id)
                    save_installed(installed)
                self._json({"ok": True, "installed": installed})
                return
            if u.path == "/api/store/uninstall":
                app_id = data.get("id")
                installed = load_installed()
                if app_id in installed:
                    installed.remove(app_id)
                    save_installed(installed)
                self._json({"ok": True, "installed": installed})
                return
            if u.path == "/api/workflow/save":
                fname = data.get("file", "untitled.yml")
                content = data.get("content", "")
                wf_dir = KEMUS_HOME / "workflows"
                wf_dir.mkdir(parents=True, exist_ok=True)
                if not fname.endswith(".yml"):
                    fname += ".yml"
                (wf_dir / fname).write_text(content, encoding="utf-8")
                self._json({"ok": True, "file": fname})
                return
            self._json({"error": "unknown endpoint"}, 404)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)


def _prompt(cwd: str) -> str:
    short = cwd if cwd == "/" else cwd
    if short == "/" or short == "":
        short = "~"
    elif not short.startswith("~"):
        short = "~" + ("" if short == "/" else short)
    return f"\x1b[32mkemus@nixos\x1b[0m:\x1b[34m{short}\x1b[0m$ "


START_TIME = time.time()


# ----------------------------------------------------------------------------
# Frontend (single embedded HTML page)
# ----------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kemus - PC virtuel</title>
<style>
:root {
  --bg: #0b0f19;
  --panel: rgba(20, 26, 40, 0.92);
  --panel-solid: #141a28;
  --accent: #3daee9;
  --accent-2: #7eb4e6;
  --nix: #5277c3;
  --fedora: #294172;
  --text: #e6e9ef;
  --text-dim: #9aa4b8;
  --win-bg: #1c2230;
  --win-border: #2a3142;
  --term-bg: #0a0d14;
  --term-fg: #c5d0e6;
  --term-font-size: 13px;
  --term-font: "JetBrains Mono", "Consolas", "Noto Sans Mono", monospace;
  --green: #27ae60;
  --red: #e74c3c;
  --yellow: #f1c40f;
  --shadow: 0 8px 28px rgba(0,0,0,0.55);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
  height: 100%; width: 100%; overflow: hidden;
  font-family: "Inter", "Segoe UI", "Noto Sans", system-ui, sans-serif;
  color: var(--text); background: var(--bg); user-select: none;
}
#desktop {
  position: fixed; inset: 0;
  background:
    radial-gradient(ellipse at 30% 20%, rgba(82,119,195,0.35), transparent 60%),
    radial-gradient(ellipse at 75% 80%, rgba(41,65,114,0.45), transparent 55%),
    linear-gradient(135deg, #0b0f19 0%, #11182b 50%, #1a2138 100%);
  overflow: hidden;
}
#desktop::before {
  content: ""; position: absolute; inset: 0;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'><g fill='none' stroke='%235277c3' stroke-width='1.2' opacity='0.06'><path d='M100 20 L130 70 L100 100 L70 70 Z'/><path d='M100 100 L130 130 L100 180 L70 130 Z'/><path d='M100 100 L160 100 L100 130 L40 100 Z'/><path d='M100 100 L160 100 L100 70 L40 100 Z'/></g></svg>");
  background-size: 360px 360px;
  pointer-events: none; opacity: 0.5;
}
/* ---- Top panel (KDE Plasma style) ---- */
#top-panel {
  position: fixed; top: 0; left: 0; right: 0; height: 28px;
  background: var(--panel); backdrop-filter: blur(14px);
  border-bottom: 1px solid rgba(255,255,255,0.06);
  display: flex; align-items: center; padding: 0 8px; z-index: 10000;
  font-size: 13px;
}
#kickoff {
  width: 28px; height: 22px; display: flex; align-items: center; justify-content: center;
  cursor: pointer; border-radius: 4px;
}
#kickoff:hover { background: rgba(255,255,255,0.08); }
#kickoff svg { width: 18px; height: 18px; }
#window-list { flex: 1; display: flex; align-items: center; gap: 2px; padding: 0 6px; overflow: hidden; }
.win-tab {
  padding: 3px 10px; background: rgba(255,255,255,0.05); border-radius: 4px;
  cursor: pointer; font-size: 12px; max-width: 160px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
.win-tab.active { background: var(--accent); color: #fff; }
.win-tab:hover { background: rgba(255,255,255,0.12); }
#sys-tray { display: flex; align-items: center; gap: 10px; padding: 0 6px; color: var(--text-dim); }
#sys-tray .tray-item { cursor: pointer; padding: 2px 4px; border-radius: 3px; }
#sys-tray .tray-item:hover { background: rgba(255,255,255,0.08); color: var(--text); }
#clock { color: var(--text); font-variant-numeric: tabular-nums; font-size: 12px; min-width: 120px; text-align: right; }
/* ---- Bottom panel (icons) ---- */
#dock {
  position: fixed; bottom: 0; left: 50%; transform: translateX(-50%);
  height: 52px; padding: 6px 10px; background: var(--panel);
  backdrop-filter: blur(14px); border-radius: 12px 12px 0 0;
  border: 1px solid rgba(255,255,255,0.06); border-bottom: none;
  display: flex; align-items: center; gap: 6px; z-index: 10000;
}
.dock-icon {
  width: 40px; height: 40px; border-radius: 8px; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  background: rgba(255,255,255,0.04); transition: transform 0.15s, background 0.15s;
}
.dock-icon:hover { background: rgba(61,174,233,0.25); transform: translateY(-4px); }
.dock-icon svg { width: 24px; height: 24px; }
/* ---- Kickoff menu ---- */
#kickoff-menu {
  position: fixed; top: 30px; left: 6px; width: 380px; max-height: 70vh;
  background: var(--panel-solid); border: 1px solid var(--win-border);
  border-radius: 8px; box-shadow: var(--shadow); padding: 12px;
  display: none; flex-direction: column; z-index: 20000; overflow: hidden;
}
#kickoff-menu.open { display: flex; }
.km-search {
  width: 100%; padding: 8px 12px; background: #0d121d;
  border: 1px solid var(--win-border); border-radius: 4px;
  color: var(--text); font-size: 13px; margin-bottom: 10px;
}
.km-search:focus { outline: none; border-color: var(--accent); }
.km-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; overflow-y: auto; }
.km-app {
  padding: 10px 6px; border-radius: 6px; cursor: pointer; text-align: center;
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  font-size: 11px; color: var(--text-dim);
}
.km-app:hover { background: rgba(61,174,233,0.18); color: var(--text); }
.km-app svg { width: 28px; height: 28px; }
.km-footer {
  margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--win-border);
  display: flex; justify-content: space-between; align-items: center;
  font-size: 12px; color: var(--text-dim);
}
.km-power { display: flex; gap: 4px; }
.km-power button {
  background: rgba(255,255,255,0.06); border: none; color: var(--text);
  width: 28px; height: 28px; border-radius: 4px; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
}
.km-power button:hover { background: var(--accent); }
.km-power button svg { width: 14px; height: 14px; }
/* ---- Desktop icons ---- */
#desktop-icons { position: absolute; top: 40px; left: 12px; display: flex; flex-direction: column; gap: 10px; }
.desk-icon {
  width: 80px; padding: 8px 4px; border-radius: 6px; cursor: pointer;
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  font-size: 11px; color: var(--text); text-align: center;
}
.desk-icon:hover { background: rgba(255,255,255,0.08); }
.desk-icon svg { width: 36px; height: 36px; }
/* ---- Windows ---- */
.window {
  position: absolute; min-width: 240px; min-height: 120px;
  background: var(--win-bg); border: 1px solid var(--win-border);
  border-radius: 6px; box-shadow: var(--shadow);
  display: flex; flex-direction: column; overflow: hidden;
}
.window.maximized { border-radius: 0; }
.win-titlebar {
  height: 30px; background: #232a3a; display: flex; align-items: center;
  padding: 0 6px 0 10px; cursor: grab; user-select: none;
  border-bottom: 1px solid var(--win-border);
}
.win-titlebar.dragging { cursor: grabbing; }
.win-title { flex: 1; font-size: 12px; color: var(--text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.win-controls { display: flex; gap: 4px; }
.win-btn {
  width: 18px; height: 18px; border-radius: 3px; border: none;
  background: rgba(255,255,255,0.06); cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  color: var(--text-dim); font-size: 11px;
}
.win-btn:hover { background: rgba(255,255,255,0.18); color: var(--text); }
.win-btn.close:hover { background: var(--red); color: #fff; }
.win-body { flex: 1; overflow: auto; position: relative; }
.win-resize {
  position: absolute; right: 0; bottom: 0; width: 14px; height: 14px;
  cursor: nwse-resize; z-index: 5;
}
/* ---- Terminal ---- */
.term {
  height: 100%; background: var(--term-bg); color: var(--term-fg);
  font-family: var(--term-font);
  font-size: var(--term-font-size); padding: 8px; overflow-y: auto; cursor: text;
}
.term-line { white-space: pre-wrap; word-break: break-word; }
.term-input-line { display: flex; align-items: baseline; }
.term-prompt { white-space: pre; color: var(--accent-2); }
.term-prompt .user { color: var(--green); }
.term-prompt .path { color: var(--accent); }
.term-input {
  flex: 1; background: transparent; border: none; outline: none;
  color: var(--term-fg); font: inherit; padding: 0; margin-left: 4px;
}
/* ---- File manager ---- */
.files { display: flex; height: 100%; }
.files-sidebar { width: 160px; background: #161b29; padding: 8px; border-right: 1px solid var(--win-border); overflow-y: auto; }
.files-sidebar h4 { font-size: 11px; color: var(--text-dim); margin: 8px 0 4px; text-transform: uppercase; }
.files-side-item {
  padding: 5px 8px; border-radius: 4px; cursor: pointer; font-size: 12px;
  display: flex; align-items: center; gap: 6px; color: var(--text);
}
.files-side-item:hover { background: rgba(255,255,255,0.06); }
.files-side-item.active { background: var(--accent); color: #fff; }
.files-main { flex: 1; padding: 12px; overflow-y: auto; }
.files-toolbar { display: flex; gap: 6px; margin-bottom: 8px; align-items: center; font-size: 12px; }
.files-toolbar button {
  background: rgba(255,255,255,0.05); border: 1px solid var(--win-border);
  color: var(--text); padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 12px;
}
.files-toolbar button:hover { background: rgba(61,174,233,0.2); }
.files-path { flex: 1; color: var(--text-dim); padding: 4px 8px; }
.files-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 8px; }
.file-item {
  padding: 10px 6px; border-radius: 4px; cursor: pointer; text-align: center;
  display: flex; flex-direction: column; align-items: center; gap: 4px;
}
.file-item:hover { background: rgba(61,174,233,0.15); }
.file-item svg { width: 36px; height: 36px; }
.file-item .name { font-size: 11px; color: var(--text); word-break: break-word; }
/* ---- Editor ---- */
.editor { display: flex; flex-direction: column; height: 100%; }
.editor-toolbar { display: flex; gap: 6px; padding: 6px; background: #161b29; border-bottom: 1px solid var(--win-border); align-items: center; font-size: 12px; }
.editor-toolbar input { flex: 1; background: #0d121d; border: 1px solid var(--win-border); border-radius: 4px; color: var(--text); padding: 4px 8px; font-size: 12px; }
.editor-toolbar button { background: var(--accent); border: none; color: #fff; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; }
.editor-toolbar button:hover { background: var(--accent-2); }
.editor textarea {
  flex: 1; background: #0a0d14; color: var(--term-fg); border: none; outline: none;
  font-family: "JetBrains Mono", "Consolas", monospace; font-size: 13px;
  padding: 10px; resize: none; line-height: 1.5;
}
.editor-status { padding: 3px 8px; background: #161b29; border-top: 1px solid var(--win-border); font-size: 11px; color: var(--text-dim); display: flex; justify-content: space-between; }
/* ---- Settings ---- */
.settings { padding: 16px; overflow-y: auto; }
.settings h2 { font-size: 16px; margin-bottom: 10px; }
.settings .row { padding: 8px 0; border-bottom: 1px solid var(--win-border); display: flex; justify-content: space-between; font-size: 13px; }
.settings .row span:last-child { color: var(--accent); }
/* ---- Help / About ---- */
.help { padding: 16px; font-size: 13px; line-height: 1.6; }
.help h2 { color: var(--accent); margin-bottom: 8px; }
.help code { background: #0d121d; padding: 1px 5px; border-radius: 3px; color: var(--accent-2); }
.help ul { padding-left: 20px; }
/* ---- Browser (mock) ---- */
.browser { display: flex; flex-direction: column; height: 100%; }
.browser-toolbar { display: flex; gap: 4px; padding: 6px; background: #161b29; border-bottom: 1px solid var(--win-border); }
.browser-toolbar input { flex: 1; background: #0d121d; border: 1px solid var(--win-border); border-radius: 4px; color: var(--text); padding: 4px 8px; font-size: 12px; }
.browser-toolbar button { background: rgba(255,255,255,0.05); border: 1px solid var(--win-border); color: var(--text); padding: 4px 8px; border-radius: 4px; cursor: pointer; }
.browser-frame { flex: 1; background: #fff; }
/* ---- Calculator ---- */
.calc { padding: 8px; background: #161b29; height: 100%; display: flex; flex-direction: column; gap: 4px; }
.calc-display { background: #0a0d14; color: var(--term-fg); padding: 10px; border-radius: 4px; text-align: right; font-family: monospace; font-size: 18px; min-height: 40px; }
.calc-grid { flex: 1; display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; }
.calc-grid button { background: #232a3a; border: none; color: var(--text); font-size: 16px; border-radius: 4px; cursor: pointer; }
.calc-grid button:hover { background: #2d3650; }
.calc-grid button.op { background: var(--accent); }
.calc-grid button.eq { background: var(--green); color: #fff; }
/* ---- Toast ---- */
#toast-wrap { position: fixed; bottom: 70px; right: 12px; display: flex; flex-direction: column; gap: 6px; z-index: 30000; }
.toast {
  background: var(--panel-solid); border-left: 3px solid var(--green);
  color: var(--text); padding: 8px 14px; border-radius: 4px; font-size: 12px;
  box-shadow: var(--shadow); animation: slideIn 0.2s ease;
}
@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
/* ---- Boot screen ---- */
#boot {
  position: fixed; inset: 0; background: #0b0f19; z-index: 99999;
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 20px;
}
#boot .logo svg { width: 100px; height: 100px; }
#boot .progress { width: 200px; height: 4px; background: #1a2138; border-radius: 2px; overflow: hidden; }
#boot .progress-bar { height: 100%; background: var(--accent); width: 0%; transition: width 0.3s; }
#boot .text { color: var(--text-dim); font-size: 12px; }
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.3); }
</style>
</head>
<body>
<div id="boot">
  <div class="logo">
    <svg viewBox="0 0 200 200"><g fill="none" stroke="#5277c3" stroke-width="2"><path d="M100 20 L130 70 L100 100 L70 70 Z"/><path d="M100 100 L130 130 L100 180 L70 130 Z"/><path d="M100 100 L160 100 L100 130 L40 100 Z"/><path d="M100 100 L160 100 L100 70 L40 100 Z"/></g><circle cx="100" cy="100" r="4" fill="#3daee9"/></svg>
  </div>
  <div class="progress"><div class="progress-bar" id="boot-bar"></div></div>
  <div class="text" id="boot-text">Demarrage de KemusOS 1.0...</div>
</div>

<div id="desktop">
  <div id="top-panel">
    <div id="kickoff" title="Menu">
      <svg viewBox="0 0 200 200"><g fill="none" stroke="#7eb4e6" stroke-width="6"><path d="M100 20 L130 70 L100 100 L70 70 Z"/><path d="M100 100 L130 130 L100 180 L70 130 Z"/><path d="M100 100 L160 100 L100 130 L40 100 Z"/><path d="M100 100 L160 100 L100 70 L40 100 Z"/></g></svg>
    </div>
    <div id="window-list"></div>
    <div id="sys-tray">
      <span class="tray-item" title="Reseau">📶</span>
      <span class="tray-item" title="Volume">🔊</span>
      <span class="tray-item" title="Batterie">🔋</span>
      <span id="clock">--:--:--</span>
    </div>
  </div>

  <div id="desktop-icons"></div>

  <div id="kickoff-menu">
    <input class="km-search" placeholder="Rechercher une application..." id="km-search-input">
    <div class="km-grid" id="km-grid"></div>
    <div class="km-footer">
      <span>kemus @ nixos</span>
      <div class="km-power">
        <button title="Verrouiller" onclick="toast('Verrouillage simule')">🔒</button>
        <button title="Redemarrer" onclick="if(confirm('Redemarrer KemusOS ?')) location.reload()">🔄</button>
        <button title="Eteindre" onclick="if(confirm('Eteindre KemusOS ?')) document.getElementById('desktop').style.display='none'">⏻</button>
      </div>
    </div>
  </div>

  <div id="dock"></div>
  <div id="toast-wrap"></div>
</div>

<script>
// ====== State ======
let zTop = 100;
let windows = {};
let winSeq = 0;
let activeWin = null;
let session = null;

// ====== API helpers ======
async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" }, credentials: "same-origin" };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  const text = await r.text();
  let json = null; try { json = JSON.parse(text); } catch (e) {}
  if (!r.ok && json) throw new Error(json.error || r.status);
  return json || {};
}

// ====== Toast ======
function toast(msg) {
  const w = document.getElementById("toast-wrap");
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = msg;
  w.appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity 0.4s"; setTimeout(() => t.remove(), 400); }, 3000);
}

// ====== Apps registry ======
const APPS = {
  terminal: { name: "Terminal", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#27ae60" stroke-width="2"><rect x="2" y="3" width="20" height="18" rx="2"/><path d="M6 8 L10 12 L6 16"/><line x1="12" y1="16" x2="18" y2="16"/></svg>', open: openTerminal },
  files: { name: "Fichiers", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#f1c40f" stroke-width="2"><path d="M3 7 L3 19 L21 19 L21 9 L12 9 L9 7 Z"/></svg>', open: openFiles },
  editor: { name: "Editeur", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#3daee9" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5 a2.121 2.121 0 0 1 3 3 L7 19 l-4 1 1 -4 Z"/></svg>', open: () => openEditor(null) },
  settings: { name: "Pro Reglages", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#9aa4b8" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>', open: openSettings },
  help: { name: "Aide", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#e74c3c" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12" y2="17"/></svg>', open: openHelp },
  browser: { name: "KemusBrowser", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#7eb4e6" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2 a15 15 0 0 1 4 10 a15 15 0 0 1 -4 10 a15 15 0 0 1 -4 -10 a15 15 0 0 1 4 -10 z"/></svg>', open: openBrowser },
  calc: { name: "Calculatrice", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#f1c40f" stroke-width="2"><rect x="4" y="2" width="16" height="20" rx="2"/><line x1="8" y1="6" x2="16" y2="6"/></svg>', open: openCalc },
  neofetch: { name: "Systeme", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#5277c3" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/></svg>', open: () => openTerminalWithCmd("neofetch") },
  // === v2 apps ===
  store: { name: "Logitheque", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#9b59b6" stroke-width="2"><path d="M3 9 L21 9 L21 21 L3 21 Z"/><path d="M3 9 L7 4 L17 4 L21 9"/><line x1="9" y1="13" x2="15" y2="13"/></svg>', open: openStore },
  workflow: { name: "Workflow Studio", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#27ae60" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>', open: openWorkflow },
  monitor: { name: "Process Monitor", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#e67e22" stroke-width="2"><path d="M3 3v18h18"/><path d="M7 14l4-4 4 4 6-6"/></svg>', open: openMonitor },
  logs: { name: "Logs Viewer", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#7eb4e6" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/></svg>', open: openLogs },
  man: { name: "Man Pages", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#34495e" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>', open: openMan },
  markdown: { name: "Markdown", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#e74c3c" stroke-width="2"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M7 15V9l3 3 3-3v6"/><path d="M18 9v6M16 13l2 2 2-2"/></svg>', open: openMarkdown },
  notes: { name: "Notes", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#f1c40f" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>', open: openNotes },
  tasks: { name: "Taches", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#27ae60" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>', open: openTasks },
  calendar: { name: "Calendrier", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#e74c3c" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/></svg>', open: openCalendar },
  clock: { name: "Horloge", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#7eb4e6" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>', open: openClock },
  color: { name: "Color Picker", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#9b59b6" stroke-width="2"><circle cx="13.5" cy="6.5" r=".5"/><circle cx="17.5" cy="10.5" r=".5"/><circle cx="8.5" cy="7.5" r=".5"/><circle cx="6.5" cy="12.5" r=".5"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.555C21.965 6.012 17.461 2 12 2z"/></svg>', open: openColorPicker },
  dictionary: { name: "Dictionnaire", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#34495e" stroke-width="2"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>', open: openDictionary },
  imageviewer: { name: "Image Viewer", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#f39c12" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>', open: openImageViewer },
  code: { name: "Kode", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#3daee9" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>', open: () => openEditor(null) },
  git: { name: "Git GUI", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#e74c3c" stroke-width="2"><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9v6M6 15V9a3 3 0 0 0 3-3"/></svg>', open: openGit },
  screenshot: { name: "Screenshot", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#7eb4e6" stroke-width="2"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>', open: openScreenshot },
  disks: { name: "Disk Usage", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#7eb4e6" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>', open: openDisks },
  packages: { name: "Paquets", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#9b59b6" stroke-width="2"><line x1="16.5" y1="7.5" x2="7.5" y2="16.5"/><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>', open: openPackages },
  firewall: { name: "Firewall", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#e74c3c" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>', open: openFirewall },
  network: { name: "Network", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#27ae60" stroke-width="2"><path d="M5 12.55a11 11 0 0 1 14.08 0M1.42 9a16 16 0 0 1 21.16 0M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01"/></svg>', open: openNetwork },
  sensors: { name: "Sensors", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#e67e22" stroke-width="2"><path d="M14 14.76V3.5a2.5 2.5 0 0 0-5 0v11.26a4.5 4.5 0 1 0 5 0z"/></svg>', open: openSensors },
  battery: { name: "Battery", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#27ae60" stroke-width="2"><rect x="1" y="6" width="18" height="12" rx="2"/><line x1="23" y1="13" x2="23" y2="11"/></svg>', open: openBattery },
  updates: { name: "Updates", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#3daee9" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>', open: openUpdates },
  backup: { name: "Backup", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#7eb4e6" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>', open: openBackup },
  diagnostics: { name: "Diagnostics", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#e74c3c" stroke-width="2"><path d="M3 12h4l3 9 4-18 3 9h4"/></svg>', open: openDiagnostics },
  weather: { name: "Meteo", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#f1c40f" stroke-width="2"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>', open: openWeather },
  music: { name: "Music", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#9b59b6" stroke-width="2"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>', open: openMusic },
  video: { name: "Video", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#e74c3c" stroke-width="2"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>', open: openVideo },
  pdf: { name: "PDF Viewer", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#e74c3c" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/></svg>', open: openPdf },
  mail: { name: "Courriel", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#3daee9" stroke-width="2"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>', open: openMail },
  news: { name: "News", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#f39c12" stroke-width="2"><path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2"/><path d="M18 14h-8M15 18h-5M10 6h8v4h-8V6z"/></svg>', open: openNews },
  translator: { name: "Traducteur", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#27ae60" stroke-width="2"><path d="M3 5h12M9 3v2c0 4.42-3.58 8-8 8M5 9c0 2.42 3.58 4 8 4M21 21l-6-6m0 0l6-6m-6 6h6"/></svg>', open: openTranslator },
  regex: { name: "Regex", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#9b59b6" stroke-width="2">.*</svg>', open: openRegex },
  mines: { name: "Demineur", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#e74c3c" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="2" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="22"/></svg>', open: openMines },
  snake: { name: "Snake", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#27ae60" stroke-width="2"><path d="M3 12c0 4 4 4 4 0s4-4 4 0 4 4 4 0 4-4 4 0"/></svg>', open: openSnake },
  "2048": { name: "2048", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#f39c12" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/><line x1="15" y1="3" x2="15" y2="21"/></svg>', open: open2048 },
  tetris: { name: "Tetris", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="#3daee9" stroke-width="2"><rect x="3" y="3" width="6" height="6"/><rect x="9" y="3" width="6" height="6"/><rect x="15" y="3" width="6" height="6"/><rect x="9" y="9" width="6" height="6"/></svg>', open: openComingSoon },
};

// ====== Window manager ======
function createWindow(opts) {
  const id = "win" + (++winSeq);
  const w = document.createElement("div");
  w.className = "window";
  w.id = id;
  w.style.left = (60 + (winSeq * 28) % 200) + "px";
  w.style.top = (50 + (winSeq * 28) % 120) + "px";
  w.style.width = (opts.width || 560) + "px";
  w.style.height = (opts.height || 380) + "px";
  w.style.zIndex = ++zTop;
  w.innerHTML = `
    <div class="win-titlebar">
      <span class="win-title">${opts.title}</span>
      <div class="win-controls">
        <button class="win-btn min" title="Reduire">—</button>
        <button class="win-btn max" title="Agrandir">▢</button>
        <button class="win-btn close" title="Fermer">✕</button>
      </div>
    </div>
    <div class="win-body"></div>
    <div class="win-resize"></div>
  `;
  document.getElementById("desktop").appendChild(w);
  const body = w.querySelector(".win-body");
  if (typeof opts.content === "string") body.innerHTML = opts.content;
  else if (opts.content) body.appendChild(opts.content);

  // Controls
  w.querySelector(".close").onclick = (e) => { e.stopPropagation(); closeWindow(id); };
  w.querySelector(".min").onclick = (e) => { e.stopPropagation(); w.style.display = "none"; updateTaskbar(); };
  w.querySelector(".max").onclick = (e) => { e.stopPropagation(); toggleMax(id); };
  w.querySelector(".win-titlebar").addEventListener("mousedown", (e) => startDrag(id, e));
  w.querySelector(".win-resize").addEventListener("mousedown", (e) => startResize(id, e));
  w.addEventListener("mousedown", () => focusWindow(id));

  windows[id] = { el: w, title: opts.title, minimized: false, maximized: false, prev: null };
  focusWindow(id);
  updateTaskbar();
  return { id, body };
}

function focusWindow(id) {
  if (!windows[id]) return;
  windows[id].el.style.zIndex = ++zTop;
  activeWin = id;
  updateTaskbar();
}

function closeWindow(id) {
  if (!windows[id]) return;
  windows[id].el.remove();
  delete windows[id];
  updateTaskbar();
}

function toggleMax(id) {
  const w = windows[id]; if (!w) return;
  if (w.maximized) {
    const p = w.prev;
    w.el.style.left = p.left; w.el.style.top = p.top;
    w.el.style.width = p.width; w.el.style.height = p.height;
    w.el.classList.remove("maximized");
    w.maximized = false;
  } else {
    w.prev = { left: w.el.style.left, top: w.el.style.top, width: w.el.style.width, height: w.el.style.height };
    w.el.style.left = "0"; w.el.style.top = "28px";
    w.el.style.width = "100%"; w.el.style.height = "calc(100% - 80px)";
    w.el.classList.add("maximized");
    w.maximized = true;
  }
}

function startDrag(id, e) {
  if (e.target.classList.contains("win-btn")) return;
  const w = windows[id]; if (!w || w.maximized) return;
  focusWindow(id);
  const tb = windows[id].el.querySelector(".win-titlebar");
  tb.classList.add("dragging");
  const startX = e.clientX, startY = e.clientY;
  const left = parseInt(w.el.style.left), top = parseInt(w.el.style.top);
  const onMove = (ev) => {
    w.el.style.left = (left + ev.clientX - startX) + "px";
    w.el.style.top = Math.max(28, top + ev.clientY - startY) + "px";
  };
  const onUp = () => {
    tb.classList.remove("dragging");
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
  };
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
  e.preventDefault();
}

function startResize(id, e) {
  e.stopPropagation();
  const w = windows[id]; if (!w) return;
  const startX = e.clientX, startY = e.clientY;
  const width = parseInt(w.el.style.width), height = parseInt(w.el.style.height);
  const onMove = (ev) => {
    w.el.style.width = Math.max(240, width + ev.clientX - startX) + "px";
    w.el.style.height = Math.max(120, height + ev.clientY - startY) + "px";
  };
  const onUp = () => {
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
  };
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
}

function updateTaskbar() {
  const list = document.getElementById("window-list");
  list.innerHTML = "";
  Object.entries(windows).forEach(([id, w]) => {
    const tab = document.createElement("div");
    tab.className = "win-tab" + (id === activeWin && !w.minimized ? " active" : "");
    tab.textContent = w.title;
    tab.onclick = () => {
      if (w.minimized || w.el.style.display === "none") {
        w.el.style.display = "flex"; w.minimized = false; focusWindow(id);
      } else if (id === activeWin) {
        w.el.style.display = "none"; w.minimized = true; updateTaskbar();
      } else { focusWindow(id); }
    };
    list.appendChild(tab);
  });
}

// ====== Terminal ======
function openTerminal() {
  const { id, body } = createWindow({ title: "Terminal — kemus@nixos", width: 640, height: 400 });
  body.innerHTML = `<div class="term" id="term-${id}"></div>`;
  initTerminal(id);
}

function openTerminalWith(cmd) {
  const { id, body } = createWindow({ title: "Terminal — kemus@nixos", width: 640, height: 400 });
  body.innerHTML = `<div class="term" id="term-${id}"></div>`;
  initTerminal(id, cmd);
}

function initTerminal(id, initialCmd) {
  const termEl = document.getElementById("term-" + id);
  let cwd = "/";
  let history = [];
  let histIdx = -1;
  const writeLine = (html, plain) => {
    const div = document.createElement("div");
    div.className = "term-line";
    div.innerHTML = html;
    termEl.appendChild(div);
    termEl.scrollTop = termEl.scrollHeight;
  };
  const writeRaw = (text) => {
    // Convert ANSI basic + escape sequences
    let html = text
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/\x1b\[32m/g, '<span style="color:#27ae60">')
      .replace(/\x1b\[34m/g, '<span style="color:#3daee9">')
      .replace(/\x1b\[31m/g, '<span style="color:#e74c3c">')
      .replace(/\x1b\[33m/g, '<span style="color:#f1c40f">')
      .replace(/\x1b\[36m/g, '<span style="color:#7eb4e6">')
      .replace(/\x1b\[0m/g, "</span>")
      .replace(/\x1bc/g, ""); // clear
    // Custom escapes:
    if (text.includes("\x1bCLOSE_TERM\x1b")) { closeWindow(id); return; }
    if (text.includes("\x1bOPEN_EDITOR:")) {
      const m = text.match(/\x1bOPEN_EDITOR:([^\x1b]+)\x1b/);
      if (m) openEditor(m[1]);
      html = html.replace(/\x1bOPEN_EDITOR:[^\x1b]+\x1b/g, "");
    }
    if (text.includes("\x1bTOAST:")) {
      const m = text.match(/\x1bTOAST:([^\x1b]+)\x1b/);
      if (m) toast(m[1]);
      html = html.replace(/\x1bTOAST:[^\x1b]+\x1b/g, "");
    }
    const div = document.createElement("div");
    div.className = "term-line";
    div.innerHTML = html;
    termEl.appendChild(div);
    termEl.scrollTop = termEl.scrollHeight;
  };

  const promptHTML = (cwdDisp) => {
    let d = cwdDisp === "/" ? "~" : cwdDisp;
    return `<span class="user">kemus@nixos</span>:<span class="path">${d}</span>$ `;
  };

  const newInputLine = () => {
    const line = document.createElement("div");
    line.className = "term-input-line";
    line.innerHTML = `<span class="term-prompt">${promptHTML(cwd)}</span><input class="term-input" autocomplete="off" spellcheck="false">`;
    termEl.appendChild(line);
    const input = line.querySelector("input");
    input.focus();
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const cmd = input.value;
        input.disabled = true;
        if (cmd.trim()) { history.push(cmd); histIdx = history.length; }
        runCmd(cmd);
      } else if (e.key === "ArrowUp") {
        if (histIdx > 0) { histIdx--; input.value = history[histIdx] || ""; }
        e.preventDefault();
      } else if (e.key === "ArrowDown") {
        if (histIdx < history.length - 1) { histIdx++; input.value = history[histIdx] || ""; }
        else { histIdx = history.length; input.value = ""; }
        e.preventDefault();
      } else if (e.key === "Tab") {
        e.preventDefault();
      } else if (e.ctrlKey && e.key === "l") {
        e.preventDefault();
        termEl.innerHTML = "";
        newInputLine();
      } else if (e.ctrlKey && e.key === "c") {
        input.disabled = true;
        writeLine('<span style="color:#e74c3c">^C</span>');
        newInputLine();
      }
    });
    termEl.scrollTop = termEl.scrollHeight;
  };

  const runCmd = async (cmd) => {
    if (cmd.trim() === "clear") { termEl.innerHTML = ""; newInputLine(); return; }
    try {
      const r = await api("POST", "/api/terminal", { cmd, cwd, session });
      session = r.session;
      if (r.output && r.output.includes("\x1bc")) {
        termEl.innerHTML = "";
      } else if (r.output) {
        writeRaw(r.output);
      }
      cwd = r.cwd;
    } catch (e) {
      writeRaw("Erreur: " + e.message + "\n");
    }
    newInputLine();
  };

  termEl.addEventListener("click", () => {
    const inputs = termEl.querySelectorAll("input.term-input");
    inputs[inputs.length - 1]?.focus();
  });

  // Welcome
  writeRaw("KemusOS 2.0 - Terminal reel (pipes, redirections, env vars)\n");
  writeRaw("Tapez 'help' pour 55+ commandes, 'man CMD' pour la doc.\n");
  writeRaw("Nouveau : Logitheque, Workflow Studio, Browser DuckDuckGo, 8 themes.\n\n");
  newInputLine();

  if (initialCmd) {
    setTimeout(() => {
      const lastInput = termEl.querySelector("input.term-input");
      if (lastInput) { lastInput.value = initialCmd; lastInput.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" })); }
    }, 50);
  }
}

// ====== File manager ======
async function openFiles() {
  const { id, body } = createWindow({ title: "Fichiers —kemus", width: 700, height: 460 });
  body.innerHTML = `
    <div class="files">
      <div class="files-sidebar">
        <h4>Emplacements</h4>
        <div class="files-side-item" data-path="/">🏠 Accueil</div>
        <div class="files-side-item" data-path="/Documents">📄 Documents</div>
        <div class="files-side-item" data-path="/Downloads">⬇️ Telechargements</div>
        <div class="files-side-item" data-path="/Pictures">🖼️ Images</div>
        <div class="files-side-item" data-path="/Music">🎵 Musique</div>
        <div class="files-side-item" data-path="/Projects">💻 Projets</div>
      </div>
      <div class="files-main">
        <div class="files-toolbar">
          <button id="files-up">↑</button>
          <button id="files-newdir">+ Dossier</button>
          <button id="files-newfile">+ Fichier</button>
          <span class="files-path" id="files-path">/</span>
        </div>
        <div class="files-grid" id="files-grid"></div>
      </div>
    </div>`;
  let cwd = "/";
  const refresh = async () => {
    body.querySelector("#files-path").textContent = cwd === "/" ? "/ (Accueil)" : cwd;
    try {
      const r = await api("GET", "/api/fs?path=" + encodeURIComponent(cwd));
      const grid = body.querySelector("#files-grid");
      grid.innerHTML = "";
      if (r.items.length === 0) {
        grid.innerHTML = '<div style="color:#9aa4b8;grid-column:1/-1;text-align:center;padding:30px">Dossier vide</div>';
      }
      r.items.forEach(it => {
        const div = document.createElement("div");
        div.className = "file-item";
        const icon = it.type === "dir"
          ? '<svg viewBox="0 0 24 24" fill="#f1c40f"><path d="M3 7 L3 19 L21 19 L21 9 L12 9 L9 7 Z"/></svg>'
          : '<svg viewBox="0 0 24 24" fill="#3daee9"><path d="M6 2 H14 L18 6 V22 H6 Z"/></svg>';
        div.innerHTML = icon + '<div class="name">' + it.name + '</div>';
        div.ondblclick = () => {
          if (it.type === "dir") {
            cwd = (cwd === "/" ? "" : cwd) + "/" + it.name;
            refresh();
          } else {
            openEditor((cwd === "/" ? "" : cwd) + "/" + it.name);
          }
        };
        grid.appendChild(div);
      });
    } catch (e) { toast("Erreur: " + e.message); }
  };
  body.querySelectorAll(".files-side-item").forEach(el => {
    el.onclick = () => { cwd = el.dataset.path; refresh(); };
  });
  body.querySelector("#files-up").onclick = () => {
    if (cwd === "/") return;
    const parts = cwd.split("/").filter(Boolean);
    parts.pop();
    cwd = "/" + parts.join("/");
    if (cwd === "/") cwd = "/";
    refresh();
  };
  body.querySelector("#files-newdir").onclick = async () => {
    const name = prompt("Nom du dossier :");
    if (!name) return;
    try {
      await api("POST", "/api/terminal", { cmd: `mkdir ${name}`, cwd });
      refresh();
    } catch (e) { toast(e.message); }
  };
  body.querySelector("#files-newfile").onclick = async () => {
    const name = prompt("Nom du fichier :");
    if (!name) return;
    try {
      await api("POST", "/api/terminal", { cmd: `touch ${name}`, cwd });
      refresh();
    } catch (e) { toast(e.message); }
  };
  refresh();
}

// ====== Editor ======
async function openEditor(path) {
  const { id, body } = createWindow({ title: path ? ("Editeur — " + path) : "Editeur — sans titre", width: 600, height: 420 });
  body.innerHTML = `
    <div class="editor">
      <div class="editor-toolbar">
        <input id="ed-path" value="${path || ""}" placeholder="/chemin/du/fichier">
        <button id="ed-save">Enregistrer</button>
      </div>
      <textarea id="ed-text" spellcheck="false" placeholder="Saisissez votre texte..."></textarea>
      <div class="editor-status"><span id="ed-info">Pret</span><span id="ed-count">0 caract.</span></div>
    </div>`;
  const ta = body.querySelector("#ed-text");
  const info = body.querySelector("#ed-info");
  const count = body.querySelector("#ed-count");
  if (path) {
    try {
      const r = await api("GET", "/api/file?path=" + encodeURIComponent(path));
      ta.value = r.content; info.textContent = "Charge : " + path;
    } catch (e) { info.textContent = "Nouveau fichier"; }
  }
  const updateCount = () => count.textContent = ta.value.length + " caract.";
  ta.addEventListener("input", updateCount); updateCount();
  body.querySelector("#ed-save").onclick = async () => {
    const p = body.querySelector("#ed-path").value.trim();
    if (!p) { toast("Indiquez un chemin"); return; }
    try {
      await api("POST", "/api/file/save", { path: p, content: ta.value });
      info.textContent = "Enregistre : " + p + " (" + ta.value.length + " octets)";
      toast("Fichier enregistre");
    } catch (e) { toast(e.message); }
  };
}

// ====== Settings ======
async function openSettings() {
  const { id, body } = createWindow({ title: "Parametres", width: 480, height: 380 });
  let info = {};
  try { info = await api("GET", "/api/desktop"); } catch (e) {}
  body.innerHTML = `
    <div class="settings">
      <h2>Systeme</h2>
      <div class="row"><span>Distribution</span><span>${info.distro || "KemusOS"}</span></div>
      <div class="row"><span>Version</span><span>${info.version || "1.0"}</span></div>
      <div class="row"><span>Utilisateur</span><span>${info.user || "kemus"}</span></div>
      <div class="row"><span>Hote</span><span>${info.host || "—"}</span></div>
      <div class="row"><span>Port serveur</span><span>${info.port || 3922}</span></div>
      <div class="row"><span>Theme</span><span>Breeze-Dark + NixSnow</span></div>
      <div class="row"><span>Backend</span><span>Python stdlib</span></div>
      <div class="row"><span>VFS racine</span><span>~/.kemus/home</span></div>
      <h2 style="margin-top:14px">A propos</h2>
      <p style="color:#9aa4b8;font-size:12px;line-height:1.6">
        Kemus est un PC virtuel leger, sans dependance externe, inspire de KDE Plasma,
        NixOS et Fedora. Tout le code tient dans un seul fichier Python.
      </p>
    </div>`;
}

// ====== Help ======
function openHelp() {
  const { id, body } = createWindow({ title: "Aide Kemus", width: 480, height: 380 });
  body.innerHTML = `
    <div class="help">
      <h2>Bienvenue dans Kemus</h2>
      <p>PC virtuel leger, inspire KDE Plasma + NixOS + Fedora.</p>
      <h3 style="margin-top:12px;color:#3daee9">Raccourcis Terminal</h3>
      <ul>
        <li><code>help</code> — liste des commandes</li>
        <li><code>neofetch</code> — info systeme</li>
        <li><code>ls</code>, <code>cd</code>, <code>cat</code>, <code>mkdir</code>, <code>touch</code>, <code>rm</code></li>
        <li><code>edit &lt;file&gt;</code> — ouvrir dans l'editeur</li>
        <li><code>python -c "print(1+1)"</code> — executer du Python</li>
        <li><code>apt install firefox</code> — simulation gestionnaire</li>
        <li><code>Ctrl+L</code> — effacer ; <code>Ctrl+C</code> — interrompre</li>
      </ul>
      <h3 style="margin-top:12px;color:#3daee9">Fenetres</h3>
      <ul>
        <li>Glisser la barre de titre pour deplacer</li>
        <li>Corner bas-droite pour redimensionner</li>
        <li>Boutons — / ▢ / ✕ pour reduire / agrandir / fermer</li>
      </ul>
    </div>`;
}

// ====== Browser (mock) ======
function openBrowser() {
  const { id, body } = createWindow({ title: "Navigateur", width: 700, height: 460 });
  body.innerHTML = `
    <div class="browser">
      <div class="browser-toolbar">
        <button>←</button><button>→</button><button>⟳</button>
        <input id="br-url" value="kemus://welcome" placeholder="URL ou recherche">
        <button id="br-go">Go</button>
      </div>
      <iframe class="browser-frame" id="br-frame" sandbox></iframe>
    </div>`;
  const frame = body.querySelector("#br-frame");
  const go = () => {
    const u = body.querySelector("#br-url").value;
    frame.srcdoc = `<!DOCTYPE html><html><body style="font-family:sans-serif;padding:20px;background:#fff;color:#000">
      <h1>Bienvenue dans KemusBrowser</h1>
      <p>URL demandee : <code>${u}</code></p>
      <p>Ceci est un navigateur simule. Pour un vrai navigateur, utilisez Firefox.</p>
      <p>Le PC virtuel Kemus tourne sur le port 3922.</p>
    </body></html>`;
  };
  body.querySelector("#br-go").onclick = go;
  body.querySelector("#br-url").addEventListener("keydown", e => { if (e.key === "Enter") go(); });
  go();
}

// ====== Pro Reglages (v2) ======
async function openSettings() {
  const { id, body } = createWindow({ title: "Pro Reglages", width: 620, height: 480 });
  let info = {}; try { info = await api("GET", "/api/desktop"); } catch (e) {}
  let themes = {}; let s = {};
  try { const r = await api("GET", "/api/themes"); themes = r.themes; } catch (e) {}
  try { s = await api("GET", "/api/settings"); } catch (e) {}
  const themeNames = Object.keys(themes);
  body.innerHTML = `
    <div class="settings" style="overflow-y:auto;padding:14px">
      <div class="settings-tabs" style="display:flex;gap:4px;border-bottom:1px solid var(--win-border);margin-bottom:10px">
        <button class="st-tab active" data-tab="appear">Apparence</button>
        <button class="st-tab" data-tab="term">Terminal</button>
        <button class="st-tab" data-tab="sys">Systeme</button>
        <button class="st-tab" data-tab="adv">Avance</button>
      </div>
      <div class="st-panel" data-panel="appear">
        <h2 style="font-size:14px;margin-bottom:8px">Theme</h2>
        <div class="theme-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px">
          ${themeNames.map(t => `
            <div class="theme-card" data-theme="${t}" style="cursor:pointer;padding:8px;border-radius:6px;border:2px solid ${s.theme===t?'var(--accent)':'var(--win-border)'};background:${themes[t].bg};color:${themes[t].text}">
              <div style="display:flex;gap:4px;margin-bottom:4px">
                <div style="width:14px;height:14px;border-radius:2px;background:${themes[t].accent}"></div>
                <div style="width:14px;height:14px;border-radius:2px;background:${themes[t].win_bg}"></div>
              </div>
              <div style="font-size:11px">${t}</div>
            </div>`).join("")}
        </div>
        <h2 style="font-size:14px;margin-bottom:8px">Couleur accent</h2>
        <div style="display:flex;gap:6px;margin-bottom:14px">
          ${["#3daee9","#5277c3","#294172","#bd93f9","#2aa198","#7aa2f7","#88c0d0","#e74c3c","#27ae60","#f39c12"].map(c => `
            <div class="accent-swatch" data-color="${c}" style="width:24px;height:24px;border-radius:50%;background:${c};cursor:pointer;border:2px solid ${s.accent===c?'#fff':'transparent'}"></div>`).join("")}
        </div>
        <h2 style="font-size:14px;margin-bottom:8px">Wallpaper</h2>
        <select id="st-wallpaper" style="background:#0d121d;border:1px solid var(--win-border);color:var(--text);padding:4px 8px;border-radius:4px;width:100%">
          ${["nixos","fedora","plasma","gradient","solid"].map(w => `<option value="${w}" ${s.wallpaper===w?'selected':''}>${w}</option>`).join("")}
        </select>
      </div>
      <div class="st-panel" data-panel="term" style="display:none">
        <h2 style="font-size:14px;margin-bottom:8px">Police terminal</h2>
        <select id="st-font" style="background:#0d121d;border:1px solid var(--win-border);color:var(--text);padding:4px 8px;border-radius:4px;width:100%;margin-bottom:10px">
          ${["JetBrains Mono","Fira Code","Consolas","Noto Sans Mono","monospace"].map(f => `<option ${s.term_font===f?'selected':''}>${f}</option>`).join("")}
        </select>
        <h2 style="font-size:14px;margin-bottom:8px">Taille police : <span id="st-size-val">${s.term_font_size||13}</span>px</h2>
        <input type="range" id="st-size" min="10" max="22" value="${s.term_font_size||13}" style="width:100%">
      </div>
      <div class="st-panel" data-panel="sys" style="display:none">
        <h2 style="font-size:14px;margin-bottom:8px">Systeme</h2>
        <div class="row"><span>Distribution</span><span>${info.distro||"KemusOS"}</span></div>
        <div class="row"><span>Version</span><span>${info.version||"2.0"}</span></div>
        <div class="row"><span>Utilisateur</span><span>${info.user||"kemus"}</span></div>
        <div class="row"><span>Hote</span><span>${info.host||"—"}</span></div>
        <div class="row"><span>Port serveur</span><span>${info.port||3922}</span></div>
        <div class="row"><span>Backend</span><span>Python stdlib</span></div>
        <div class="row"><span>VFS racine</span><span>~/.kemus/home</span></div>
        <div class="row"><span>Workflows</span><span>~/.kemus/workflows</span></div>
      </div>
      <div class="st-panel" data-panel="adv" style="display:none">
        <h2 style="font-size:14px;margin-bottom:8px">Avance</h2>
        <button id="st-reset" style="background:#e74c3c;border:none;color:#fff;padding:6px 12px;border-radius:4px;cursor:pointer;margin-bottom:8px">Reset VFS (efface ~/.kemus/home)</button>
        <button id="st-export" style="background:var(--accent);border:none;color:#fff;padding:6px 12px;border-radius:4px;cursor:pointer;margin-bottom:8px">Exporter settings (JSON)</button>
        <h2 style="font-size:14px;margin:14px 0 8px">A propos</h2>
        <p style="color:var(--text-dim);font-size:12px;line-height:1.6">Kemus v2.0 - PC virtuel leger. Inspirations : KDE Plasma, NixOS, Fedora. 55+ commandes terminal, 8 themes, app store, workflow runner, browser reel DuckDuckGo. Tout le code tient dans un seul fichier Python.</p>
      </div>
    </div>`;
  // Tab switching
  body.querySelectorAll(".st-tab").forEach(t => {
    t.style.cssText = "background:rgba(255,255,255,0.05);border:none;color:var(--text-dim);padding:6px 12px;border-radius:4px 4px 0 0;cursor:pointer;font-size:12px";
    if (t.classList.contains("active")) t.style.background = "var(--accent)", t.style.color = "#fff";
    t.onclick = () => {
      body.querySelectorAll(".st-tab").forEach(x => { x.classList.remove("active"); x.style.background="rgba(255,255,255,0.05)"; x.style.color="var(--text-dim)"; });
      t.classList.add("active"); t.style.background="var(--accent)"; t.style.color="#fff";
      body.querySelectorAll(".st-panel").forEach(p => p.style.display = "none");
      body.querySelector(`.st-panel[data-panel="${t.dataset.tab}"]`).style.display = "block";
    };
  });
  // Theme cards
  body.querySelectorAll(".theme-card").forEach(card => {
    card.onclick = async () => {
      const t = card.dataset.theme;
      s.theme = t;
      s.accent = themes[t].accent;
      applyTheme(s);
      await api("POST", "/api/settings/save", s);
      toast("Theme : " + t);
      // refresh cards border
      body.querySelectorAll(".theme-card").forEach(c => c.style.border = "2px solid " + (c.dataset.theme === t ? "var(--accent)" : "var(--win-border)"));
    };
  });
  // Accent swatches
  body.querySelectorAll(".accent-swatch").forEach(sw => {
    sw.onclick = async () => {
      s.accent = sw.dataset.color;
      applyTheme(s);
      await api("POST", "/api/settings/save", s);
      toast("Accent : " + sw.dataset.color);
    };
  });
  // Size slider
  const sizeSlider = body.querySelector("#st-size");
  if (sizeSlider) {
    sizeSlider.oninput = async () => {
      body.querySelector("#st-size-val").textContent = sizeSlider.value;
      s.term_font_size = parseInt(sizeSlider.value);
      document.documentElement.style.setProperty("--term-font-size", sizeSlider.value + "px");
    };
    sizeSlider.onchange = async () => { await api("POST", "/api/settings/save", s); toast("Taille : " + sizeSlider.value + "px"); };
  }
  // Font select
  const fontSel = body.querySelector("#st-font");
  if (fontSel) {
    fontSel.onchange = async () => {
      s.term_font = fontSel.value;
      document.documentElement.style.setProperty("--term-font", fontSel.value);
      await api("POST", "/api/settings/save", s);
      toast("Police : " + fontSel.value);
    };
  }
  // Wallpaper
  const wallSel = body.querySelector("#st-wallpaper");
  if (wallSel) {
    wallSel.onchange = async () => {
      s.wallpaper = wallSel.value;
      applyWallpaper(s.wallpaper);
      await api("POST", "/api/settings/save", s);
      toast("Wallpaper : " + wallSel.value);
    };
  }
  // Reset
  const resetBtn = body.querySelector("#st-reset");
  if (resetBtn) {
    resetBtn.onclick = async () => {
      if (!confirm("Effacer tous les fichiers du VFS ~/.kemus/home ?")) return;
      try {
        await api("POST", "/api/terminal", { cmd: "rm -rf Documents Projects Pictures Music Downloads hello.py README.txt test.txt workflow.txt", cwd: "/" });
        toast("VFS reinitialise");
      } catch (e) { toast(e.message); }
    };
  }
  // Export
  const exportBtn = body.querySelector("#st-export");
  if (exportBtn) {
    exportBtn.onclick = () => {
      const blob = new Blob([JSON.stringify(s, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = "kemus-settings.json"; a.click();
      toast("Settings exportes");
    };
  }
}

// ====== Theme system ======
function applyTheme(s) {
  const root = document.documentElement;
  const map = {
    "breeze-dark":   { bg:"#0b0f19", panel:"rgba(20,26,40,0.92)", panel_solid:"#141a28", accent:"#3daee9", text:"#e6e9ef", win_bg:"#1c2230", term_bg:"#0a0d14" },
    "breeze-light":  { bg:"#eff0f1", panel:"rgba(252,252,252,0.92)", panel_solid:"#fcfcfc", accent:"#1d99f3", text:"#232627", win_bg:"#ffffff", term_bg:"#ffffff" },
    "nixos":         { bg:"#1a2138", panel:"rgba(28,36,52,0.92)", panel_solid:"#1c2434", accent:"#5277c3", text:"#e6e9ef", win_bg:"#21293c", term_bg:"#0d121d" },
    "fedora":        { bg:"#101725", panel:"rgba(20,30,48,0.92)", panel_solid:"#141e30", accent:"#294172", text:"#e6e9ef", win_bg:"#1c2638", term_bg:"#0a0d14" },
    "dracula":       { bg:"#282a36", panel:"rgba(40,42,54,0.92)", panel_solid:"#282a36", accent:"#bd93f9", text:"#f8f8f2", win_bg:"#343746", term_bg:"#21222c" },
    "solarized-dark":{ bg:"#002b36", panel:"rgba(7,54,66,0.92)", panel_solid:"#073642", accent:"#2aa198", text:"#eee8d5", win_bg:"#073642", term_bg:"#002b36" },
    "tokyo-night":   { bg:"#1a1b26", panel:"rgba(26,27,38,0.92)", panel_solid:"#1a1b26", accent:"#7aa2f7", text:"#c0caf5", win_bg:"#24283b", term_bg:"#16161e" },
    "nord":          { bg:"#2e3440", panel:"rgba(46,52,64,0.92)", panel_solid:"#2e3440", accent:"#88c0d0", text:"#e5e9f0", win_bg:"#3b4252", term_bg:"#2e3440" },
  };
  const t = map[s.theme] || map["breeze-dark"];
  root.style.setProperty("--bg", t.bg);
  root.style.setProperty("--panel", t.panel);
  root.style.setProperty("--panel-solid", t.panel_solid);
  root.style.setProperty("--accent", s.accent || t.accent);
  root.style.setProperty("--text", t.text);
  root.style.setProperty("--win-bg", t.win_bg);
  root.style.setProperty("--term-bg", t.term_bg);
  root.setAttribute("data-theme", s.theme);
}

function applyWallpaper(name) {
  const desk = document.getElementById("desktop");
  if (!desk) return;
  const base = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
  let css = "";
  switch (name) {
    case "fedora": css = `radial-gradient(ellipse at 70% 30%, rgba(41,65,114,0.6), transparent 60%), linear-gradient(135deg, #101725, #1c2638)`; break;
    case "plasma": css = `radial-gradient(ellipse at 50% 100%, rgba(61,174,233,0.4), transparent 60%), linear-gradient(180deg, #0b0f19, #1a2138)`; break;
    case "solid": css = base; break;
    case "gradient": css = `linear-gradient(135deg, #0b0f19 0%, #11182b 50%, #1a2138 100%)`; break;
    case "nixos":
    default: css = `radial-gradient(ellipse at 30% 20%, rgba(82,119,195,0.35), transparent 60%), radial-gradient(ellipse at 75% 80%, rgba(41,65,114,0.45), transparent 55%), linear-gradient(135deg, #0b0f19 0%, #11182b 50%, #1a2138 100%)`;
  }
  desk.style.background = css;
}

// ====== Help ======
function openHelp() {
  const { id, body } = createWindow({ title: "Aide Kemus v2.0", width: 520, height: 420 });
  body.innerHTML = `
    <div class="help" style="overflow-y:auto;padding:14px">
      <h2 style="color:var(--accent)">Bienvenue dans Kemus 2.0</h2>
      <p>PC virtuel leger, inspire KDE Plasma + NixOS + Fedora.</p>
      <h3 style="margin-top:12px;color:var(--accent)">Nouveautes v2</h3>
      <ul>
        <li><b>55+ commandes</b> terminal reelles (pipes, redirections, env)</li>
        <li><b>8 themes</b> : Breeze Dark/Light, NixOS, Fedora, Dracula, Solarized, Tokyo Night, Nord</li>
        <li><b>Logitheque</b> : 40+ apps installables</li>
        <li><b>Browser reel</b> via proxy DuckDuckGo</li>
        <li><b>Workflow Studio</b> : runner YAML temps reel (SSE)</li>
        <li><b>man pages</b> Linux (offline + man7.org)</li>
        <li><b>curl/wget</b> reels (SSRF-safe)</li>
      </ul>
      <h3 style="margin-top:12px;color:var(--accent)">Raccourcis Terminal</h3>
      <ul>
        <li><code>help</code> — liste complete</li>
        <li><code>man CMD</code> — page de manuel</li>
        <li><code>ls | grep py | wc -l</code> — pipelines</li>
        <li><code>echo x > file.txt</code> — redirection</li>
        <li><code>export NAME=kemus && echo $NAME</code> — env vars</li>
        <li><code>curl https://example.com</code> — reel</li>
        <li><code>Ctrl+L</code> clear, <code>Ctrl+C</code> interrupt</li>
      </ul>
      <h3 style="margin-top:12px;color:var(--accent)">Workflows</h3>
      <p>Editez <code>~/.kemus/workflows/*.yml</code> ou via Workflow Studio.<br>
         Format compatible GitHub Actions (subset).</p>
    </div>`;
}

// ====== Browser reel (DuckDuckGo + proxy) ======
function openBrowser() {
  const { id, body } = createWindow({ title: "KemusBrowser - DuckDuckGo", width: 760, height: 500 });
  body.innerHTML = `
    <div class="browser" style="display:flex;flex-direction:column;height:100%">
      <div class="browser-toolbar" style="display:flex;gap:4px;padding:6px;background:var(--panel-solid);border-bottom:1px solid var(--win-border)">
        <button id="br-back" style="background:rgba(255,255,255,0.05);border:1px solid var(--win-border);color:var(--text);padding:4px 8px;border-radius:4px;cursor:pointer">←</button>
        <button id="br-home" style="background:rgba(255,255,255,0.05);border:1px solid var(--win-border);color:var(--text);padding:4px 8px;border-radius:4px;cursor:pointer">⌂</button>
        <input id="br-url" placeholder="Recherche DuckDuckGo ou URL (https://...)" style="flex:1;background:#0d121d;border:1px solid var(--win-border);color:var(--text);padding:4px 8px;border-radius:4px;font-size:12px">
        <button id="br-go" style="background:var(--accent);border:none;color:#fff;padding:4px 12px;border-radius:4px;cursor:pointer">Go</button>
      </div>
      <div id="br-content" style="flex:1;overflow:auto;background:#fff;color:#222;font-family:sans-serif;padding:14px"></div>
    </div>`;
  const content = body.querySelector("#br-content");
  const urlInput = body.querySelector("#br-url");
  let history = [];
  let homePage = true;

  const renderHome = () => {
    homePage = true;
    content.innerHTML = `
      <div style="text-align:center;padding:30px 20px;background:#fff;color:#222">
        <div style="font-size:42px;color:#de5833;margin-bottom:8px">🦆</div>
        <h1 style="font-size:24px;margin-bottom:14px;color:#222">KemusBrowser</h1>
        <p style="color:#666;margin-bottom:20px">Recherche via DuckDuckGo (proxy Kemus)</p>
        <div style="max-width:500px;margin:0 auto">
          <input id="br-home-search" placeholder="Rechercher sur le web..." style="width:100%;padding:10px;border:2px solid #de5833;border-radius:24px;font-size:14px;outline:none;color:#222">
        </div>
        <div style="margin-top:20px;color:#888;font-size:11px">Astuce : tapez une URL complete (https://...) pour naviguer directement</div>
      </div>`;
    const si = content.querySelector("#br-home-search");
    si.focus();
    si.addEventListener("keydown", e => { if (e.key === "Enter") doSearch(si.value); });
  };

  const doSearch = async (q) => {
    if (!q.trim()) return;
    homePage = false;
    urlInput.value = q;
    history.push(q);
    content.innerHTML = `<div style="padding:14px;background:#fff;color:#222">Recherche en cours... 🦆</div>`;
    try {
      const r = await api("GET", "/api/search?q=" + encodeURIComponent(q));
      if (!r.results || r.results.length === 0) {
        content.innerHTML = `<div style="padding:14px;background:#fff;color:#222"><h3>Aucun resultat</h3><p>Aucun resultat pour : <code>${q}</code></p></div>`;
        return;
      }
      if (r.results[0].error) {
        content.innerHTML = `<div style="padding:14px;background:#fff;color:#222"><h3>Erreur</h3><p>${r.results[0].error}</p><p style="color:#888;font-size:11px">Le reseau est peut-etre inaccessible depuis le serveur Kemus.</p></div>`;
        return;
      }
      content.innerHTML = `<div style="padding:14px;background:#fff;color:#222;max-width:700px;margin:0 auto">
        <h3 style="margin-bottom:14px">${r.results.length} resultats pour "${q}"</h3>
        ${r.results.map(res => `
          <div style="margin-bottom:14px">
            <a href="#" data-url="${encodeURIComponent(res.url)}" style="color:#1a0dab;text-decoration:none;font-size:16px">${escapeHtml(res.title)}</a>
            <div style="color:#006621;font-size:12px;margin:2px 0">${escapeHtml(res.url)}</div>
            <div style="color:#444;font-size:13px">${escapeHtml(res.snippet)}</div>
          </div>`).join("")}
      </div>`;
      content.querySelectorAll("a[data-url]").forEach(a => {
        a.onclick = (e) => { e.preventDefault(); navigateTo(decodeURIComponent(a.dataset.url)); };
      });
    } catch (e) {
      content.innerHTML = `<div style="padding:14px;background:#fff;color:#222">Erreur : ${escapeHtml(e.message)}</div>`;
    }
  };

  const navigateTo = (url) => {
    homePage = false;
    urlInput.value = url;
    history.push(url);
    content.innerHTML = `<div style="padding:14px;background:#fff;color:#222">Chargement de ${escapeHtml(url)}...</div>`;
    // Load through our proxy in an iframe inside content
    content.innerHTML = `<iframe src="/api/proxy?url=${encodeURIComponent(url)}" style="width:100%;height:100%;border:none;background:#fff"></iframe>`;
  };

  const go = () => {
    const v = urlInput.value.trim();
    if (!v) return;
    if (v.startsWith("http://") || v.startsWith("https://")) navigateTo(v);
    else doSearch(v);
  };

  body.querySelector("#br-go").onclick = go;
  urlInput.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
  body.querySelector("#br-back").onclick = () => {
    if (history.length > 1) { history.pop(); const prev = history[history.length - 1]; urlInput.value = prev; go(); }
    else renderHome();
  };
  body.querySelector("#br-home").onclick = renderHome;
  renderHome();
}

function escapeHtml(s) {
  return String(s||"").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ====== v2 : Nouvelles apps ======

// Logitheque (App Store)
async function openStore() {
  const { id, body } = createWindow({ title: "Logitheque - Kemus Store", width: 720, height: 500 });
  let catalog = [], installed = [];
  try {
    const r = await api("GET", "/api/installed");
    catalog = r.catalog; installed = r.installed;
  } catch (e) { body.innerHTML = "Erreur : " + e.message; return; }
  const cats = [...new Set(catalog.map(a => a.category))];
  body.innerHTML = `
    <div style="display:flex;height:100%">
      <div style="width:160px;background:var(--panel-solid);padding:8px;border-right:1px solid var(--win-border);overflow-y:auto">
        <h4 style="font-size:11px;color:var(--text-dim);margin:8px 0 4px;text-transform:uppercase">Categories</h4>
        ${cats.map(c => `<div class="store-cat" data-cat="${c}" style="padding:5px 8px;border-radius:4px;cursor:pointer;font-size:12px;color:var(--text)">${c}</div>`).join("")}
      </div>
      <div style="flex:1;padding:14px;overflow-y:auto">
        <input id="store-search" placeholder="Rechercher une app..." style="width:100%;background:#0d121d;border:1px solid var(--win-border);color:var(--text);padding:6px 10px;border-radius:4px;margin-bottom:10px;font-size:12px">
        <div id="store-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px"></div>
      </div>
    </div>`;
  const grid = body.querySelector("#store-grid");
  const search = body.querySelector("#store-search");
  let activeCat = null;

  const render = () => {
    const q = search.value.toLowerCase();
    let items = catalog;
    if (activeCat) items = items.filter(a => a.category === activeCat);
    if (q) items = items.filter(a => a.name.toLowerCase().includes(q) || a.desc.toLowerCase().includes(q));
    grid.innerHTML = items.map(a => {
      const isInst = installed.includes(a.id);
      return `<div style="background:var(--win-bg);border:1px solid var(--win-border);border-radius:6px;padding:10px;display:flex;flex-direction:column;gap:6px">
        <div style="display:flex;align-items:center;gap:6px">
          <div style="width:32px;height:32px;background:var(--accent);border-radius:6px;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:bold;font-size:14px">${a.name.charAt(0)}</div>
          <div>
            <div style="font-size:13px;color:var(--text)">${a.name}</div>
            <div style="font-size:10px;color:var(--text-dim)">${a.category}</div>
          </div>
        </div>
        <div style="font-size:11px;color:var(--text-dim);min-height:28px">${a.desc}</div>
        <button data-id="${a.id}" data-action="${isInst?'uninstall':'install'}" style="background:${isInst?'#e74c3c':'var(--accent)'};border:none;color:#fff;padding:4px;border-radius:4px;cursor:pointer;font-size:11px">${isInst?'Desinstaller':'Installer'}</button>
      </div>`;
    }).join("");
    grid.querySelectorAll("button").forEach(btn => {
      btn.onclick = async () => {
        const aid = btn.dataset.id;
        const action = btn.dataset.action;
        try {
          await api("POST", "/api/store/" + action, { id: aid });
          if (action === "install") { installed.push(aid); toast("App installee"); }
          else { installed = installed.filter(x => x !== aid); toast("App desinstallee"); }
          rebuildDockAndKickoff();
          render();
        } catch (e) { toast(e.message); }
      };
    });
  };

  body.querySelectorAll(".store-cat").forEach(el => {
    el.onclick = () => {
      activeCat = (activeCat === el.dataset.cat) ? null : el.dataset.cat;
      body.querySelectorAll(".store-cat").forEach(x => x.style.background = "transparent");
      if (activeCat) el.style.background = "var(--accent)";
      render();
    };
  });
  search.oninput = render;
  render();
}

// Workflow Studio (SSE live)
async function openWorkflow() {
  const { id, body } = createWindow({ title: "Workflow Studio", width: 760, height: 520 });
  let wfs = [];
  try { const r = await api("GET", "/api/workflows"); wfs = r.workflows; } catch (e) {}
  body.innerHTML = `
    <div style="display:flex;height:100%">
      <div style="width:240px;background:var(--panel-solid);padding:8px;border-right:1px solid var(--win-border);overflow-y:auto">
        <h4 style="font-size:11px;color:var(--text-dim);margin:8px 0 4px;text-transform:uppercase">Workflows (~/.kemus/workflows/)</h4>
        <div id="wf-list"></div>
        <button id="wf-new" style="margin-top:8px;background:var(--accent);border:none;color:#fff;padding:6px;border-radius:4px;cursor:pointer;width:100%;font-size:11px">+ Nouveau workflow</button>
      </div>
      <div style="flex:1;display:flex;flex-direction:column">
        <div id="wf-toolbar" style="padding:6px;background:var(--panel-solid);border-bottom:1px solid var(--win-border);display:flex;gap:6px;align-items:center">
          <button id="wf-run" style="background:#27ae60;border:none;color:#fff;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:12px" disabled>▶ Run</button>
          <button id="wf-save" style="background:var(--accent);border:none;color:#fff;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:12px">Enregistrer</button>
          <span id="wf-name" style="flex:1;color:var(--text-dim);font-size:12px;padding:0 8px">Selectionnez un workflow</span>
        </div>
        <textarea id="wf-editor" placeholder="name: Mon workflow\non: manual\njobs:\n  build:\n    steps:\n      - name: Etape 1\n        run: echo Hello" style="flex:1;background:#0a0d14;color:var(--text);border:none;outline:none;font-family:monospace;font-size:12px;padding:10px;resize:none"></textarea>
        <div id="wf-output" style="height:200px;background:#000;color:#0f0;font-family:monospace;font-size:11px;padding:8px;overflow-y:auto;border-top:2px solid var(--accent);white-space:pre-wrap"></div>
      </div>
    </div>`;
  const list = body.querySelector("#wf-list");
  const editor = body.querySelector("#wf-editor");
  const output = body.querySelector("#wf-output");
  const runBtn = body.querySelector("#wf-run");
  const saveBtn = body.querySelector("#wf-save");
  const nameSpan = body.querySelector("#wf-name");
  let curFile = null;

  const renderList = () => {
    list.innerHTML = wfs.map(w => `<div class="wf-item" data-file="${w.file}" style="padding:6px 8px;border-radius:4px;cursor:pointer;font-size:12px;color:var(--text);margin-bottom:2px">
      <div style="font-weight:bold">${w.name||w.file}</div>
      <div style="font-size:10px;color:var(--text-dim)">${w.step_count} etapes · on: ${w.on||'manual'}</div>
    </div>`).join("");
    list.querySelectorAll(".wf-item").forEach(el => {
      el.onclick = () => {
        list.querySelectorAll(".wf-item").forEach(x => x.style.background = "transparent");
        el.style.background = "var(--accent)";
        const w = wfs.find(x => x.file === el.dataset.file);
        curFile = w.file;
        editor.value = w.raw || "";
        nameSpan.textContent = w.name + " (" + w.file + ")";
        runBtn.disabled = false;
      };
    });
  };
  renderList();

  body.querySelector("#wf-new").onclick = () => {
    const name = prompt("Nom du fichier (.yml) :", "mon-workflow.yml");
    if (!name) return;
    curFile = name.endsWith(".yml") ? name : name + ".yml";
    editor.value = "name: Mon workflow\non: manual\njobs:\n  build:\n    steps:\n      - name: Hello\n      - run: echo Bonjour Kemus\n";
    nameSpan.textContent = curFile;
    runBtn.disabled = false;
  };

  saveBtn.onclick = async () => {
    if (!curFile) { toast("Aucun workflow selectionne"); return; }
    try {
      await api("POST", "/api/workflow/save", { file: curFile, content: editor.value });
      toast("Workflow enregistre : " + curFile);
      const r = await api("GET", "/api/workflows"); wfs = r.workflows; renderList();
    } catch (e) { toast(e.message); }
  };

  runBtn.onclick = () => {
    if (!curFile) return;
    output.innerHTML = "";
    const es = new EventSource("/api/workflow/run?name=" + encodeURIComponent(curFile));
    const append = (cls, text) => {
      const div = document.createElement("div");
      div.className = cls;
      div.textContent = text;
      output.appendChild(div);
      output.scrollTop = output.scrollHeight;
    };
    es.addEventListener("start", e => append("wf-start", "▶ " + JSON.parse(e.data).name));
    es.addEventListener("job", e => { const d = JSON.parse(e.data); append("wf-job", "\n[JOB] " + d.name + " (" + d.steps + " etapes)"); });
    es.addEventListener("step-start", e => { const d = JSON.parse(e.data); append("wf-step", "\n→ Etape " + d.index + " : " + d.name + "  [" + d.run + "]"); });
    es.addEventListener("output", e => { const d = JSON.parse(e.data); append("wf-out", d.text); });
    es.addEventListener("step-done", e => { const d = JSON.parse(e.data); append("wf-done", "  ✓ etape " + d.index + " terminee"); });
    es.addEventListener("job-done", e => append("wf-jobdone", "  ✓ job termine"));
    es.addEventListener("done", e => { const d = JSON.parse(e.data); append("wf-end", "\n═══════════════════════════════════\n✅ Workflow termine : " + d.name); es.close(); });
    es.addEventListener("error", e => { append("wf-err", "❌ " + (e.data || "erreur")); es.close(); });
    es.onerror = () => { append("wf-err", "Connexion perdue"); es.close(); };
  };
}

// Process Monitor
async function openMonitor() {
  const { id, body } = createWindow({ title: "Process Monitor", width: 480, height: 380 });
  const update = async () => {
    try {
      const r = await api("POST", "/api/terminal", { cmd: "top", cwd: "/" });
      body.innerHTML = `<pre style="font-family:monospace;font-size:11px;color:var(--text);padding:10px;background:var(--term-bg);height:100%;overflow:auto;white-space:pre-wrap;margin:0">${escapeHtml(r.output)}</pre>`;
    } catch (e) {}
  };
  update();
  const iv = setInterval(update, 3000);
  // cleanup on close
  const observer = new MutationObserver(() => {
    if (!document.body.contains(body)) { clearInterval(iv); observer.disconnect(); }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

// Logs viewer
async function openLogs() {
  const { id, body } = createWindow({ title: "Logs Kemus (live tail)", width: 700, height: 400 });
  body.innerHTML = `<pre id="log-pre" style="font-family:monospace;font-size:11px;color:#27ae60;padding:10px;background:#000;height:100%;overflow:auto;white-space:pre-wrap;margin:0"></pre>`;
  const pre = body.querySelector("#log-pre");
  const update = async () => {
    try {
      const r = await api("POST", "/api/terminal", { cmd: "cat ~/.kemus/kemus.log | tail -50", cwd: "/" });
      pre.textContent = r.output || "(log vide)";
      pre.scrollTop = pre.scrollHeight;
    } catch (e) {}
  };
  update();
  const iv = setInterval(update, 2000);
  const observer = new MutationObserver(() => {
    if (!document.body.contains(body)) { clearInterval(iv); observer.disconnect(); }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

// Man pages
async function openMan() {
  const { id, body } = createWindow({ title: "Man Pages Linux", width: 620, height: 460 });
  body.innerHTML = `
    <div style="display:flex;flex-direction:column;height:100%">
      <div style="padding:6px;background:var(--panel-solid);border-bottom:1px solid var(--win-border);display:flex;gap:6px">
        <input id="man-input" placeholder="Commande (ls, grep, curl, ...)" style="flex:1;background:#0d121d;border:1px solid var(--win-border);color:var(--text);padding:4px 8px;border-radius:4px;font-family:monospace">
        <button id="man-go" style="background:var(--accent);border:none;color:#fff;padding:4px 12px;border-radius:4px;cursor:pointer">Afficher</button>
      </div>
      <pre id="man-out" style="flex:1;font-family:monospace;font-size:12px;color:var(--text);padding:14px;background:var(--term-bg);overflow:auto;white-space:pre-wrap;margin:0"></pre>
    </div>`;
  const input = body.querySelector("#man-input");
  const out = body.querySelector("#man-out");
  const go = async () => {
    const cmd = input.value.trim();
    if (!cmd) return;
    out.textContent = "Chargement...";
    try {
      const r = await api("GET", "/api/man?cmd=" + encodeURIComponent(cmd));
      out.textContent = r.text || "(aucune page)";
    } catch (e) { out.textContent = "Erreur : " + e.message; }
  };
  body.querySelector("#man-go").onclick = go;
  input.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
  input.value = "ls"; go();
}

// Coming soon (placeholder for many apps)
function openComingSoon() {
  const { id, body } = createWindow({ title: "Bientot disponible", width: 360, height: 200 });
  body.innerHTML = `<div style="padding:30px;text-align:center;color:var(--text-dim)">
    <div style="font-size:32px;margin-bottom:10px">🚧</div>
    <p>Cette application est en developpement.</p>
    <p style="font-size:11px;margin-top:8px">Kemus v2.0 - roadmap</p>
  </div>`;
}

// Markdown preview
async function openMarkdown() {
  const { id, body } = createWindow({ title: "Markdown Preview", width: 700, height: 460 });
  body.innerHTML = `
    <div style="display:flex;height:100%">
      <textarea id="md-input" style="flex:1;background:#0a0d14;color:var(--text);border:none;outline:none;font-family:monospace;font-size:12px;padding:10px;resize:none" spellcheck="false"># Titre\n\nBonjour **Kemus** !\n\n- Element 1\n- Element 2\n\n\`\`\`python\nprint("hello")\n\`\`\`</textarea>
      <div id="md-preview" style="flex:1;padding:14px;background:#fff;color:#222;overflow:auto;font-family:sans-serif"></div>
    </div>`;
  const input = body.querySelector("#md-input");
  const prev = body.querySelector("#md-preview");
  const render = () => {
    let md = input.value;
    md = md.replace(/^# (.+)$/gm, '<h1>$1</h1>');
    md = md.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    md = md.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    md = md.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    md = md.replace(/`(.+?)`/g, '<code>$1</code>');
    md = md.replace(/^- (.+)$/gm, '<li>$1</li>');
    md = md.replace(/(<li>.*<\/li>\n?)+/g, m => '<ul>' + m + '</ul>');
    md = md.replace(/\n\n/g, '<br><br>');
    prev.innerHTML = md;
  };
  input.oninput = render; render();
}

// Notes
function openNotes() {
  const { id, body } = createWindow({ title: "Notes rapides", width: 400, height: 360 });
  body.innerHTML = `<textarea id="notes-area" style="width:100%;height:100%;background:#fffacd;color:#222;border:none;outline:none;padding:14px;font-family:cursive,serif;font-size:14px;resize:none" placeholder="Vos notes..."></textarea>`;
  const ta = body.querySelector("#notes-area");
  try { ta.value = localStorage.getItem("kemus-notes") || ""; } catch (e) {}
  ta.oninput = () => { try { localStorage.setItem("kemus-notes", ta.value); } catch (e) {} };
}

// Tasks
function openTasks() {
  const { id, body } = createWindow({ title: "Liste de taches", width: 360, height: 420 });
  body.innerHTML = `
    <div style="padding:14px;height:100%;display:flex;flex-direction:column;gap:8px;background:var(--win-bg)">
      <div style="display:flex;gap:6px">
        <input id="task-input" placeholder="Nouvelle tache..." style="flex:1;background:#0d121d;border:1px solid var(--win-border);color:var(--text);padding:6px 8px;border-radius:4px;font-size:12px">
        <button id="task-add" style="background:var(--accent);border:none;color:#fff;padding:4px 12px;border-radius:4px;cursor:pointer">+</button>
      </div>
      <ul id="task-list" style="flex:1;overflow-y:auto;list-style:none;padding:0;margin:0"></ul>
    </div>`;
  let tasks = [];
  try { tasks = JSON.parse(localStorage.getItem("kemus-tasks") || "[]"); } catch (e) {}
  const list = body.querySelector("#task-list");
  const render = () => {
    list.innerHTML = tasks.map((t, i) => `<li style="padding:6px;display:flex;gap:6px;align-items:center;font-size:13px;color:var(--text)">
      <input type="checkbox" data-i="${i}" ${t.done?'checked':''} style="cursor:pointer">
      <span style="flex:1;${t.done?'text-decoration:line-through;color:var(--text-dim)':''}">${escapeHtml(t.text)}</span>
      <button data-del="${i}" style="background:transparent;border:none;color:#e74c3c;cursor:pointer">✕</button>
    </li>`).join("");
    list.querySelectorAll("input[type=checkbox]").forEach(cb => cb.onclick = () => { tasks[cb.dataset.i].done = cb.checked; save(); render(); });
    list.querySelectorAll("button[data-del]").forEach(b => b.onclick = () => { tasks.splice(b.dataset.del, 1); save(); render(); });
  };
  const save = () => { try { localStorage.setItem("kemus-tasks", JSON.stringify(tasks)); } catch (e) {} };
  const add = () => {
    const inp = body.querySelector("#task-input");
    if (inp.value.trim()) { tasks.push({ text: inp.value.trim(), done: false }); inp.value = ""; save(); render(); }
  };
  body.querySelector("#task-add").onclick = add;
  body.querySelector("#task-input").addEventListener("keydown", e => { if (e.key === "Enter") add(); });
  render();
}

// Calendar
function openCalendar() {
  const { id, body } = createWindow({ title: "Calendrier", width: 320, height: 320 });
  const now = new Date();
  const y = now.getFullYear(), m = now.getMonth();
  const first = new Date(y, m, 1).getDay();
  const days = new Date(y, m + 1, 0).getDate();
  const mois = ["Janvier","Fevrier","Mars","Avril","Mai","Juin","Juillet","Aout","Septembre","Octobre","Novembre","Decembre"];
  let cells = "";
  for (let i = 0; i < first; i++) cells += `<td></td>`;
  for (let d = 1; d <= days; d++) {
    const today = (d === now.getDate()) ? `style="background:var(--accent);color:#fff;border-radius:50%"` : "";
    cells += `<td ${today} style="text-align:center;padding:6px;color:var(--text)">${d}</td>`;
    if ((first + d) % 7 === 0) cells += "</tr><tr>";
  }
  body.innerHTML = `<div style="padding:14px;background:var(--win-bg);height:100%">
    <h3 style="color:var(--text);text-align:center;margin-bottom:10px">${mois[m]} ${y}</h3>
    <table style="width:100%;font-size:12px;color:var(--text-dim)">
      <tr><th>L</th><th>M</th><th>M</th><th>J</th><th>V</th><th>S</th><th>D</th></tr>
      <tr>${cells}</tr>
    </table>`;
}

// Image viewer (SVG placeholder)
function openImageViewer() {
  const { id, body } = createWindow({ title: "Image Viewer", width: 480, height: 380 });
  body.innerHTML = `<div style="background:#000;height:100%;display:flex;align-items:center;justify-content:center">
    <svg viewBox="0 0 200 200" style="width:200px;height:200px"><g fill="none" stroke="#5277c3" stroke-width="2"><path d="M100 20 L130 70 L100 100 L70 70 Z"/><path d="M100 100 L130 130 L100 180 L70 130 Z"/><path d="M100 100 L160 100 L100 130 L40 100 Z"/><path d="M100 100 L160 100 L100 70 L40 100 Z"/></g></svg>
  </div>`;
}

// Stub apps that just open terminal with command
function openTerminalWith(cmd) { return openTerminalWithCmd(cmd); }
function openTerminalWithCmd(cmd) {
  const { id, body } = createWindow({ title: "Terminal — kemus@nixos", width: 640, height: 400 });
  body.innerHTML = `<div class="term" id="term-${id}"></div>`;
  initTerminal(id, cmd);
}

// Color picker
function openColorPicker() {
  const { id, body } = createWindow({ title: "Color Picker", width: 280, height: 200 });
  body.innerHTML = `<div style="padding:14px;background:var(--win-bg);height:100%;display:flex;flex-direction:column;gap:10px;align-items:center">
    <input type="color" id="cp-input" value="#3daee9" style="width:80px;height:80px;border:none;cursor:pointer">
    <div id="cp-hex" style="font-family:monospace;font-size:14px;color:var(--text)">HEX: #3daee9</div>
    <div id="cp-rgb" style="font-family:monospace;font-size:12px;color:var(--text-dim)">RGB: 61, 174, 233</div>
  </div>`;
  const inp = body.querySelector("#cp-input");
  const hex = body.querySelector("#cp-hex");
  const rgb = body.querySelector("#cp-rgb");
  inp.oninput = () => {
    const v = inp.value;
    hex.textContent = "HEX: " + v.toUpperCase();
    const r = parseInt(v.slice(1,3),16), g = parseInt(v.slice(3,5),16), b = parseInt(v.slice(5,7),16);
    rgb.textContent = `RGB: ${r}, ${g}, ${b}`;
  };
}

// Dictionary via DuckDuckGo
async function openDictionary() {
  const { id, body } = createWindow({ title: "Dictionnaire (DuckDuckGo)", width: 480, height: 380 });
  body.innerHTML = `
    <div style="display:flex;flex-direction:column;height:100%">
      <div style="padding:6px;background:var(--panel-solid);border-bottom:1px solid var(--win-border);display:flex;gap:6px">
        <input id="dict-input" placeholder="Mot a definir..." style="flex:1;background:#0d121d;border:1px solid var(--win-border);color:var(--text);padding:4px 8px;border-radius:4px">
        <button id="dict-go" style="background:var(--accent);border:none;color:#fff;padding:4px 12px;border-radius:4px;cursor:pointer">Definir</button>
      </div>
      <div id="dict-out" style="flex:1;padding:14px;overflow:auto;background:var(--win-bg);color:var(--text);font-size:13px">Tapez un mot ci-dessus.</div>
    </div>`;
  const inp = body.querySelector("#dict-input");
  const out = body.querySelector("#dict-out");
  const go = async () => {
    const w = inp.value.trim();
    if (!w) return;
    out.innerHTML = "Recherche via DuckDuckGo...";
    try {
      const r = await api("GET", "/api/search?q=" + encodeURIComponent("define " + w));
      if (r.results && r.results[0]) {
        out.innerHTML = `<h3 style="color:var(--accent)">${escapeHtml(w)}</h3><p>${escapeHtml(r.results[0].snippet || r.results[0].title)}</p><p style="margin-top:10px;font-size:11px;color:var(--text-dim)">Source: <a href="#" data-url="${encodeURIComponent(r.results[0].url)}" style="color:var(--accent)">${escapeHtml(r.results[0].url)}</a></p>`;
      } else {
        out.innerHTML = "Aucune definition trouvee.";
      }
    } catch (e) { out.innerHTML = "Erreur : " + e.message; }
  };
  body.querySelector("#dict-go").onclick = go;
  inp.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
}

// Generic stub for the rest
function openGit() { openTerminalWithCmd("echo 'Git GUI (simule) - utilisez le terminal pour git log/status'"); }
function openRegex() { openComingSoon(); }
function openWeather() { openTerminalWithCmd("echo 'Meteo KemusOS : 18°C, ensoleille ☀️ (simule)'"); }
function openNews() { openComingSoon(); }
function openMail() { openComingSoon(); }
function openDisks() { openTerminalWithCmd("df -h"); }
function openPackages() { openTerminalWithCmd("apt list"); }
function openFirewall() { openComingSoon(); }
function openNetwork() { openTerminalWithCmd("ip a 2>/dev/null || ifconfig 2>/dev/null || echo 'Reseau Kemus: 127.0.0.1/8, port 3922'"); }
function openSensors() { openTerminalWithCmd("echo 'Capteurs: CPU 42°C, Fan 1200 RPM (simule)'"); }
function openBattery() { openTerminalWithCmd("echo 'Batterie: 95%, 4h20 restant (simule)'"); }
function openUpdates() { openTerminalWithCmd("apt update"); }
function openBackup() { openTerminalWithCmd("tar czf backup.tar.gz . && echo 'Backup cree: backup.tar.gz'"); }
function openDiagnostics() { openTerminalWithCmd("neofetch && df -h && free -m && uptime"); }
function openClock() {
  const { id, body } = createWindow({ title: "Horloge", width: 240, height: 280 });
  body.innerHTML = `<div style="background:var(--win-bg);height:100%;display:flex;align-items:center;justify-content:center">
    <svg viewBox="0 0 100 100" style="width:200px;height:200px">
      <circle cx="50" cy="50" r="48" fill="none" stroke="var(--accent)" stroke-width="2"/>
      <line id="cl-h" x1="50" y1="50" x2="50" y2="30" stroke="var(--text)" stroke-width="3" stroke-linecap="round"/>
      <line id="cl-m" x1="50" y1="50" x2="50" y2="20" stroke="var(--text)" stroke-width="2" stroke-linecap="round"/>
      <line id="cl-s" x1="50" y1="50" x2="50" y2="15" stroke="var(--accent)" stroke-width="1" stroke-linecap="round"/>
    </svg>
  </div>`;
  const h = body.querySelector("#cl-h"), m = body.querySelector("#cl-m"), s = body.querySelector("#cl-s");
  const upd = () => {
    const d = new Date();
    s.setAttribute("transform", `rotate(${d.getSeconds()*6} 50 50)`);
    m.setAttribute("transform", `rotate(${d.getMinutes()*6} 50 50)`);
    h.setAttribute("transform", `rotate(${(d.getHours()%12)*30 + d.getMinutes()/2} 50 50)`);
  };
  upd();
  const iv = setInterval(upd, 1000);
  const obs = new MutationObserver(() => { if (!document.body.contains(body)) { clearInterval(iv); obs.disconnect(); } });
  obs.observe(document.body, { childList: true, subtree: true });
}
function openScreenshot() { toast("Screenshot simule - non disponible en mode web"); }
function openTranslator() { openComingSoon(); }
function openMusic() { openComingSoon(); }
function openVideo() { openComingSoon(); }
function openPdf() { openComingSoon(); }
function openMines() { openComingSoon(); }
function openSnake() { openComingSoon(); }
function open2048() { openComingSoon(); }

// ====== Calculator ======
function openCalc() {
  const { id, body } = createWindow({ title: "Calculatrice", width: 240, height: 320 });
  body.innerHTML = `
    <div class="calc">
      <div class="calc-display" id="calc-disp">0</div>
      <div class="calc-grid">
        <button class="op" data-k="C">C</button><button class="op" data-k="(">(</button><button class="op" data-k=")">)</button><button class="op" data-k="/">/</button>
        <button data-k="7">7</button><button data-k="8">8</button><button data-k="9">9</button><button class="op" data-k="*">*</button>
        <button data-k="4">4</button><button data-k="5">5</button><button data-k="6">6</button><button class="op" data-k="-">-</button>
        <button data-k="1">1</button><button data-k="2">2</button><button data-k="3">3</button><button class="op" data-k="+">+</button>
        <button data-k="0">0</button><button data-k=".">.</button><button data-k="back">⌫</button><button class="eq" data-k="=">=</button>
      </div>
    </div>`;
  const disp = body.querySelector("#calc-disp");
  let expr = "";
  body.querySelectorAll(".calc-grid button").forEach(btn => {
    btn.onclick = () => {
      const k = btn.dataset.k;
      if (k === "C") { expr = ""; disp.textContent = "0"; }
      else if (k === "back") { expr = expr.slice(0, -1); disp.textContent = expr || "0"; }
      else if (k === "=") {
        try {
          // Safe-ish eval: only digits and operators
          if (!/^[0-9+\-*/.() ]+$/.test(expr)) throw 0;
          const r = Function('"use strict";return (' + expr + ')')();
          disp.textContent = String(r); expr = String(r);
        } catch (e) { disp.textContent = "Erreur"; expr = ""; }
      } else { expr += k; disp.textContent = expr; }
    };
  });
}

// ====== Kickoff menu (dynamique selon apps installees) ======
let installedApps = ["terminal", "files", "editor", "browser", "calc", "settings", "help", "neofetch", "workflow", "store", "man", "monitor", "logs"];

async function loadInstalledApps() {
  try {
    const r = await api("GET", "/api/installed");
    installedApps = r.installed || installedApps;
  } catch (e) {}
}

function buildKickoff() {
  const grid = document.getElementById("km-grid");
  grid.innerHTML = "";
  installedApps.forEach(key => {
    const app = APPS[key];
    if (!app) return;
    const div = document.createElement("div");
    div.className = "km-app";
    div.innerHTML = app.icon + '<span>' + app.name + '</span>';
    div.onclick = () => { app.open(); closeKickoff(); };
    grid.appendChild(div);
  });
  const search = document.getElementById("km-search-input");
  search.oninput = () => {
    const q = search.value.toLowerCase();
    grid.innerHTML = "";
    installedApps.forEach(key => {
      const app = APPS[key];
      if (!app) return;
      if (app.name.toLowerCase().includes(q)) {
        const div = document.createElement("div");
        div.className = "km-app";
        div.innerHTML = app.icon + '<span>' + app.name + '</span>';
        div.onclick = () => { app.open(); closeKickoff(); };
        grid.appendChild(div);
      }
    });
  };
}

function closeKickoff() {
  document.getElementById("kickoff-menu").classList.remove("open");
}

document.getElementById("kickoff").onclick = (e) => {
  e.stopPropagation();
  document.getElementById("kickoff-menu").classList.toggle("open");
  if (document.getElementById("kickoff-menu").classList.contains("open")) {
    document.getElementById("km-search-input").focus();
  }
};

document.addEventListener("click", (e) => {
  if (!e.target.closest("#kickoff-menu") && !e.target.closest("#kickoff")) {
    closeKickoff();
  }
});

// ====== Dock (dynamique selon apps installees) ======
function buildDock() {
  const dock = document.getElementById("dock");
  dock.innerHTML = "";
  // Afficher les 9 premieres apps installees dans le dock
  installedApps.slice(0, 9).forEach(key => {
    const app = APPS[key];
    if (!app) return;
    const div = document.createElement("div");
    div.className = "dock-icon";
    div.innerHTML = app.icon;
    div.title = app.name;
    div.onclick = () => app.open();
    dock.appendChild(div);
  });
}

function rebuildDockAndKickoff() {
  buildDock();
  buildKickoff();
}

// ====== Desktop icons ======
function buildDesktopIcons() {
  const wrap = document.getElementById("desktop-icons");
  wrap.innerHTML = "";
  const items = [
    { key: "terminal", label: "Terminal" },
    { key: "files", label: "Fichiers" },
    { key: "store", label: "Logitheque" },
    { key: "workflow", label: "Workflows" },
    { key: "help", label: "Aide" },
  ];
  items.forEach(it => {
    const div = document.createElement("div");
    div.className = "desk-icon";
    const app = APPS[it.key];
    if (!app) return;
    div.innerHTML = app.icon + '<span>' + it.label + '</span>';
    div.ondblclick = () => app.open();
    div.onclick = (e) => { if (e.detail === 2) app.open(); };
    wrap.appendChild(div);
  });
}

// ====== Clock ======
function tickClock() {
  const now = new Date();
  const d = now.toLocaleDateString("fr-FR", { weekday: "short", day: "2-digit", month: "short" });
  const t = now.toLocaleTimeString("fr-FR", { hour12: false });
  document.getElementById("clock").textContent = d + " " + t;
}
setInterval(tickClock, 1000); tickClock();

// ====== Boot sequence ======
async function boot() {
  const bar = document.getElementById("boot-bar");
  const txt = document.getElementById("boot-text");
  const steps = ["Initialisation du noyau Kemus 2.0...", "Chargement des modules Plasma + Nix...", "Montage du VFS ~/.kemus/home...", "Chargement des workflows...", "Demarrage du serveur port 3922...", "Application du theme...", "Connexion a la session...", "Pret."];
  for (let i = 0; i < steps.length; i++) {
    txt.textContent = steps[i];
    bar.style.width = ((i + 1) / steps.length * 100) + "%";
    await new Promise(r => setTimeout(r, 220));
  }
  await new Promise(r => setTimeout(r, 200));
  // Charger settings + apps installees
  try {
    const s = await api("GET", "/api/settings");
    applyTheme(s);
    applyWallpaper(s.wallpaper || "nixos");
    if (s.term_font_size) document.documentElement.style.setProperty("--term-font-size", s.term_font_size + "px");
  } catch (e) {}
  await loadInstalledApps();
  document.getElementById("boot").style.display = "none";
  buildKickoff();
  buildDock();
  buildDesktopIcons();
  // Auto-open terminal avec message de bienvenue v2
  setTimeout(() => {
    openTerminal();
    setTimeout(() => toast("Kemus 2.0 demarre - 55+ commandes, 8 themes, Logitheque, Workflow Studio"), 1000);
  }, 400);
}

boot();
</script>
</body>
</html>"""


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def find_free_port(preferred: int) -> int:
    """Try preferred port; if taken, walk up to 3932 etc."""
    for port in [preferred, preferred + 1, preferred + 2, preferred + 10, 0]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((HOST, port))
                return port if port != 0 else s.getsockname()[1]
        except OSError:
            continue
    return 0


def main():
    seed_vfs()
    _seed_workflows()
    port = find_free_port(PORT)
    if port != PORT:
        print(f"[Kemus] Port {PORT} occupe, utilisation de {port}", file=sys.stderr)
    server = ThreadingHTTPServer((HOST, port), KemusHandler)
    daemon = server.serve_forever
    url = f"http://{HOST}:{port}/"
    banner = f"""
============================================================
  Kemus {VERSION} - PC virtuel leger (KDE/NixOS/Fedora)
  VFS reel : {VFS_ROOT}
  Log      : {LOG_FILE}
  URL      : {url}
============================================================
  Ouvrez {url} dans Firefox / Chrome.
  Ctrl+C pour arreter.
============================================================
"""
    print(banner)
    try:
        daemon()
    except KeyboardInterrupt:
        print("\n[Kemus] Arret...")
        server.shutdown()


if __name__ == "__main__":
    main()
