"""Hermetic tests for runtime/mail_idle.py: fake clocks, fake IMAP, no network, no cron, no gateway."""
import asyncio
import json
import os
import socket
import stat
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime"))
import mail_idle as mi  # noqa: E402
import phconf  # noqa: E402

TOML = """
[accounts.rawacct]
email = "raw@example.org"
backend.type = "imap"
backend.host = "imap.example.org"
backend.port = 993
backend.encryption.type = "tls"
backend.login = "rawlogin@example.org"
backend.auth.type = "password"
backend.auth.raw = "s3cret-raw"

[accounts.cmdacct]
email = "cmd@example.org"
backend.type = "imap"
backend.host = "imap.example.org"
backend.auth.type = "password"
backend.auth.cmd = "pass show mail"

[accounts.oauth]
backend.type = "imap"
backend.host = "imap.example.org"
backend.auth.type = "oauth2"

[accounts.maildir]
backend.type = "maildir"
"""

TZ = ZoneInfo("Europe/Berlin")      # UTC+1 in November
DAY = datetime(2026, 11, 25, 11, 0, tzinfo=timezone.utc)    # 12:00 local
NIGHT = datetime(2026, 11, 25, 23, 0, tzinfo=timezone.utc)  # 00:00 local


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(TemporaryDirectory()))
        self.toml = self.tmp / "config.toml"
        self.toml.write_text(TOML)
        home = self.tmp / "profiles" / "example"
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps({"timezone": "Europe/Berlin",
                                           "mail": {"himalaya_accounts": ["rawacct", {"account": "cmdacct"}, "oauth"]}}))
        self.enterContext(patch.dict(os.environ, EA_STATE_DIR=str(self.tmp / "state"), HERMES_HOME=str(home),
                                     PROACTIVE_HERMES_CONFIG=str(self.config), HIMALAYA_CONFIG=str(self.toml)))
        self.enterContext(patch.object(phconf, "CFG", phconf.load()))
        self.enterContext(patch.object(phconf, "hermes_home", return_value=home))


class ConfigTests(Base):
    def test_raw_cmd_and_unsupported(self):
        accounts, skipped = mi.load_accounts()
        by = {a.name: a for a in accounts}
        self.assertEqual(set(by), {"rawacct", "cmdacct"})
        self.assertEqual(by["rawacct"].login, "rawlogin@example.org")
        self.assertEqual(by["rawacct"].password(), "s3cret-raw")
        fake = lambda *a, **k: SimpleNamespace(returncode=0, stdout="pw-from-cmd\nmore\n")
        self.assertEqual(by["cmdacct"].password(run=fake), "pw-from-cmd")
        self.assertIn("oauth2", skipped["oauth"])
        self.assertIn("not imap", skipped["maildir"])
        self.assertNotIn("s3cret", json.dumps(skipped) + repr(by["rawacct"]))

    def test_selection_follows_mail_radar_accounts(self):
        self.assertEqual(mi.selected_accounts(), ["rawacct", "cmdacct", "oauth"])
        accounts, skipped = mi.load_accounts(only=mi.selected_accounts())
        self.assertEqual({a.name for a in accounts}, {"rawacct", "cmdacct"})
        self.assertNotIn("maildir", skipped)        # not selected: never touched
        self.assertEqual(mi.selected_accounts({"mail": {"himalaya_accounts": []}}), [])  # empty = none

    def test_window_from_quiet_hours(self):
        self.assertEqual(mi.poke_window({"quiet_hours": {"start": 23, "end": 7}}), (7, 23))


class DebounceTests(unittest.TestCase):
    def make(self, wall=DAY):
        self.mono = Clock()
        self.pokes = []
        self.status = mi.Status(path=Path(self.enterContext(TemporaryDirectory())) / "hb.json",
                                clock=lambda: wall, tz=TZ)
        return mi.Debouncer(lambda: self.pokes.append(self.mono()) or True, status=self.status,
                            mono=self.mono, wall=lambda: wall, window=(7, 23), tz=TZ)

    def test_five_events_one_poke(self):
        d = self.make()
        for i in range(5):
            d.event("a" if i % 2 else "b")
            self.mono.t += 2
            d.tick()
        self.assertEqual(self.pokes, [])
        self.mono.t = 1030
        self.assertTrue(d.tick())
        for _ in range(10):
            self.mono.t += 5
            d.tick()
        self.assertEqual(len(self.pokes), 1)
        self.assertEqual(self.status.pokes_today, 1)

    def test_min_gap_between_pokes(self):
        d = self.make()
        d.event("a")
        self.mono.t += 30
        d.tick()
        d.event("a")
        self.mono.t += 30
        self.assertFalse(d.tick())
        self.mono.t = self.pokes[0] + 119
        self.assertFalse(d.tick())
        self.mono.t = self.pokes[0] + 120
        self.assertTrue(d.tick())
        self.assertGreaterEqual(self.pokes[1] - self.pokes[0], 120)

    def test_quiet_window_drops(self):
        d = self.make(wall=NIGHT)
        d.event("a")
        self.mono.t += 31
        self.assertFalse(d.tick())
        self.assertEqual(self.pokes, [])
        self.assertFalse(d.dirty)


class PokeTests(Base):
    def write_registry(self, entry):
        phconf.data_dir().mkdir(parents=True, exist_ok=True)
        (phconf.data_dir() / "jobs.json").write_text(json.dumps({"mail_radar": entry, "health": {"id": "0" * 12}}))

    def test_resolves_plugin_radar_job_by_key(self):
        self.write_registry({"id": "abcdef012345", "spec": "x"})
        self.assertEqual(mi.radar_job_id(), "abcdef012345")
        self.write_registry("0123456789ab")              # older bare-id registry
        self.assertEqual(mi.radar_job_id(), "0123456789ab")

    def test_in_process_first_then_cli_fallback(self):
        self.write_registry({"id": "abcdef012345"})
        seen = []
        live = lambda j: {"id": j, "enabled": True, "paused_at": None}
        self.assertTrue(mi.poke_radar(in_process=lambda j: seen.append(("in", j)) or True,
                                      cli=lambda j: self.fail("cli used"), job_state=live))
        self.assertTrue(mi.poke_radar(in_process=lambda j: None, cli=lambda j: seen.append(("cli", j)) or True,
                                      job_state=live))
        self.assertEqual(seen, [("in", "abcdef012345"), ("cli", "abcdef012345")])

    def test_no_job_no_poke_and_dry_run(self):
        self.assertFalse(mi.poke_radar(in_process=lambda j: self.fail("poked"), cli=lambda j: self.fail("poked")))
        self.write_registry({"id": "abcdef012345"})
        self.assertTrue(mi.poke_radar(dry_run=True, in_process=lambda j: self.fail("dry"), cli=lambda j: self.fail("dry"),
                                      job_state=lambda j: {"id": j, "enabled": True}))

    def test_cli_detached_uses_profile_home_and_defers_while_running(self):
        calls = []
        done = threading.Event()

        class Proc:
            def __init__(self, argv, **k):
                calls.append((argv, k["env"]["HERMES_HOME"], k["start_new_session"]))

            def poll(self):
                return 0 if done.is_set() else None

            def wait(self):
                done.wait(5)
                return 0

        mi._INFLIGHT.clear()
        self.assertTrue(mi._trigger_cli("abcdef012345", popen=Proc))
        self.assertEqual(calls[0][0][1:], ["cron", "run", "abcdef012345"])
        self.assertEqual(calls[0][1:], (str(phconf.hermes_home()), True))
        self.assertIsNone(mi._trigger_cli("abcdef012345", popen=Proc))
        done.set()
        mi._INFLIGHT.clear()

    def test_busy_poke_retries_later(self):
        mono = Clock()
        answers = [None, True]
        d = mi.Debouncer(lambda: answers.pop(0), mono=mono, wall=lambda: DAY, window=(0, 0))
        d.event("a")
        mono.t += 30
        self.assertFalse(d.tick())
        self.assertTrue(d.dirty)
        mono.t += 30
        self.assertTrue(d.tick())


class HeartbeatTests(unittest.TestCase):
    def test_atomic_0600_shape(self):
        tmp = Path(self.enterContext(TemporaryDirectory()))
        st = mi.Status(path=tmp / "hb.json", clock=lambda: DAY, tz=TZ)
        st.set("a", "idle")
        st.event("a")
        st.set("b", "error", "TimeoutError")
        st.poked()
        st.write()
        data = json.loads((tmp / "hb.json").read_text())
        self.assertEqual(set(data), {"pid", "started", "updated", "accounts", "last_poke", "pokes_today"})
        self.assertEqual(data["accounts"]["b"]["last_error_class"], "TimeoutError")
        self.assertEqual(data["pokes_today"], 1)
        self.assertEqual(stat.S_IMODE((tmp / "hb.json").stat().st_mode), 0o600)
        self.assertEqual([p.name for p in tmp.iterdir()], ["hb.json"])


class FakeConn:
    capabilities = ("IMAP4REV1", "IDLE")

    def select(self, box, readonly=False):
        return "OK", [b"5"]

    def noop(self):
        return "OK", [b""]

    def shutdown(self):
        pass

    def logout(self):
        pass


class WorkerTests(unittest.TestCase):
    def test_exists_reaches_debouncer_after_reconnect(self):
        stop = threading.Event()
        status = mi.Status(path=Path(self.enterContext(TemporaryDirectory())) / "hb.json")
        deb = mi.Debouncer(lambda: True, mono=Clock(), wall=lambda: DAY)
        attempts = []

        def connect(acct):
            attempts.append(1)
            if len(attempts) == 1:
                raise socket.timeout("x")
            return FakeConn()

        def fake_idle(conn, seconds, on_resp):
            on_resp("EXISTS", 5)
            on_resp("EXISTS", 6)
            stop.set()

        w = mi.Worker(mi.Account(name="a", host="h"), deb, status, stop, connect_fn=connect, backoff=(0.01, 0.02))
        with patch.object(mi, "idle_once", fake_idle):
            w.run()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(deb.sources, {"a"})
        self.assertEqual(status.accounts["a"]["last_error_class"], "TimeoutError")

    def test_manual_idle_for_older_python(self):
        script = [b"+ idling\r\n", b"* 7 EXISTS\r\n", None, b"A1 OK done\r\n"]
        sent = []

        class Sock:
            def settimeout(self, t):
                pass

            def recv(self, n):
                item = script.pop(0)
                if item is None:
                    raise socket.timeout()
                return item

        conn = SimpleNamespace(sock=Sock(), tagged_commands={}, send=sent.append, _new_tag=lambda: b"A1")
        seen = []
        mi.idle_manual(conn, 60, lambda t, n: seen.append((t, n)))
        self.assertEqual(seen, [("EXISTS", 7)])
        self.assertEqual(sent, [b"A1 IDLE\r\n", b"DONE\r\n"])


class FakeCtx:
    def __init__(self):
        self.spawned, self.unload = [], []

    def spawn_task(self, coro, *, name=None):
        self.spawned.append(name)
        coro.close()

    def on_unload(self, cb):
        self.unload.append(cb)


class GatewayStartTests(Base):
    def tearDown(self):
        mi.stop_service()

    def test_cli_load_never_starts(self):
        ctx = FakeCtx()
        self.assertEqual(mi.start_in_gateway(ctx, gateway=False), "not the gateway")
        self.assertEqual(ctx.spawned, [])

    def test_not_set_up_or_disabled_never_starts(self):
        ctx = FakeCtx()
        self.config.unlink()
        self.assertEqual(mi.start_in_gateway(ctx, gateway=True), "not set up")
        self.config.write_text("{}")
        with patch.object(phconf, "CFG", phconf.load()):
            self.assertEqual(mi.start_in_gateway(ctx, gateway=True), "disabled")      # no accounts selected
        cfg = phconf.load()
        cfg["features"]["mail_push"] = False
        cfg["mail"]["himalaya_accounts"] = ["rawacct"]
        with patch.object(phconf, "CFG", cfg):
            self.assertEqual(mi.start_in_gateway(ctx, gateway=True), "disabled")
        self.assertEqual(ctx.spawned, [])

    def test_gateway_with_loop_spawns_once(self):
        ctx = FakeCtx()

        async def go():
            return mi.start_in_gateway(ctx, gateway=True), mi.start_in_gateway(ctx, gateway=True)

        first, second = asyncio.run(go())
        self.assertEqual((first, second), ("started", "already running"))
        self.assertEqual(ctx.spawned, [mi.TASK_NAME])
        self.assertEqual(ctx.unload, [mi.stop_service])

    def test_is_gateway_process_reads_argv(self):
        with patch.object(sys, "argv", ["hermes", "-p", "x", "gateway", "run", "--replace"]), \
             patch.dict(sys.modules, {"gateway.run": None}):
            sys.modules.pop("gateway.run")
            self.assertTrue(mi.is_gateway_process())
        with patch.object(sys, "argv", ["hermes", "proactive", "doctor"]):
            self.assertFalse(mi.is_gateway_process())

    def test_default_feature_on_and_preset(self):
        self.assertIs(phconf.DEFAULTS["features"]["mail_push"], True)
        preset = json.loads((Path(__file__).resolve().parent.parent / "presets" / "juan.json").read_text())
        self.assertIs(preset["features"]["mail_push"], True)

    def test_run_async_cancels_cleanly(self):
        svc = mi.Service([mi.Account(name="a", host="h")], {}, lambda: True,
                         status=mi.Status(path=self.tmp / "hb.json"), connect_fn=lambda a: FakeConn(),
                         window=(7, 23), tz=TZ)

        def blocking_idle(conn, seconds, on_resp):
            svc.stop.wait(5)

        async def go():
            task = asyncio.create_task(svc.run_async())
            await asyncio.sleep(0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with patch.object(mi, "idle_once", blocking_idle):
            asyncio.run(go())
        self.assertTrue(svc.stop.is_set())
        self.assertFalse(any(w.is_alive() for w in svc.workers))
        self.assertEqual(json.loads((self.tmp / "hb.json").read_text())["accounts"]["a"]["state"], "stopped")


class DoctorTests(Base):
    def test_push_live_and_polling_reasons(self):
        now = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
        hb = self.tmp / "hb.json"
        hb.write_text(json.dumps({"updated": now.isoformat(), "accounts": {
            "rawacct": {"state": "idle"}, "cmdacct": {"state": "error", "last_error_class": "TimeoutError"}}}))
        cfg = phconf.load()
        cfg["mail"]["gmail_api"] = True
        rep = mi.push_report(cfg, now=now, path=hb)
        self.assertEqual(rep["rawacct"], "push live")
        self.assertIn("TimeoutError", rep["cmdacct"])
        self.assertTrue(rep["oauth"].startswith("polling only (auth type oauth2"))
        self.assertTrue(rep["Gmail API"].startswith("polling only"))
        stale = mi.push_report(cfg, now=now + timedelta(minutes=11), path=hb)
        self.assertIn("stale", stale["rawacct"])

    def test_doctor_report_has_mail_push(self):
        import ph_doctor
        with patch.object(ph_doctor, "check_gateway", return_value=(True, "running")), \
             patch.object(ph_doctor, "check_bot_chat", return_value=(True, "found")), \
             patch.object(ph_doctor, "check_mobile", return_value=(True, "")):
            rep = ph_doctor.report()
        self.assertEqual(set(rep["mail_push"]), {"rawacct", "cmdacct", "oauth"})
        self.assertTrue(all(v.startswith(("push live", "polling only")) for v in rep["mail_push"].values()))


class PausedRadarTests(Base):
    """A poke must never un-pause a radar the owner paused (trigger_job sets enabled and clears paused_at)."""
    JID = "abcdef012345"

    def setUp(self):
        super().setUp()
        phconf.data_dir().mkdir(parents=True, exist_ok=True)
        (phconf.data_dir() / "jobs.json").write_text(json.dumps({"mail_radar": {"id": self.JID}}))
        self.jobs = phconf.hermes_home() / "cron" / "jobs.json"
        self.jobs.parent.mkdir(parents=True, exist_ok=True)

    def fail_poke(self, j):
        self.fail("a paused radar was poked")

    def test_paused_disabled_or_state_paused_skips_with_log(self):
        for job in ({"id": self.JID, "enabled": False}, {"id": self.JID, "enabled": True, "paused_at": "2026-11-25T10:00"},
                    {"id": self.JID, "enabled": True, "state": "paused"}):
            with self.assertLogs(mi.log, "INFO") as cm:
                self.assertFalse(mi.poke_radar(job_state=lambda j, job=job: job, in_process=self.fail_poke,
                                               cli=self.fail_poke))
            self.assertIn("radar paused, poke skipped", "\n".join(cm.output))

    def test_unknown_or_missing_job_is_not_poked(self):
        self.assertFalse(mi.poke_radar(job_state=lambda j: None, in_process=self.fail_poke, cli=self.fail_poke))
        self.assertFalse(mi.poke_radar(job_state=lambda j: {}, in_process=self.fail_poke, cli=self.fail_poke))

    def test_outside_hermes_reads_profile_jobs_file(self):
        with patch.dict(sys.modules, {"cron.jobs": None}):
            self.assertIsNone(mi.radar_job_state(self.JID))                       # no file: unknown
            self.jobs.write_text(json.dumps({"jobs": [{"id": self.JID, "enabled": True, "paused_at": "x"}]}))
            self.assertTrue(mi.job_paused(mi.radar_job_state(self.JID)))
            self.assertFalse(mi.poke_radar(in_process=self.fail_poke, cli=self.fail_poke))
            self.jobs.write_text(json.dumps([{"id": self.JID, "enabled": True, "paused_at": None}]))
            self.assertTrue(mi.poke_radar(in_process=lambda j: True, cli=self.fail_poke))
            self.assertEqual(mi.radar_job_state("0" * 12), {})

    def test_inside_hermes_reads_job_through_cron_jobs(self):
        import types
        mod = types.ModuleType("cron.jobs")
        mod.get_job = lambda j: {"id": j, "enabled": True, "paused_at": "2026-11-25T10:00"}
        with patch.dict(sys.modules, {"cron": types.ModuleType("cron"), "cron.jobs": mod}):
            self.assertFalse(mi.poke_radar(in_process=self.fail_poke, cli=self.fail_poke))
            mod.get_job = lambda j: {"id": j, "enabled": True, "paused_at": None}
            self.assertTrue(mi.poke_radar(in_process=lambda j: True, cli=self.fail_poke))


class RecStop(threading.Event):
    """stop.wait() records the wait and stops the worker after `limit` waits."""

    def __init__(self, limit):
        super().__init__()
        self.waits, self.limit = [], limit

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if len(self.waits) >= self.limit:
            self.set()
        return self.is_set()


class IdleConn(FakeConn):
    def __init__(self):
        self.untagged_responses = {"RECENT": [b"2"]}     # what SELECT left behind: the baseline, not new mail
        self.sock = None

    def noop(self):
        self.untagged_responses.setdefault("EXISTS", []).append(b"6")
        return "OK", [b""]


class BackoffTests(unittest.TestCase):
    def worker(self, connect, stop, **kw):
        status = mi.Status(path=Path(self.enterContext(TemporaryDirectory())) / "hb.json")
        self.deb = mi.Debouncer(lambda: True, mono=Clock(), wall=lambda: DAY)
        return mi.Worker(mi.Account(name="secretname", host="h"), self.deb, status, stop, connect_fn=connect,
                         backoff=(5, 300), label="acct#2", **kw)

    def test_backoff_resets_only_after_a_full_idle_round(self):
        rounds = iter([0, 0, 1, 0])      # IDLE rounds that complete before the error, per connection

        def idle(conn, seconds, on_resp):
            if conn.left <= 0:
                raise socket.timeout("x")
            conn.left -= 1

        def connect(acct):
            conn = IdleConn()
            conn.left = next(rounds)
            return conn

        stop = RecStop(4)
        with patch.object(mi, "idle_once", idle):
            self.worker(connect, stop, rand=lambda a, b: 1.0).run()
        self.assertEqual(stop.waits, [5, 10, 5, 10])   # SELECT alone never reset; the full round did

    def test_jitter_is_plus_minus_20_percent(self):
        seen = []
        stop = RecStop(1)
        w = self.worker(lambda a: (_ for _ in ()).throw(socket.timeout("x")), stop,
                        rand=lambda a, b: seen.append((a, b)) or b)
        w.run()
        self.assertEqual(seen, [(0.8, 1.2)])
        self.assertAlmostEqual(stop.waits[0], 6.0)

    def test_auth_failure_waits_an_hour_and_logs_once_per_hour(self):
        def refuse(acct):
            raise mi.AuthError("login refused")

        stop = RecStop(3)
        with self.assertLogs(mi.log, "INFO") as cm:
            self.worker(refuse, stop, rand=lambda a, b: 1.0, mono=lambda: 5000.0).run()
        self.assertEqual(stop.waits, [3600, 3600, 3600])
        refused = [l for l in cm.output if "login refused" in l]
        self.assertEqual(len(refused), 1)
        self.assertIn("acct#2", refused[0])
        self.assertNotIn("secretname", "\n".join(cm.output))

    def test_renew_drains_noop_responses_into_the_counter(self):
        stop = threading.Event()
        conns = []

        def connect(acct):
            conns.append(IdleConn())
            return conns[-1]

        calls = []

        def idle(conn, seconds, on_resp):
            calls.append(seconds)
            if len(calls) == 2:
                stop.set()

        with patch.object(mi, "idle_once", idle):
            self.worker(connect, stop).run()
        self.assertEqual(calls, [mi.RENEW_S, mi.RENEW_S])
        self.assertEqual(self.deb.sources, {"secretname"})       # EXISTS 6 after NOOP > 5 from SELECT
        self.assertEqual(conns[0].untagged_responses, {})       # consumed, the list does not grow


class RenewKeepaliveTests(unittest.TestCase):
    def test_renew_every_9_minutes(self):
        self.assertEqual(mi.RENEW_S, 540)
        w = mi.Worker(mi.Account(name="a", host="h"), None, None, threading.Event())
        self.assertEqual(w.renew, 540)

    def test_keepalive_applied_on_connect(self):
        calls = []

        class Sock:
            def setsockopt(self, level, opt, value):
                calls.append((level, opt, value))

            def settimeout(self, t):
                pass

        class FakeSSL:
            def __init__(self, host, port, ssl_context=None, timeout=None):
                self.sock, self.capabilities = Sock(), ("IMAP4REV1",)

            def login(self, user, pw):
                return "OK", [b""]

            def capability(self):
                return "OK", [b"IMAP4rev1 IDLE"]

        with patch.object(mi.imaplib, "IMAP4_SSL", FakeSSL):
            conn = mi.connect(mi.Account(name="a", host="h", _secret="pw"))
        self.assertIn("IDLE", conn.capabilities)
        self.assertIn((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1), calls)
        for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 20), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, name):
                self.assertIn((socket.IPPROTO_TCP, getattr(socket, name), value), calls)


class DrainTests(unittest.TestCase):
    def test_drain_feeds_counter_and_clears_only_what_it_used(self):
        conn = SimpleNamespace(untagged_responses={"EXISTS": [b"6"], "RECENT": [b"1"], "EXPUNGE": [b"3"],
                                                   "FLAGS": [b"(\\Seen)"]})
        counter, seen = mi.MailCounter(5), []
        used = mi.drain_untagged(conn, lambda t, n: seen.append((t, n, counter.feed(t, n))))
        self.assertEqual(used, 3)
        self.assertEqual(seen, [("EXPUNGE", 3, False), ("EXISTS", 6, True), ("RECENT", 1, True)])
        self.assertEqual(conn.untagged_responses, {"FLAGS": [b"(\\Seen)"]})
        self.assertEqual(mi.drain_untagged(SimpleNamespace()), 0)


class AuthCmdTests(unittest.TestCase):
    def test_list_runs_without_shell_and_string_keeps_sh(self):
        seen = []
        run = lambda argv, **k: seen.append(argv) or SimpleNamespace(returncode=0, stdout="pw\n")
        mi.Account(name="a", host="h", auth_kind="cmd", _secret=["pass", "show", "mail box"]).password(run=run)
        mi.Account(name="a", host="h", auth_kind="cmd", _secret="pass show mail").password(run=run)
        self.assertEqual(seen, [["pass", "show", "mail box"], ["sh", "-c", "pass show mail"]])

    def test_toml_list_form_is_kept_as_argv(self):
        tmp = Path(self.enterContext(TemporaryDirectory()))
        (tmp / "c.toml").write_text('[accounts.l]\nbackend.type = "imap"\nbackend.host = "h"\n'
                                    'backend.auth.type = "password"\nbackend.auth.cmd = ["pass", "show", "a b"]\n')
        (acct,), _ = mi.load_accounts(tmp / "c.toml")
        self.assertEqual(acct._secret, ["pass", "show", "a b"])

    def test_failed_command_is_an_auth_error(self):
        with self.assertRaises(PermissionError):
            mi.Account(name="a", host="h", auth_kind="cmd", _secret=["false"]).password(
                run=lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))


class LogFileTests(unittest.TestCase):
    def setUp(self):
        self.tearDown()     # a gateway-start test may have attached the real file handler

    def tearDown(self):
        for h in list(mi.log.handlers):
            if isinstance(h, mi.logging.FileHandler):
                mi.log.removeHandler(h)
                h.close()

    def test_private_rotating_log_with_account_index(self):
        from logging.handlers import RotatingFileHandler
        path = Path(self.enterContext(TemporaryDirectory())) / "logs" / "push.log"
        old = os.umask(0o022)
        try:
            mi._setup_file_log(path)
        finally:
            os.umask(old)
        (h,) = [h for h in mi.log.handlers if isinstance(h, mi.logging.FileHandler)]
        self.assertIsInstance(h, RotatingFileHandler)
        self.assertEqual((h.maxBytes, h.backupCount), (1_000_000, 3))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        stop = RecStop(1)
        w = mi.Worker(mi.Account(name="private-name", host="h"), None,
                      mi.Status(path=path.parent / "hb.json"), stop, label="acct#2",
                      connect_fn=lambda a: (_ for _ in ()).throw(socket.timeout("x")))
        w.run()
        h.doRollover()
        mi.log.warning("after rotation")
        h.flush()
        text = (path.parent / "push.log.1").read_text()
        self.assertIn("acct#2", text)
        self.assertNotIn("private-name", text)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)   # the new file after rotation too

    def test_labels_follow_selected_order(self):
        self.assertEqual(mi.acct_label("b", ["a", "b"]), "acct#2")
        self.assertEqual(mi.acct_label("zz", ["a"]), "acct#?")


if __name__ == "__main__":
    unittest.main()
