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
        self.assertTrue(mi.poke_radar(in_process=lambda j: seen.append(("in", j)) or True,
                                      cli=lambda j: self.fail("cli used")))
        self.assertTrue(mi.poke_radar(in_process=lambda j: None, cli=lambda j: seen.append(("cli", j)) or True))
        self.assertEqual(seen, [("in", "abcdef012345"), ("cli", "abcdef012345")])

    def test_no_job_no_poke_and_dry_run(self):
        self.assertFalse(mi.poke_radar(in_process=lambda j: self.fail("poked"), cli=lambda j: self.fail("poked")))
        self.write_registry({"id": "abcdef012345"})
        self.assertTrue(mi.poke_radar(dry_run=True, in_process=lambda j: self.fail("dry"), cli=lambda j: self.fail("dry")))

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


if __name__ == "__main__":
    unittest.main()
