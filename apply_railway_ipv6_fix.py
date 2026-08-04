#!/usr/bin/env python3
"""
apply_railway_ipv6_fix.py — one-shot repo patcher.

    python3 apply_railway_ipv6_fix.py            # patch in place
    python3 apply_railway_ipv6_fix.py --dry-run  # show what would change
    python3 apply_railway_ipv6_fix.py --revert   # restore from .bak files

WHAT THIS FIXES
---------------
ConnectTimeoutError between the bot service and the wallet service on Railway.

Railway's private network is IPv6-only on environments created before
2025-10-16, and dual-stack after. `<service>.railway.internal` publishes an
AAAA record. monero-wallet-rpc was started with `--rpc-bind-ip 0.0.0.0`, which
listens on IPv4 ONLY. So the bot resolves an IPv6 address, dials it, and
nothing is listening — which surfaces as a connect TIMEOUT rather than a
connection refused, because the packets are dropped rather than rejected.

The fix is to bind both stacks:

    --rpc-bind-ip 0.0.0.0            (IPv4, for local docker-compose)
    --rpc-use-ipv6                   (enable IPv6 listener)
    --rpc-bind-ipv6-address ::       (all IPv6 interfaces — what Railway needs)
    --confirm-external-bind          (required for any non-loopback bind)

This was my error in the Dockerfile I gave you, not a mistake in your setup.

Every hunk is idempotent: re-running is a no-op. Originals are saved as
`<file>.bak` before the first modification.
"""

from __future__ import annotations

import argparse
import difflib
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

MARKER = "RAILWAY_IPV6_PATCH"


# --------------------------------------------------------------------------- #
# Terminal helpers
# --------------------------------------------------------------------------- #

_USE_COLOR = sys.stdout.isatty() and os.getenv("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def ok(msg: str) -> None:
    print(f"  {_c('32', '[ok]')}      {msg}")


def skip(msg: str) -> None:
    print(f"  {_c('36', '[skip]')}    {msg}")


def warn(msg: str) -> None:
    print(f"  {_c('33', '[warn]')}    {msg}")


def fail(msg: str) -> None:
    print(f"  {_c('31', '[FAIL]')}    {msg}")


def header(msg: str) -> None:
    print(f"\n{_c('1', msg)}\n{'-' * len(msg)}")


# --------------------------------------------------------------------------- #
# Patch hunk model
# --------------------------------------------------------------------------- #


@dataclass
class Hunk:
    name: str
    # Returns True if this hunk's change is already present.
    applied: Callable[[str], bool]
    # Returns new text, or None if the anchor could not be located.
    apply: Callable[[str], Optional[str]]
    required: bool = True


def _replace_once(text: str, old: str, new: str) -> Optional[str]:
    if text.count(old) != 1:
        return None
    return text.replace(old, new, 1)


# --------------------------------------------------------------------------- #
# bot.py hunks
# --------------------------------------------------------------------------- #

PROBE_HELPER = f'''
# --- {MARKER}:probe (begin) ------------------------------------------------- #


def probe_rpc_endpoint(url: str, timeout: float = 5.0) -> str:
    """Resolve an RPC URL and attempt a raw TCP connect on every address family.

    A ConnectTimeoutError from `requests` cannot distinguish "DNS gave me an
    IPv6 address but the server only listens on IPv4" from "the service is
    down". This probe separates them: it reports which families DNS returned
    and which of them actually accept a TCP connection.
    """
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or 18083
    out = [f"probe {{host}}:{{port}}"]

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        out.append(f"  DNS: FAILED ({{exc}})")
        out.append("  => the hostname does not resolve. Check the service name.")
        return "\\n".join(out)

    if not infos:
        out.append("  DNS: resolved to nothing")
        return "\\n".join(out)

    reachable = {{}}
    for family, socktype, proto, _canon, sockaddr in infos:
        label = "IPv6" if family == socket.AF_INET6 else "IPv4"
        addr = sockaddr[0]
        try:
            sock = socket.socket(family, socktype, proto)
        except OSError as exc:
            # e.g. IPv6 disabled in the kernel/container.
            out.append(f"  {{label}} {{addr}} -> unavailable locally ({{exc}})")
            reachable.setdefault(label, False)
            continue
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            out.append(f"  {{label}} {{addr}} -> CONNECTED")
            reachable[label] = True
        except socket.timeout:
            out.append(f"  {{label}} {{addr}} -> TIMEOUT (packets dropped)")
            reachable.setdefault(label, False)
        except OSError as exc:
            out.append(f"  {{label}} {{addr}} -> {{type(exc).__name__}}: {{exc}}")
            reachable.setdefault(label, False)
        finally:
            sock.close()

    if not any(reachable.values()):
        if reachable.get("IPv6") is False and "IPv4" not in reachable:
            out.append(
                "  => DNS returned ONLY IPv6 and nothing is listening there.\\n"
                "     Railway's private network is IPv6-only on legacy\\n"
                "     environments. monero-wallet-rpc must be started with\\n"
                "     --rpc-use-ipv6 --rpc-bind-ipv6-address :: --confirm-external-bind\\n"
                "     Binding 0.0.0.0 alone is IPv4-only and is NOT reachable."
            )
        else:
            out.append("  => no address family accepted a connection.")
    return "\\n".join(out)


# --- {MARKER}:probe (end) --------------------------------------------------- #

'''

BOT_HUNKS: list[Hunk] = [
    Hunk(
        name="add probe_rpc_endpoint() helper",
        applied=lambda t: "def probe_rpc_endpoint(" in t,
        apply=lambda t: _replace_once(
            t, "\ndef now_ts() -> int:", PROBE_HELPER + "\ndef now_ts() -> int:"
        ),
    ),
    Hunk(
        name="split RPC timeout into (connect, read)",
        applied=lambda t: "timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT)" in t,
        apply=lambda t: _replace_once(
            t,
            "            resp = self._session.post(self._url, json=payload, timeout=30)",
            "            resp = self._session.post(\n"
            "                self._url,\n"
            "                json=payload,\n"
            "                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),\n"
            "            )",
        ),
    ),
    Hunk(
        name="define timeout constants",
        applied=lambda t: "_CONNECT_TIMEOUT =" in t,
        apply=lambda t: _replace_once(
            t,
            'LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"',
            'LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"\n'
            "\n"
            f"# {MARKER}: a dead IPv6 listener drops packets rather than refusing\n"
            "# them, so the connect phase hangs for the full timeout. Keep the\n"
            "# connect timeout short to fail fast, and the read timeout long\n"
            "# because refresh/get_transfers on a large wallet is genuinely slow.\n"
            "_CONNECT_TIMEOUT = 10\n"
            "_READ_TIMEOUT = 120",
        ),
    ),
    Hunk(
        name="include socket probe in boot-timeout error",
        applied=lambda t: "probe = await asyncio.to_thread(probe_rpc_endpoint" in t,
        apply=lambda t: _replace_once(
            t,
            '        raise PaymentError(\n'
            '            f"wallet-rpc at {self._url} did not respond within {timeout}s.\\n"\n'
            '            f"  Last error: {last}\\n"\n'
            '            "  Check: is the wallet service running, is the service name in\\n"\n'
            '            "  MONERO_RPC_URL correct, and is it listening on that port?"\n'
            '        )',
            '        probe = await asyncio.to_thread(probe_rpc_endpoint, self._url)\n'
            '        raise PaymentError(\n'
            '            f"wallet-rpc at {self._url} did not respond within {timeout}s.\\n"\n'
            '            f"  Last error: {last}\\n"\n'
            f'            f"\\n{{probe}}\\n"\n'
            '        )',
        ),
    ),
    Hunk(
        name="include socket probe in /diag output",
        applied=lambda t: 'lines = [await asyncio.to_thread(probe_rpc_endpoint' in t,
        apply=lambda t: _replace_once(
            t,
            '        lines = [f"RPC URL: {self._url}"]',
            '        lines = [await asyncio.to_thread(probe_rpc_endpoint, self._url)]\n'
            '        lines.insert(0, f"RPC URL: {self._url}")',
        ),
    ),
]


# --------------------------------------------------------------------------- #
# Shell / Dockerfile hunks
# --------------------------------------------------------------------------- #

IPV6_FLAGS = (
    "    --rpc-bind-ip 0.0.0.0 \\\n"
    f"    `# {MARKER}: Railway private networking is IPv6-only on legacy` \\\n"
    "    `# environments. Binding 0.0.0.0 alone listens on IPv4 only, so the` \\\n"
    "    `# bot resolves the AAAA record, dials IPv6, and times out. These` \\\n"
    "    `# three flags add a dual-stack listener.` \\\n"
    "    --rpc-use-ipv6 \\\n"
    "    --rpc-bind-ipv6-address :: \\\n"
)


def _shell_hunks() -> list[Hunk]:
    return [
        Hunk(
            name="bind IPv6 as well as IPv4 (--rpc-use-ipv6 / ::)",
            applied=lambda t: "--rpc-use-ipv6" in t,
            apply=lambda t: _replace_once(
                t, "    --rpc-bind-ip 0.0.0.0 \\\n", IPV6_FLAGS
            ),
        ),
    ]


DOCKERFILE_HUNKS: list[Hunk] = [
    Hunk(
        name="document the IPv6 requirement",
        applied=lambda t: f"{MARKER}" in t,
        apply=lambda t: (
            f"# {MARKER}: this image must listen on BOTH stacks.\n"
            "# Railway private networking is IPv6-only on environments created\n"
            "# before 2025-10-16; <service>.railway.internal publishes an AAAA\n"
            "# record. The daemon flags live in entrypoint.sh.\n" + t
        ),
        required=False,
    ),
]


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #


def find_file(names: list[str], root: Path) -> Optional[Path]:
    for name in names:
        direct = root / name
        if direct.is_file():
            return direct
    skipdirs = {".git", "node_modules", "__pycache__", ".venv", "venv"}
    for path in root.rglob("*"):
        if path.is_file() and path.name in names:
            if any(part in skipdirs for part in path.parts):
                continue
            return path
    return None


# --------------------------------------------------------------------------- #
# Patch engine
# --------------------------------------------------------------------------- #


def patch_file(
    path: Path, hunks: list[Hunk], dry_run: bool, verify_python: bool
) -> bool:
    header(f"{path}")
    original = path.read_text()
    text = original
    changed = False
    hard_failure = False

    for hunk in hunks:
        if hunk.applied(text):
            skip(f"{hunk.name} — already present")
            continue
        result = hunk.apply(text)
        if result is None:
            if hunk.required:
                fail(f"{hunk.name} — anchor not found; file may have diverged")
                hard_failure = True
            else:
                warn(f"{hunk.name} — anchor not found, skipping (optional)")
            continue
        text = result
        changed = True
        ok(hunk.name)

    if hard_failure:
        fail("aborting this file; no changes written")
        return False

    if not changed:
        skip("nothing to do — already patched")
        return True

    if verify_python:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        try:
            py_compile.compile(tmp_path, doraise=True)
            ok("python syntax verified before writing")
        except py_compile.PyCompileError as exc:
            fail(f"patched output does not compile — refusing to write:\n{exc}")
            return False
        finally:
            os.unlink(tmp_path)

    if path.suffix == ".sh" or path.name == "entrypoint.sh":
        with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        try:
            proc = subprocess.run(
                ["bash", "-n", tmp_path], capture_output=True, text=True
            )
            if proc.returncode != 0:
                fail(f"patched shell script has a syntax error:\n{proc.stderr}")
                return False
            ok("bash syntax verified before writing")
        except FileNotFoundError:
            warn("bash not available; skipping shell syntax check")
        finally:
            os.unlink(tmp_path)

    if dry_run:
        diff = difflib.unified_diff(
            original.splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
        )
        print()
        sys.stdout.writelines(diff)
        return True

    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)
        ok(f"backup written to {backup.name}")
    path.write_text(text)
    ok("changes written")
    return True


def revert(root: Path) -> int:
    header("Reverting from .bak files")
    restored = 0
    for backup in root.rglob("*.bak"):
        target = backup.with_suffix("")
        shutil.copy2(backup, target)
        ok(f"{target} restored")
        restored += 1
    if not restored:
        warn("no .bak files found")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--dry-run", action="store_true", help="show diffs only")
    parser.add_argument("--revert", action="store_true", help="restore .bak files")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if args.revert:
        return revert(root)

    print(_c("1", "\nRailway IPv6 private-networking fix"))
    print(f"repository root: {root}")
    if args.dry_run:
        print(_c("33", "DRY RUN — no files will be modified"))

    results: list[bool] = []

    bot = find_file(["bot.py"], root)
    if bot is None:
        fail("bot.py not found")
        results.append(False)
    else:
        results.append(patch_file(bot, BOT_HUNKS, args.dry_run, True))

    entry = find_file(["entrypoint.sh"], root)
    dockerfile = find_file(["Dockerfile.walletrpc"], root)

    if entry is not None:
        results.append(patch_file(entry, _shell_hunks(), args.dry_run, False))
    elif dockerfile is not None and "--rpc-bind-ip 0.0.0.0" in dockerfile.read_text():
        # Flags are inline in the Dockerfile rather than in an entrypoint.
        results.append(patch_file(dockerfile, _shell_hunks(), args.dry_run, False))
    else:
        warn("entrypoint.sh not found and no inline bind flags in Dockerfile")

    if dockerfile is not None:
        results.append(
            patch_file(dockerfile, DOCKERFILE_HUNKS, args.dry_run, False)
        )
    else:
        fail("Dockerfile.walletrpc not found")
        results.append(False)

    header("Result")
    if all(results) and results:
        ok("all patches applied")
        print(
            "\nCommit and push. Railway redeploys automatically.\n"
            "After the wallet service restarts, its log should show a line like:\n"
            "  Binding on 0.0.0.0 (IPv4):18083 and :: (IPv6):18083\n"
            "Then DM the bot: /admin <password> then /diag\n"
            "The report now includes a per-address-family TCP probe.\n"
        )
        return 0

    fail("some patches did not apply — see above")
    print(
        "\nIf an anchor was not found, your file has diverged from the version\n"
        "this patcher expects. Nothing was written for that file.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
