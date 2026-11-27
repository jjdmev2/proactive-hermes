#!/usr/bin/env python3
"""Mail push: IMAP IDLE on the mailboxes the owner gave Hermes wakes the mail radar within 1 to 3 minutes.

Opt-in. A mailbox gets push only when the owner fills one of six slots in Settings -> Plugins -> Proactive
Hermes (or `hermes plugins config proactive-hermes`): Mailbox N address, Mailbox N app password, and for an
unknown provider Mailbox N IMAP server (port 993 unless set). The address, server and port are plugin settings;
the app password is a Hermes secret (PROACTIVE_MAIL_N_PASSWORD) read only through agent.secret_scope.get_secret,
when the gateway loads the plugin. Nothing is read from another tool's config, a keyring or a .env file. Every
other mailbox stays on the radar's schedule (15 minutes by default).

It only POKES the plugin's own mail radar cron job (the job `hermes proactive setup` registered under
the key "mail_radar") with the public `hermes cron run <id>`: the job runs now and its gate reads mail as usual.
A radar the owner paused or disabled (its `hermes cron list --all` badge is not [active]) is never poked. It never calls a model, never posts, never
reads or stores mail content, and never logs addresses or secrets: the log names a mailbox by its slot
("acct#2"). Polling on the radar's schedule stays on as the safety net.

IDLE is renewed every 9 minutes and the socket has TCP keepalive (60 s idle, 20 s interval, 3 probes), so a
dead NAT path is noticed within minutes. Errors reconnect with jittered backoff (5 s doubling to 5 min, reset
only after a full IDLE round); a refused login waits 1 h. Log: logs/proactive-hermes-mail-push.log (0600,
rotated at 1 MB, 3 kept).

Inside the gateway, `start_in_gateway(ctx)` runs the service loop on the plugin's own daemon thread, started in a
copy of the plugin-loading context (so it keeps the profile scope) and stopped by `ctx.on_unload`; blocking
IMAP lives in one thread per mailbox. CLI loads never start it. Feature flag: features.mail_push.
With `plugins.isolation: host` the plugin host cannot tell a gateway from a CLI parent, so push stays off
there and `hermes proactive doctor` reports 15 min checks only.

    python3 mail_idle.py --once-test   connect each filled slot, report idle-capable yes/no, exit
    python3 mail_idle.py --dry-run     run in the foreground and log "would poke" instead of poking
(run them inside Hermes, e.g. through `hermes proactive doctor`, so the app passwords are readable)
"""
from __future__ import annotations

import argparse
import contextvars
import imaplib
import json
import logging
import logging.handlers
import os
import random
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ph_jobs  # noqa: E402
import phconf  # noqa: E402

DEBOUNCE_S = 30
MIN_GAP_S = 120
RENEW_S = 9 * 60            # re-IDLE often: NAT/firewalls drop idle flows well before the 29 min server limit
BACKOFF = (5, 300)
AUTH_BACKOFF_S = 3600       # a refused login is not retried for an hour (and logged once per hour)
JITTER = 0.2                # +-20% on every reconnect wait
KEEPALIVE = {"TCP_KEEPIDLE": 60, "TCP_KEEPINTVL": 20, "TCP_KEEPCNT": 3}
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3
SOCK_TIMEOUT = 120
HEARTBEAT_EVERY = 60
STALE_AFTER_S = 10 * 60
TASK_NAME = "proactive-hermes:mail-push"

SLOTS = range(1, 7)         # Mailbox 1..6 in Settings -> Plugins -> Proactive Hermes
DEFAULT_PORT = 993
SETTINGS = "Settings -> Plugins -> Proactive Hermes (or `hermes plugins config proactive-hermes`)"
RESTART = "restart Hermes (`hermes gateway restart`, or quit and reopen the desktop app)"
# Well-known providers: the IMAP server is filled in for these domains. Any other domain needs Mailbox N IMAP server.
HOSTS = {
    "gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
    "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com", "mac.com": "imap.mail.me.com",
    "fastmail.com": "imap.fastmail.com", "fastmail.fm": "imap.fastmail.com",
}
# Outlook.com, Hotmail, Live and Microsoft 365 mail takes only Microsoft's own sign-in: password and app password
# logins are refused. Such a mailbox is never filled in with a server and stays on the radar schedule.
MICROSOFT = ("outlook", "hotmail", "live")   # outlook.com, hotmail.co.uk, live.com, ...
MICROSOFT_HOSTS = ("office365.com", "outlook.com", "office.com")   # outlook.office365.com, imap-mail.outlook.com, ...
YAHOO_HOST = "imap.mail.yahoo.com"           # yahoo.com, yahoo.co.uk, yahoo.fr, ...; not Yahoo Japan (yahoo.co.jp)

log = logging.getLogger("proactive_hermes.mail_idle")
log.addHandler(logging.NullHandler())


def heartbeat_path() -> Path:
    return phconf.state_dir() / "mail_idle.json"


def log_path() -> Path:
    return phconf.hermes_home() / "logs" / "proactive-hermes-mail-push.log"


def slot_label(n: int) -> str:
    """How the log and the heartbeat name a mailbox: its slot, never the address."""
    return f"acct#{n}"


def password_env(n: int) -> str:
    return f"PROACTIVE_MAIL_{n}_PASSWORD"


def poke_window(cfg: dict | None = None) -> tuple[int, int]:
    """Active hours = outside quiet hours: (quiet end, quiet start)."""
    q = (cfg or phconf.CFG).get("quiet_hours") or {}
    return int(q.get("end", 7)), int(q.get("start", 23))


# ---------------------------------------------------------------- config

def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().lower().rstrip(".") if "@" in address else ""


def auto_host(address: str) -> str | None:
    """The IMAP server of a well-known provider, from the address's domain. None for any other domain
    (and for Microsoft mail, which has no app passwords: see microsoft_only)."""
    domain = _domain(address)
    if not domain:
        return None
    if domain in HOSTS:
        return HOSTS[domain]
    first = domain.split(".", 1)[0]
    if "." not in domain:
        return None
    if first == "yahoo" and domain != "yahoo.co.jp":
        return YAHOO_HOST
    return None


def microsoft_only(address: str, host: str = "") -> bool:
    """True for a mailbox Microsoft opens only through its own sign-in: an outlook.*, hotmail.* or live.* address
    with no server filled in, or any address whose filled-in server is Microsoft's (Microsoft 365 on a company
    domain)."""
    host = host.strip().lower().rstrip(".")
    if host:
        return any(host == d or host.endswith("." + d) for d in MICROSOFT_HOSTS)
    domain = _domain(address)
    return "." in domain and domain.split(".", 1)[0] in MICROSOFT


def settings_keys() -> list[str]:
    return [f"mail_{n}_{part}" for n in SLOTS for part in ("address", "host", "port")]


def plugin_settings(ctx=None) -> dict:
    """The slot settings (address, server, port; never the password) through Hermes: ctx.get_config inside the
    plugin, else Hermes's read-only config loader (plugins.entries.proactive-hermes.settings)."""
    if ctx is not None and hasattr(ctx, "get_config"):
        out = {}
        for key in settings_keys():
            try:
                out[key] = ctx.get_config(key)
            except Exception:
                out[key] = None
        return out
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
    except Exception:
        return {}
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(phconf.PLUGIN) if isinstance(cfg, dict) else None
    settings = entry.get("settings") if isinstance(entry, dict) else None
    return {k: settings.get(k) for k in settings_keys()} if isinstance(settings, dict) else {}


@dataclass
class Account:
    name: str                            # slot label, e.g. "acct#1"; never the address
    host: str
    port: int = DEFAULT_PORT
    login: str = field(default="", repr=False)
    tls: str = "tls"                     # tls (implicit, port 993) | start-tls (port 143); never plain text
    _secret: str = field(default="", repr=False)
    slot: int = 0

    def password(self) -> str:
        return self._secret


class AuthError(PermissionError):
    """The server refused the app password: retried only every AUTH_BACKOFF_S."""


# Why a filled slot is not watched. Codes go into the heartbeat; doctor turns them into words and a next step.
REASONS = {
    "no_password": "no app password",
    "unknown_host": "unknown mail server for this address",
    "microsoft": "Microsoft allows only its own sign-in, no app passwords",
    "bad_port": "IMAP port is not a number",
    "bad_address": "the address is not a mail address",
    "secret_unavailable": "Hermes could not read the app password in the gateway",
}


def load_accounts(settings: dict | None = None, secret=None) -> tuple[list[Account], dict[str, str]]:
    """Accounts from the filled slots, plus {label: reason code} for filled slots that cannot be watched.
    Empty slots are ignored. Reasons never contain an address or a secret. `secret(name)` reads a declared
    secret through Hermes (phconf.secret -> agent.secret_scope.get_secret)."""
    settings = plugin_settings() if settings is None else settings
    secret = secret or phconf.secret
    accounts: list[Account] = []
    skipped: dict[str, str] = {}
    for n in SLOTS:
        address = str(settings.get(f"mail_{n}_address") or "").strip()
        if not address:
            continue
        label = slot_label(n)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
            skipped[label] = "bad_address"
            continue
        host = str(settings.get(f"mail_{n}_host") or "").strip()
        if microsoft_only(address, host):
            skipped[label] = "microsoft"
            continue
        host = host or auto_host(address)
        if not host:
            skipped[label] = "unknown_host"
            continue
        raw_port = settings.get(f"mail_{n}_port")
        try:
            port = int(raw_port) if raw_port not in (None, "") else DEFAULT_PORT
        except (TypeError, ValueError):
            skipped[label] = "bad_port"
            continue
        if not 0 < port < 65536:
            skipped[label] = "bad_port"
            continue
        try:
            password = secret(password_env(n))
        except Exception:
            skipped[label] = "secret_unavailable"
            continue
        if not password:
            skipped[label] = "no_password"
            continue
        accounts.append(Account(name=label, host=host, port=port, login=address,
                                tls="start-tls" if port == 143 else "tls", _secret=password, slot=n))
    return accounts, skipped


def filled_slots(settings: dict | None = None) -> dict[int, str]:
    """{slot: domain of the address} for every slot with an address (the domain only, for doctor)."""
    settings = plugin_settings() if settings is None else settings
    out = {}
    for n in SLOTS:
        address = str(settings.get(f"mail_{n}_address") or "").strip()
        if address:
            out[n] = address.rsplit("@", 1)[-1].lower() if "@" in address else "?"
    return out


# ---------------------------------------------------------------- heartbeat

def _iso(ts: datetime | None = None) -> str:
    return (ts or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mail_idle-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Status:
    def __init__(self, path: Path | None = None, clock=lambda: datetime.now(timezone.utc), tz: ZoneInfo | None = None):
        self.path = path
        self.clock = clock
        self.tz = tz or ZoneInfo("UTC")
        self.lock = threading.Lock()
        self.started = _iso(clock())
        self.accounts: dict[str, dict] = {}
        self.last_poke: str | None = None
        self.pokes_today = 0
        self._poke_day: str | None = None
        self.changed = True

    def set(self, name: str, state: str, error_class: str | None = None) -> None:
        with self.lock:
            row = self.accounts.setdefault(name, {"state": "starting", "last_event": None, "last_error_class": None,
                                                  "error_since": None})
            if state == "error":
                row["last_error_class"] = error_class
                row["error_since"] = row.get("error_since") or _iso(self.clock())
            elif state in ("idle", "skipped"):
                row["error_since"] = None
                if error_class:
                    row["last_error_class"] = error_class
            self.changed = self.changed or row["state"] != state
            row["state"] = state

    def event(self, name: str) -> None:
        with self.lock:
            self.accounts.setdefault(name, {"state": "idle", "last_event": None, "last_error_class": None,
                                            "error_since": None})["last_event"] = _iso(self.clock())
            self.changed = True

    def poked(self) -> None:
        with self.lock:
            now = self.clock()
            day = now.astimezone(self.tz).date().isoformat()
            if day != self._poke_day:
                self._poke_day, self.pokes_today = day, 0
            self.pokes_today += 1
            self.last_poke = _iso(now)
            self.changed = True

    def snapshot(self) -> dict:
        with self.lock:
            day = self.clock().astimezone(self.tz).date().isoformat()
            return {"pid": os.getpid(), "started": self.started, "updated": _iso(self.clock()),
                    "accounts": {k: dict(v) for k, v in self.accounts.items()}, "last_poke": self.last_poke,
                    "pokes_today": self.pokes_today if day == self._poke_day else 0}

    def write(self) -> None:
        self.changed = False
        try:
            atomic_write_json(self.path or heartbeat_path(), self.snapshot())
        except OSError as exc:
            log.warning("heartbeat write failed: %s", type(exc).__name__)


# ---------------------------------------------------------------- debounce

def in_window(when: datetime, window: tuple[int, int], tz: ZoneInfo) -> bool:
    start, end = window
    hour = when.astimezone(tz).hour
    if start == end:
        return True
    return start <= hour < end if start < end else (hour >= start or hour < end)


class Debouncer:
    """First event starts a DEBOUNCE_S timer; then one poke; at least MIN_GAP_S between pokes;
    pokes outside the window are dropped (the dirty flag is cleared, nothing queued)."""

    def __init__(self, poke, *, status: Status | None = None, mono=time.monotonic,
                 wall=lambda: datetime.now(timezone.utc), delay: float = DEBOUNCE_S, min_gap: float = MIN_GAP_S,
                 window: tuple[int, int] = (7, 23), tz: ZoneInfo | None = None):
        self.poke, self.status, self.mono, self.wall = poke, status, mono, wall
        self.delay, self.min_gap, self.window, self.tz = delay, min_gap, window, tz or ZoneInfo("UTC")
        self.lock = threading.Lock()
        self.dirty = False
        self.due: float | None = None
        self.last_poke: float | None = None
        self.sources: set[str] = set()

    def event(self, account: str) -> None:
        with self.lock:
            self.dirty = True
            self.sources.add(account)
            if self.due is None:
                self.due = self.mono() + self.delay

    def is_due(self) -> bool:
        with self.lock:
            return self.dirty and self.due is not None and self.mono() >= self.due

    def tick(self) -> bool:
        """Poke if due. Returns True when a poke was made."""
        with self.lock:
            if not self.dirty or self.due is None or self.mono() < self.due:
                return False
            now = self.mono()
            if self.last_poke is not None and now - self.last_poke < self.min_gap:
                self.due = self.last_poke + self.min_gap
                return False
            count = len(self.sources)
            self.dirty, self.due, self.sources = False, None, set()
            if not in_window(self.wall(), self.window, self.tz):
                log.info("new mail in quiet hours, dropped (the radar schedule covers it), accounts=%d", count)
                return False
            self.last_poke = now
        ok = False
        try:
            ok = self.poke()
        except Exception as exc:
            log.warning("poke failed: %s", type(exc).__name__)
        if ok is None:  # busy: keep the mail dirty and try again after the debounce delay
            with self.lock:
                self.dirty, self.last_poke = True, None
                self.due = self.mono() + self.delay
            return False
        if ok and self.status:
            self.status.poked()
        return bool(ok)


# ---------------------------------------------------------------- poke

def radar_job_id() -> str | None:
    """Read the current radar ID, including a verified replacement waiting only on history."""
    reg = ph_jobs.read_registry(phconf.data_dir() / "jobs.json")
    jid = ph_jobs.recorded_job_id(reg, "mail_radar", allow_pending=True)
    return jid if jid and re.fullmatch(r"[0-9a-f]{12}", jid) else None


def job_paused(job: dict) -> bool:
    """A job the owner paused or disabled: never poke it."""
    return job.get("enabled") is False or bool(job.get("paused_at")) or str(job.get("state") or "active") != "active"


def _hermes() -> str:
    return shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")


def _cron_list(run=subprocess.run) -> str | None:
    try:
        proc = run([_hermes(), "cron", "list", "--all"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                   timeout=60, env={**os.environ, "HERMES_HOME": str(phconf.hermes_home()), "NO_COLOR": "1"})
    except Exception as exc:
        log.warning("hermes cron list failed: %s", type(exc).__name__)
        return None
    return proc.stdout if proc.returncode == 0 else None


def radar_job_state(job_id: str, run=subprocess.run) -> dict | None:
    """The radar's state from the public `hermes cron list --all` ("  <id> [active|paused|disabled|completed]").
    {} = not found, None = unknown."""
    out = _cron_list(run)
    if out is None:
        return None
    m = re.search(rf"^[ \t]+{re.escape(job_id)} \[([^\]\n]+)\]", out, re.MULTILINE)
    if not m:
        return {} if re.search(r"^No scheduled jobs\b", out, re.MULTILINE) or re.search(
            r"^[ \t]+[0-9a-f]{12} \[", out, re.MULTILINE) else None
    return {"id": job_id, "state": m.group(1)}


_INFLIGHT: list = []   # the detached `hermes cron run` process, if any


def _trigger_cli(job_id: str, popen=subprocess.Popen) -> bool | None:
    """`hermes cron run` runs the job to completion in the CLI, so start it detached and never kill it
    (a killed CLI could orphan a claimed run). None while the previous one is still running."""
    if _INFLIGHT and _INFLIGHT[0].poll() is None:
        log.info("mail radar run still in flight, poke deferred")
        return None
    exe = shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")
    try:
        proc = popen([exe, "cron", "run", job_id], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True,
                     env={**os.environ, "HERMES_HOME": str(phconf.hermes_home()), "NO_COLOR": "1"})
    except Exception as exc:
        log.warning("hermes cron run failed: %s", type(exc).__name__)
        return False
    _INFLIGHT[:] = [proc]
    threading.Thread(target=proc.wait, name="mail-push-reaper", daemon=True).start()
    return True


class Fence:
    """Closed when its service is unloaded. A poke starts its `hermes cron run` only while holding the lock and
    seeing the fence open, and unload closes it under the same lock: after stop_service() returns, no poke from that
    service can start, even one already past its checks."""

    def __init__(self, generation: int = 0):
        self.lock = threading.Lock()
        self.closed = False
        self.generation = generation

    def close(self) -> None:
        with self.lock:
            self.closed = True

    def is_open(self) -> bool:
        return not self.closed and self.generation == _GENERATION


def poke_radar(*, dry_run: bool = False, job_id: str | None = None, cli=_trigger_cli,
               job_state=None, fence: Fence | None = None) -> bool | None:
    jid = job_id or radar_job_id()
    if not jid:
        log.warning("no mail radar job registered, nothing to poke")
        return False
    job = (job_state or radar_job_state)(jid)
    if job is None:
        log.warning("radar state unreadable, poke skipped")
        return False
    if not job:
        log.warning("mail radar job not found, poke skipped")
        return False
    if job_paused(job):
        log.info("radar paused, poke skipped")
        return False
    if fence is None:
        if dry_run:
            log.info("dry-run: would poke the mail radar")
            return True
        ok = cli(jid)
    else:
        with fence.lock:
            if not fence.is_open():
                log.info("mail push unloaded, poke dropped")
                return False
            if dry_run:
                log.info("dry-run: would poke the mail radar")
                return True
            ok = cli(jid)
    if ok is None:
        return None  # busy: the debouncer retries
    log.info("poked the mail radar: %s", "ok" if ok else "failed")
    return bool(ok)


# ---------------------------------------------------------------- IMAP

_EVENT = re.compile(rb"\*\s+(\d+)\s+(EXISTS|RECENT|EXPUNGE)\b", re.I)


class MailCounter:
    """Turns untagged responses into 'new mail' decisions: EXISTS that grows, or RECENT > 0."""

    def __init__(self, exists: int | None = None):
        self.exists = exists

    def feed(self, typ: str, num: int) -> bool:
        typ = typ.upper()
        if typ == "EXPUNGE":
            if self.exists:
                self.exists -= 1
            return False
        if typ == "RECENT":
            return num > 0
        if typ == "EXISTS":
            grew = self.exists is None or num > self.exists
            self.exists = num
            return grew
        return False


def drain_untagged(conn, on_resp=None) -> int:
    """Feed EXISTS/RECENT/EXPUNGE that imaplib queued in conn.untagged_responses (after NOOP) to on_resp and
    remove them, so the list never grows. EXPUNGE first so a later EXISTS count is compared correctly.
    Returns how many responses were consumed."""
    store = getattr(conn, "untagged_responses", None)
    if not isinstance(store, dict):
        return 0
    used = 0
    for typ in ("EXPUNGE", "EXISTS", "RECENT"):
        for raw in store.pop(typ, None) or []:
            used += 1
            try:
                num = int(raw.split()[0] if isinstance(raw, (bytes, str)) else raw)
            except (TypeError, ValueError, IndexError):
                continue
            if on_resp is not None:
                on_resp(typ, num)
    return used


def set_keepalive(sock) -> None:
    """SO_KEEPALIVE plus Linux-style probe timing where the platform has the options."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except (OSError, AttributeError):
        return
    for name, value in KEEPALIVE.items():
        opt = getattr(socket, name, None)
        if opt is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, opt, value)
        except OSError:
            pass


def refresh_capabilities(conn) -> None:
    """Servers may advertise IDLE only after login; imaplib keeps the pre-login list."""
    try:
        typ, dat = conn.capability()
        if typ == "OK" and dat and dat[-1]:
            raw = dat[-1].decode() if isinstance(dat[-1], bytes) else str(dat[-1])
            conn.capabilities = tuple(raw.upper().split())
    except Exception:
        pass


def connect(acct: Account) -> imaplib.IMAP4:
    ctx = ssl.create_default_context()
    if acct.tls == "tls":
        conn = imaplib.IMAP4_SSL(acct.host, acct.port, ssl_context=ctx, timeout=60)
    else:
        conn = imaplib.IMAP4(acct.host, acct.port, timeout=60)
        if acct.tls in ("start-tls", "starttls"):
            conn.starttls(ssl_context=ctx)
    try:
        conn.login(acct.login, acct.password())
    except imaplib.IMAP4.error:
        try:
            conn.shutdown()
        except Exception:
            pass
        raise AuthError("login refused") from None
    refresh_capabilities(conn)
    if conn.sock:
        conn.sock.settimeout(SOCK_TIMEOUT)
        set_keepalive(conn.sock)
    return conn


def select_inbox(conn) -> int | None:
    typ, data = conn.select("INBOX", readonly=True)
    if typ != "OK":
        raise RuntimeError("select failed")
    try:
        return int(data[0])
    except (TypeError, ValueError, IndexError):
        return None


def _parse(typ, data) -> tuple[str, int] | None:
    try:
        num = int((data[0] if isinstance(data, list) else data).split()[0])
    except Exception:
        return None
    return str(typ).upper(), num


def idle_builtin(conn, seconds: float, on_resp) -> None:
    with conn.idle(duration=seconds) as idler:
        for typ, data in idler:
            if str(typ).upper() == "BYE":
                raise imaplib.IMAP4.abort("server closed idle")
            parsed = _parse(typ, data)
            if parsed:
                on_resp(*parsed)


def idle_manual(conn, seconds: float, on_resp, mono=time.monotonic) -> None:
    """IDLE (RFC 2177) on the raw socket, for Pythons whose imaplib has no idle() (before 3.14)."""
    sock = conn.sock
    tag = conn._new_tag()
    conn.send(tag + b" IDLE\r\n")
    buf = b""

    def lines(timeout):
        nonlocal buf
        sock.settimeout(timeout)
        while b"\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise imaplib.IMAP4.abort("socket EOF")
            buf += chunk
        line, buf = buf.split(b"\r\n", 1)
        return line

    try:
        while True:
            line = lines(SOCK_TIMEOUT)
            if line.startswith(b"+"):
                break
            if line.startswith(tag):
                raise imaplib.IMAP4.error("idle refused")
        deadline = mono() + seconds
        while True:
            remaining = deadline - mono()
            if remaining <= 0:
                break
            try:
                line = lines(remaining)
            except (socket.timeout, TimeoutError):
                break
            if line.upper().startswith(b"* BYE"):
                raise imaplib.IMAP4.abort("server closed idle")
            m = _EVENT.match(line)
            if m:
                on_resp(m.group(2).decode().upper(), int(m.group(1)))
        conn.send(b"DONE\r\n")
        while True:
            line = lines(SOCK_TIMEOUT)
            if line.startswith(tag):
                if b" OK" not in line.upper():
                    raise imaplib.IMAP4.error("idle ended badly")
                break
            m = _EVENT.match(line)
            if m:
                on_resp(m.group(2).decode().upper(), int(m.group(1)))
    finally:
        sock.settimeout(SOCK_TIMEOUT)
        conn.tagged_commands.pop(tag, None)


def idle_once(conn, seconds: float, on_resp) -> None:
    if hasattr(imaplib.IMAP4, "idle"):
        idle_builtin(conn, seconds, on_resp)
    else:
        idle_manual(conn, seconds, on_resp)


class Worker(threading.Thread):
    def __init__(self, acct: Account, debouncer: Debouncer, status: Status, stop: threading.Event,
                 connect_fn=connect, renew: float = RENEW_S, backoff=BACKOFF, *, label: str | None = None,
                 auth_backoff: float = AUTH_BACKOFF_S, rand=random.uniform, mono=time.monotonic):
        self.label = label or "acct#?"
        super().__init__(name=f"mail-push:{self.label}", daemon=True)
        self.acct, self.debouncer, self.status, self.stop = acct, debouncer, status, stop
        self.connect_fn, self.renew, self.backoff = connect_fn, renew, backoff
        self.auth_backoff, self.rand, self.mono = auth_backoff, rand, mono
        self.conn = None
        self._auth_logged: float | None = None

    def close(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.shutdown()
            except Exception:
                pass

    def jitter(self, delay: float) -> float:
        return delay * self.rand(1 - JITTER, 1 + JITTER)

    def run(self) -> None:
        delay = self.backoff[0]
        name, label = self.acct.name, self.label
        while not self.stop.is_set():
            try:
                self.status.set(name, "connecting")
                self.conn = self.connect_fn(self.acct)
                if "IDLE" not in getattr(self.conn, "capabilities", ()):
                    self.status.set(name, "skipped", "NoIdleCapability")
                    log.info("%s: server has no IDLE, polling only", label)
                    self.close()
                    return
                counter = MailCounter(select_inbox(self.conn))
                drain_untagged(self.conn)   # what SELECT reported is the baseline, not new mail
                self.status.set(name, "idle")
                log.info("%s: idle on INBOX", label)
                self._auth_logged = None

                def on_resp(typ, num):
                    if counter.feed(typ, num):
                        self.status.event(name)
                        self.debouncer.event(name)
                        log.info("%s: new mail event", label)

                while not self.stop.is_set():
                    idle_once(self.conn, self.renew, on_resp)
                    if not self.stop.is_set():
                        self.conn.noop()
                        drain_untagged(self.conn, on_resp)
                    delay = self.backoff[0]   # one full IDLE round worked: the next error starts small again
            except PermissionError as exc:
                if self.stop.is_set():
                    break
                self.status.set(name, "error", type(exc).__name__)
                now = self.mono()
                if self._auth_logged is None or now - self._auth_logged >= self.auth_backoff:
                    log.warning("%s: login refused, retry in %ds", label, int(self.auth_backoff))
                    self._auth_logged = now
                self.close()
                self.stop.wait(self.jitter(self.auth_backoff))
            except Exception as exc:
                if self.stop.is_set():
                    break
                self.status.set(name, "error", type(exc).__name__)
                wait = self.jitter(delay)
                log.warning("%s: %s, reconnect in %.0fs", label, type(exc).__name__, wait)
                self.close()
                self.stop.wait(wait)
                delay = min(delay * 2, self.backoff[1])
            finally:
                self.close()
        self.status.set(name, "stopped")


# ---------------------------------------------------------------- service

class Service:
    def __init__(self, accounts: list[Account], skipped: dict[str, str], poke, *, status: Status | None = None,
                 connect_fn=connect, window: tuple[int, int] | None = None, tz: ZoneInfo | None = None,
                 labels: dict[str, str] | None = None):
        tz = tz or phconf.tz()
        self.status = status or Status(tz=tz)
        self.stop = threading.Event()
        self.debouncer = Debouncer(poke, status=self.status, window=window or poke_window(), tz=tz)
        for name, reason in skipped.items():
            self.status.set(name, "skipped", reason)
        labels = labels or {}
        self.workers = [Worker(a, self.debouncer, self.status, self.stop, connect_fn, label=labels.get(a.name, a.name))
                        for a in accounts]
        self._last_hb = 0.0
        self.thread: threading.Thread | None = None   # the service loop's own thread (start_in_gateway)
        self.fence = Fence(_GENERATION)

    def start(self) -> None:
        for w in self.workers:
            w.start()

    def step(self) -> None:
        if not self.fence.is_open():
            self.stop.set()   # an older generation: retire without poking
            return
        self.debouncer.tick()
        if self.status.changed or time.monotonic() - self._last_hb >= HEARTBEAT_EVERY:
            self.status.write()
            self._last_hb = time.monotonic()

    def shutdown(self) -> None:
        self.stop.set()
        for w in self.workers:
            w.close()
        for w in self.workers:
            w.join(timeout=5)
        for w in self.workers:
            self.status.set(w.acct.name, "stopped")
        self.status.write()

    def run_forever(self) -> None:
        self.start()
        try:
            while not self.stop.is_set():
                self.step()
                self.stop.wait(1)
        finally:
            self.shutdown()


# ---------------------------------------------------------------- gateway start

_SERVICE: Service | None = None
_LOCK = threading.Lock()
_GENERATION = 0          # bumped by every start and stop; a service only pokes while its generation is current
_RETIRING: list[threading.Thread] = []   # loop threads of stopped services that had not finished yet
JOIN_S = 15.0            # how long a stop or a new start waits for an old loop thread to finish


def enabled(cfg: dict | None = None) -> bool:
    """The feature flags. Which mailboxes get push is decided by the filled slots (load_accounts)."""
    cfg = cfg or phconf.CFG
    feats = cfg.get("features") or {}
    return bool(feats.get("mail_push", True) and feats.get("mail_radar", True))


def is_gateway_process() -> bool:
    if "gateway.run" in sys.modules:
        return True
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    return "gateway" in argv and "run" in argv[argv.index("gateway") + 1:]


def build_service(ctx=None, *, dry_run: bool = False, secret=None) -> Service:
    """Read the slots and their app passwords NOW, in the caller's context: inside the gateway that is plugin
    registration, where Hermes's profile secret scope applies. The IMAP threads only use what was read here,
    so they never call get_secret without a scope (a multiplexed gateway would refuse that read)."""
    accounts, skipped = load_accounts(plugin_settings(ctx), secret=secret)
    for label, reason in skipped.items():
        log.info("%s: 15 min checks only (%s)", label, REASONS.get(reason, reason))
    svc = Service(accounts, skipped, None)
    svc.debouncer.poke = lambda: poke_radar(dry_run=dry_run, fence=svc.fence)
    return svc


def stop_service() -> None:
    """ctx.on_unload: close the fence (no poke can start after this returns), stop the workers, then wait up to
    JOIN_S for the loop thread; one still running is kept in _RETIRING and joined before a new start."""
    global _SERVICE, _GENERATION
    with _LOCK:
        svc, _SERVICE = _SERVICE, None
        _GENERATION += 1
    if svc is None:
        return
    svc.fence.close()
    svc.stop.set()
    for w in svc.workers:
        w.close()
    thread = svc.thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(JOIN_S)
        if thread.is_alive():
            with _LOCK:
                _RETIRING.append(thread)


def _retire_old() -> bool:
    """Join the loop threads of stopped services. False when one is still running after JOIN_S."""
    for thread in list(_RETIRING):
        thread.join(JOIN_S)
        if thread.is_alive():
            return False
        _RETIRING.remove(thread)
    return True


def start_in_gateway(ctx, *, gateway: bool | None = None, secret=None) -> str:
    """Start mail push once per process, only inside the gateway. Returns what happened (for logs/tests)."""
    global _SERVICE, _GENERATION
    if not phconf.config_path().exists():
        return "not set up"
    if not enabled():
        return "disabled"
    if not (is_gateway_process() if gateway is None else gateway):
        return "not the gateway"
    with _LOCK:
        if _SERVICE is not None:
            return "already running"
        if not _retire_old():
            log.warning("mail push not started: the previous one is still stopping")
            return "previous still stopping"
        _GENERATION += 1
        try:
            svc = build_service(ctx, secret=secret)
        except Exception as exc:
            log.warning("mail push not started: %s", type(exc).__name__)
            return "config unreadable"
        if not svc.workers and not svc.status.accounts:
            return "no mailboxes"
        _SERVICE = svc
    _setup_file_log()
    try:
        ctx.on_unload(stop_service)
    except Exception:
        pass
    # The plugin's own daemon thread, in a copy of this context: it keeps the profile scope plugin loading had.
    thread = threading.Thread(target=contextvars.copy_context().run, args=(svc.run_forever,), name=TASK_NAME,
                              daemon=True)
    svc.thread = thread
    thread.start()
    return "started"


class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler whose files are created owner-only (0600) from the first byte, rotations included."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        return os.fdopen(fd, "a", encoding=self.encoding, errors=getattr(self, "errors", None))


def _setup_file_log(path: Path | None = None) -> None:
    try:
        path = path or log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if any(isinstance(h, logging.FileHandler) for h in log.handlers):
            return
        handler = PrivateRotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    except OSError:
        pass


# ---------------------------------------------------------------- doctor

def radar_every(cfg: dict | None = None) -> str:
    """'15 min checks' from the radar's cron schedule ("*/15 7-22 * * *"); 'scheduled checks' otherwise."""
    sched = str(((cfg or phconf.CFG).get("schedule") or {}).get("mail_radar") or "")
    m = re.match(r"\*/(\d+)\s", sched + " ")
    return f"{int(m.group(1))} min checks" if m else "scheduled checks"


def _next(reason: str, n: int, every: str = "15 min checks") -> str:
    box = f"Mailbox {n}"
    return {
        "no_password": f"Make an app password for this mailbox, paste it in {box} app password in {SETTINGS}, then {RESTART}.",
        "unknown_host": f"Fill {box} IMAP server (from your provider's help page, e.g. imap.example.com) in {SETTINGS}, then {RESTART}.",
        "bad_port": f"Set {box} IMAP port to a number (usually 993) or clear it, in {SETTINGS}, then {RESTART}.",
        "bad_address": f"Fix {box} address in {SETTINGS} (the full mail address), then {RESTART}.",
        "secret_unavailable": f"Run `hermes gateway restart`. If this stays, report it: Hermes could not give the plugin its app password.",
        "login_refused": f"The mail server refused the app password. Make a new one, paste it in {box} app password in {SETTINGS}, then {RESTART}. Push retries once an hour.",
        "microsoft": f"Nothing to do: this mailbox stays on {every}.",
        "no_idle": "Nothing to do: this mail server does not offer IMAP IDLE, so it stays on the radar schedule.",
        "not_running": f"{RESTART[0].upper() + RESTART[1:]}. Push starts with the gateway, so it needs a restart after setup or after filling a Mailbox slot.",
        "stale": "Check that Hermes is running for this profile (desktop app or `hermes gateway start`).",
        "not_loaded": f"{RESTART[0].upper() + RESTART[1:]} so push picks up the new settings.",
        "reconnecting": "Nothing to do yet: push reconnects by itself. If it stays like this, check the network or the mail server.",
        "feature_off": "Turn mail push back on: set features.mail_push to true with `hermes proactive setup`.",
    }.get(reason, "Run `hermes proactive doctor` again in a few minutes.")


def push_report(cfg: dict | None = None, *, now: datetime | None = None, path: Path | None = None,
                settings: dict | None = None, secret=None) -> dict[str, dict]:
    """One row per mailbox: {"mode": "instant push" | "<N> min checks", "reason": ..., "next": ...}.
    Rows are named "Mailbox N (domain)" for the slots, never by address; the radar's other mailboxes get one
    row each ("himalaya account <name>", "Gmail API"). Passwords are only checked for presence."""
    cfg = cfg or phconf.CFG
    now = now or datetime.now(timezone.utc)
    every = radar_every(cfg)
    settings = plugin_settings() if settings is None else settings
    slots = filled_slots(settings)
    out: dict[str, dict] = {}
    feats = cfg.get("features") or {}
    push_on = feats.get("mail_push", True) and feats.get("mail_radar", True)
    try:
        beat = json.loads((path or heartbeat_path()).read_text())
        updated = datetime.fromisoformat(str(beat.get("updated")).replace("Z", "+00:00"))
        fresh = (now - updated).total_seconds() <= STALE_AFTER_S
        rows = beat.get("accounts") or {}
    except (FileNotFoundError, ValueError, TypeError, AttributeError):
        beat, fresh, rows = None, False, {}
    try:
        _, skipped = load_accounts(settings, secret=secret)
    except Exception:
        skipped = {}
    for n, domain in slots.items():
        label, key = slot_label(n), f"Mailbox {n} ({domain})"
        row = rows.get(label) if isinstance(rows, dict) else None
        if not push_on:
            reason = "feature_off"
        elif label in skipped:
            reason = skipped[label]
        elif beat is None:
            reason = "not_running"
        elif not fresh:
            reason = "stale"
        elif not isinstance(row, dict):
            reason = "not_loaded"
        elif row.get("state") == "idle":
            out[key] = {"mode": "instant push"}
            continue
        elif row.get("state") == "error":
            reason = "login_refused" if row.get("last_error_class") in ("AuthError", "PermissionError") else "reconnecting"
        elif row.get("state") == "skipped" and row.get("last_error_class") == "NoIdleCapability":
            reason = "no_idle"
        elif row.get("state") == "skipped" and row.get("last_error_class") in REASONS:
            reason = "not_loaded"   # skipped when the gateway started; the settings changed since
        else:
            reason = "reconnecting"
        words = {"feature_off": "mail push is off in features", "not_running": "push is not running",
                 "stale": "push heartbeat is stale", "not_loaded": "settings changed after Hermes started",
                 "login_refused": "login refused", "reconnecting": "reconnecting",
                 "no_idle": "server has no IMAP IDLE"}.get(reason) or REASONS.get(reason, reason)
        out[key] = {"mode": every, "reason": words, "next": _next(reason, n, every)}
    free = [n for n in SLOTS if n not in slots]
    hint = (f"If this mailbox is not one of the Mailbox slots above: make an app password and fill Mailbox "
            f"{free[0]} address and app password in {SETTINGS}, then {RESTART}. Optional." if free else
            "All six Mailbox slots are used.")
    mail = cfg.get("mail") or {}
    if mail.get("gmail_api"):
        out["Gmail API"] = {"mode": every, "reason": "no push slot (read through Google's API)", "next": hint}
    for acc in mail.get("himalaya_accounts") or []:
        name = str((acc.get("account") if isinstance(acc, dict) else acc) or "").strip()
        if name:
            out[f"himalaya account {name}"] = {"mode": every, "reason": "no push slot, unless it is one of the Mailbox slots above",
                                               "next": hint}
    return out


# ---------------------------------------------------------------- CLI

def once_test(accounts: list[Account], skipped: dict[str, str], connect_fn=connect) -> list[dict]:
    rows = []
    for acct in accounts:
        row = {"account": acct.name, "idle_capable": "no"}
        try:
            conn = connect_fn(acct)
            try:
                select_inbox(conn)
                row["idle_capable"] = "yes" if "IDLE" in conn.capabilities else "no"
                if row["idle_capable"] == "no":
                    row["reason"] = "server has no IDLE"
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as exc:
            row["reason"] = type(exc).__name__
        rows.append(row)
    for name, reason in skipped.items():
        rows.append({"account": name, "idle_capable": "no", "reason": f"skipped: {REASONS.get(reason, reason)}"})
    return rows


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once-test", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    _setup_file_log()
    if not filled_slots():
        print(f"No mailbox slots filled in {SETTINGS}: nothing to watch.")
        return 0
    if a.once_test:
        accounts, skipped = load_accounts()
        for row in once_test(accounts, skipped):
            print(f"{row['account']}: idle-capable {row['idle_capable']}"
                  + (f" ({row['reason']})" if row.get("reason") else ""))
        return 0
    svc = build_service(dry_run=a.dry_run)
    try:
        svc.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
