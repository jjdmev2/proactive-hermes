#!/usr/bin/env python3
"""Mail push: IMAP IDLE on the selected mailboxes wakes the mail radar within about a minute.

It only POKES the plugin's own mail radar cron job (the job `hermes proactive setup` registered under
the key "mail_radar"): the job's next run moves to now and its gate reads mail as usual. A radar the owner
paused or disabled is never poked (a poke would un-pause it). It never calls a model, never posts, never
reads or stores mail content, and never logs addresses, account names or secrets: the log names an account
by its position in mail.himalaya_accounts ("acct#2"). Polling on the radar's schedule stays on as the safety net.

IDLE is renewed every 9 minutes and the socket has TCP keepalive (60 s idle, 20 s interval, 3 probes), so a
dead NAT path is noticed within minutes. Errors reconnect with jittered backoff (5 s doubling to 5 min, reset
only after a full IDLE round); a refused login waits 1 h. Log: logs/proactive-hermes-mail-push.log (0600,
rotated at 1 MB, 3 kept).

Accounts: mail.himalaya_accounts, the himalaya accounts setup selected for the mail radar (names or
{"account": name}). Empty means none: the desk never opens a mailbox it was not given, the same rule
the radar and the look-ahead follow. Logins come from the himalaya config (password `raw` or `cmd`;
other auth, e.g. OAuth or keyring, stays on polling). Mail read through the Gmail API only (mail.gmail_api)
has no IMAP login here and stays on polling.

Inside the gateway, `start_in_gateway(ctx)` runs it as a supervised task (`ctx.spawn_task`); blocking
IMAP lives in one thread per account. CLI loads never start it. Feature flag: features.mail_push.
With `plugins.isolation: host` the plugin host cannot tell a gateway from a CLI parent, so push stays off
there and `hermes proactive doctor` reports polling only.

    python3 mail_idle.py --once-test   connect each selected account, report idle-capable yes/no, exit
    python3 mail_idle.py --dry-run     run in the foreground and log "would poke" instead of poking
"""
from __future__ import annotations

import argparse
import asyncio
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
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
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

log = logging.getLogger("proactive_hermes.mail_idle")
log.addHandler(logging.NullHandler())


def himalaya_config() -> Path:
    env = os.environ.get("HIMALAYA_CONFIG")
    if env:
        return Path(env).expanduser()
    candidates = [Path(os.environ["XDG_CONFIG_HOME"]) / "himalaya" / "config.toml"] if os.environ.get("XDG_CONFIG_HOME") else []
    candidates += [Path.home() / ".config" / "himalaya" / "config.toml", Path.home() / ".himalayarc"]
    return next((c for c in candidates if c.is_file()), candidates[0])


def heartbeat_path() -> Path:
    return phconf.state_dir() / "mail_idle.json"


def log_path() -> Path:
    return phconf.hermes_home() / "logs" / "proactive-hermes-mail-push.log"


def acct_label(name: str, names: list[str] | None = None) -> str:
    """How the log names an account: its 1-based position in mail.himalaya_accounts, never the name."""
    names = selected_accounts() if names is None else names
    return f"acct#{names.index(name) + 1}" if name in names else "acct#?"


def selected_accounts(cfg: dict | None = None) -> list[str]:
    """The himalaya accounts setup selected for the mail radar. Empty = none (never all)."""
    raw = ((cfg or phconf.CFG).get("mail") or {}).get("himalaya_accounts")
    names = [str((a.get("account") if isinstance(a, dict) else a) or "").strip() for a in (raw if isinstance(raw, list) else [])]
    return list(dict.fromkeys(n for n in names if n))


def poke_window(cfg: dict | None = None) -> tuple[int, int]:
    """Active hours = outside quiet hours: (quiet end, quiet start)."""
    q = (cfg or phconf.CFG).get("quiet_hours") or {}
    return int(q.get("end", 7)), int(q.get("start", 23))


# ---------------------------------------------------------------- config

@dataclass
class Account:
    name: str
    host: str
    port: int = 993
    login: str = ""
    tls: str = "tls"                     # tls | start-tls | none
    auth_kind: str = "raw"               # raw | cmd
    _secret: str | list = field(default="", repr=False)   # list = argv run without a shell

    def password(self, run=subprocess.run) -> str:
        if self.auth_kind == "raw":
            return self._secret
        argv = [str(x) for x in self._secret] if isinstance(self._secret, list) else ["sh", "-c", self._secret]
        proc = run(argv, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise AuthError("auth command failed")
        line = (proc.stdout or "").splitlines()
        if not line or not line[0].strip():
            raise AuthError("auth command printed nothing")
        return line[0].strip()


class AuthError(PermissionError):
    """The password could not be obtained or the server refused it: retried only every AUTH_BACKOFF_S."""


def load_accounts(path: Path | None = None, only: list[str] | None = None) -> tuple[list[Account], dict[str, str]]:
    """Accounts with a readable IMAP password login, plus {name: reason} for the ones skipped.
    Reasons never contain a secret."""
    with open(path or himalaya_config(), "rb") as fh:
        cfg = tomllib.load(fh)
    accounts: list[Account] = []
    skipped: dict[str, str] = {}
    for name, acct in (cfg.get("accounts") or {}).items():
        if only is not None and name not in only:
            continue
        if not isinstance(acct, dict):
            skipped[name] = "not a table"
            continue
        backend = acct.get("backend") or {}
        if (backend.get("type") or "").lower() != "imap":
            skipped[name] = f"backend {backend.get('type') or 'missing'} is not imap"
            continue
        host = backend.get("host")
        if not host:
            skipped[name] = "no imap host"
            continue
        auth = backend.get("auth") or {}
        kind = (auth.get("type") or "password").lower()
        if kind != "password":
            skipped[name] = f"auth type {kind} not supported"
            continue
        if isinstance(auth.get("raw"), str) and auth["raw"]:
            auth_kind, secret = "raw", auth["raw"]
        elif isinstance(auth.get("cmd"), str) and auth["cmd"].strip():
            auth_kind, secret = "cmd", auth["cmd"]
        elif isinstance(auth.get("cmd"), list) and auth["cmd"]:
            auth_kind, secret = "cmd", [str(x) for x in auth["cmd"]]
        elif "keyring" in auth:
            skipped[name] = "keyring auth not supported"
            continue
        else:
            skipped[name] = "no password source"
            continue
        enc = backend.get("encryption")
        tls = str((enc.get("type") if isinstance(enc, dict) else enc) or "tls").lower()
        accounts.append(Account(name=name, host=str(host), port=int(backend.get("port") or (993 if tls == "tls" else 143)),
                                login=str(backend.get("login") or acct.get("email") or ""), tls=tls,
                                auth_kind=auth_kind, _secret=secret))
    if only is not None:
        for name in only:
            if name not in skipped and not any(a.name == name for a in accounts):
                skipped[name] = "not in himalaya config"
    return accounts, skipped


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
    """The plugin's own mail radar job: jobs.json key "mail_radar" ({"id": ...} or a bare id)."""
    try:
        reg = json.loads((phconf.data_dir() / "jobs.json").read_text())
    except (FileNotFoundError, ValueError):
        return None
    entry = reg.get("mail_radar") if isinstance(reg, dict) else None
    jid = entry.get("id") if isinstance(entry, dict) else entry
    return jid if isinstance(jid, str) and re.fullmatch(r"[0-9a-f]{12}", jid) else None


def job_paused(job: dict) -> bool:
    """A job the owner paused or disabled. trigger_job would set enabled and clear paused_at, so never poke it."""
    return job.get("enabled") is False or bool(job.get("paused_at")) or str(job.get("state") or "") == "paused"


def _job_from_file(job_id: str, path: Path | None = None) -> dict | None:
    """The job row from <HERMES_HOME>/cron/jobs.json ({"jobs": [...]} or a bare list); None when unreadable."""
    try:
        data = json.loads((path or phconf.hermes_home() / "cron" / "jobs.json").read_text())
    except (OSError, ValueError):
        return None
    rows = data.get("jobs") if isinstance(data, dict) else data
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("id") == job_id:
            return row
    return {}


def radar_job_state(job_id: str) -> dict | None:
    """The radar's cron row: in-process through cron.jobs when inside Hermes, else jobs.json. {} = not found,
    None = unknown."""
    try:
        from cron.jobs import get_job
    except Exception:
        return _job_from_file(job_id)
    try:
        return get_job(job_id) or {}
    except Exception as exc:
        log.warning("reading the radar job failed: %s", type(exc).__name__)
        return None


def _trigger_in_process(job_id: str) -> bool | None:
    """True/False when cron.jobs is importable (inside Hermes), None when it is not."""
    try:
        from cron.jobs import trigger_job
    except Exception:
        return None
    try:
        return trigger_job(job_id) is not None
    except Exception as exc:
        log.warning("trigger_job failed: %s", type(exc).__name__)
        return False


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


def poke_radar(*, dry_run: bool = False, job_id: str | None = None, in_process=_trigger_in_process,
               cli=_trigger_cli, job_state=None) -> bool | None:
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
    if dry_run:
        log.info("dry-run: would poke the mail radar")
        return True
    ok = in_process(jid)
    if ok is None:
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
        names = [a.name for a in accounts] + [n for n in skipped if n not in {a.name for a in accounts}]
        labels = labels or {n: f"acct#{i}" for i, n in enumerate(names, 1)}
        self.workers = [Worker(a, self.debouncer, self.status, self.stop, connect_fn, label=labels.get(a.name))
                        for a in accounts]
        self._last_hb = 0.0

    def start(self) -> None:
        for w in self.workers:
            w.start()

    def step(self) -> None:
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

    async def run_async(self) -> None:
        """Supervised by ctx.spawn_task: blocking IMAP stays in threads; cancel stops them cleanly."""
        self.start()
        try:
            while not self.stop.is_set():
                if self.debouncer.is_due():
                    await asyncio.to_thread(self.debouncer.tick)
                if self.status.changed or time.monotonic() - self._last_hb >= HEARTBEAT_EVERY:
                    await asyncio.to_thread(self.status.write)
                    self._last_hb = time.monotonic()
                await asyncio.sleep(1)
        finally:
            await asyncio.to_thread(self.shutdown)


# ---------------------------------------------------------------- gateway start

_SERVICE: Service | None = None
_LOCK = threading.Lock()


def enabled(cfg: dict | None = None) -> bool:
    cfg = cfg or phconf.CFG
    feats = cfg.get("features") or {}
    return bool(feats.get("mail_push", True) and feats.get("mail_radar", True) and selected_accounts(cfg))


def is_gateway_process() -> bool:
    if "gateway.run" in sys.modules:
        return True
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    return "gateway" in argv and "run" in argv[argv.index("gateway") + 1:]


def _gateway_loop():
    try:
        from gateway.run import _gateway_runner_ref
        loop = getattr(_gateway_runner_ref(), "_gateway_loop", None)
    except Exception:
        return None
    return loop if loop is not None and loop.is_running() and not loop.is_closed() else None


def build_service(*, dry_run: bool = False) -> Service:
    names = selected_accounts()
    accounts, skipped = load_accounts(only=names)
    for name, reason in skipped.items():
        log.info("%s: polling only (%s)", acct_label(name, names), reason)
    return Service(accounts, skipped, lambda: poke_radar(dry_run=dry_run),
                   labels={n: acct_label(n, names) for n in names})


def stop_service() -> None:
    global _SERVICE
    with _LOCK:
        svc, _SERVICE = _SERVICE, None
    if svc is not None:
        svc.stop.set()
        for w in svc.workers:
            w.close()


def start_in_gateway(ctx, *, gateway: bool | None = None, wait_s: float = 600) -> str:
    """Start mail push once per process, only inside the gateway. Returns what happened (for logs/tests)."""
    global _SERVICE
    if not phconf.config_path().exists():
        return "not set up"
    if not enabled():
        return "disabled"
    if not (is_gateway_process() if gateway is None else gateway):
        return "not the gateway"
    with _LOCK:
        if _SERVICE is not None:
            return "already running"
        try:
            _SERVICE = svc = build_service()
        except Exception as exc:
            log.warning("mail push not started: %s", type(exc).__name__)
            return "config unreadable"
    _setup_file_log()
    try:
        ctx.on_unload(stop_service)
    except Exception:
        pass

    def spawn():
        ctx.spawn_task(svc.run_async(), name=TASK_NAME)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Plugins can load on a discovery thread before the gateway loop runs: hand off once it is up.
        def handoff():
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline and _SERVICE is svc:
                loop = _gateway_loop()
                if loop is not None:
                    loop.call_soon_threadsafe(spawn)
                    return
                time.sleep(1)
            log.warning("mail push not started: no gateway loop")
        threading.Thread(target=handoff, name="mail-push-handoff", daemon=True).start()
        return "waiting for the gateway loop"
    spawn()
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

def push_report(cfg: dict | None = None, *, now: datetime | None = None, path: Path | None = None) -> dict[str, str]:
    """{account: "push live" | "polling only (reason)"} for every mailbox the desk reads."""
    cfg = cfg or phconf.CFG
    now = now or datetime.now(timezone.utc)
    out: dict[str, str] = {}
    if (cfg.get("mail") or {}).get("gmail_api"):
        out["Gmail API"] = "polling only (Gmail API accounts have no IMAP login here)"
    names = selected_accounts(cfg)
    feats = cfg.get("features") or {}
    if not names:
        return out
    if not feats.get("mail_push", True) or not feats.get("mail_radar", True):
        for n in names:
            out[n] = "polling only (mail push is off in features)"
        return out
    try:
        beat = json.loads((path or heartbeat_path()).read_text())
        updated = datetime.fromisoformat(str(beat.get("updated")).replace("Z", "+00:00"))
        fresh = (now - updated).total_seconds() <= STALE_AFTER_S
        rows = beat.get("accounts") or {}
    except (FileNotFoundError, ValueError, TypeError):
        beat, fresh, rows = None, False, {}
    try:
        _, skipped = load_accounts(only=names)
    except Exception:
        skipped = {}
    for n in names:
        row = rows.get(n) if isinstance(rows, dict) else None
        if n in skipped:
            out[n] = f"polling only ({skipped[n]})"
        elif beat is None:
            out[n] = "polling only (push not running: start the gateway)"
        elif not fresh:
            out[n] = "polling only (push heartbeat is stale: is the gateway running?)"
        elif not isinstance(row, dict):
            out[n] = "polling only (not watched yet)"
        elif row.get("state") == "idle":
            out[n] = "push live"
        elif row.get("state") == "error":
            out[n] = f"polling only (reconnecting after {row.get('last_error_class') or 'an error'})"
        elif row.get("state") == "skipped":
            out[n] = f"polling only ({row.get('last_error_class') or 'skipped'})"
        else:
            out[n] = f"polling only ({row.get('state') or 'unknown'})"
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
        rows.append({"account": name, "idle_capable": "no", "reason": f"skipped: {reason}"})
    return rows


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once-test", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    _setup_file_log()
    names = selected_accounts()
    if not names:
        print("No mail accounts selected for the mail radar (mail.himalaya_accounts): nothing to watch.")
        return 0
    if a.once_test:
        accounts, skipped = load_accounts(only=names)
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
