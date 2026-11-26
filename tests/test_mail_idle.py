"""Hermetic tests for runtime/mail_idle.py: fake clocks, fake IMAP, no network, no cron, no gateway."""
import json
import os
import socket
import stat
import sys
import threading
import time
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

REAL_PLUGIN_SETTINGS = mi.plugin_settings

SETTINGS = {
    "mail_1_address": "me@gmail.com",                 # auto host
    "mail_2_address": "work@example.org", "mail_2_host": "imap.example.org", "mail_2_port": 143,
    "mail_3_address": "nohost@example.net",           # unknown domain, no server: skipped
    "mail_4_address": "nopass@icloud.com",            # no app password: skipped
}
SECRETS = {"PROACTIVE_MAIL_1_PASSWORD": "s3cret-one", "PROACTIVE_MAIL_2_PASSWORD": "s3cret-two",
           "PROACTIVE_MAIL_3_PASSWORD": "s3cret-three"}


def fake_secret(name):
    return SECRETS.get(name, "")


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
        home = self.tmp / "profiles" / "example"
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps({"timezone": "Europe/Berlin",
                                           "mail": {"himalaya_accounts": ["icloud", {"account": "work"}]}}))
        state = self.tmp / "state"
        self.enterContext(patch.dict(os.environ, HERMES_HOME=str(home)))
        # The config and state paths are not settable from the environment: patch the functions.
        self.enterContext(patch.object(phconf, "config_path", return_value=self.config))
        self.enterContext(patch.object(phconf, "state_dir",
                                       side_effect=lambda: state.mkdir(mode=0o700, exist_ok=True) or state))
        self.enterContext(patch.object(phconf, "CFG", phconf.load()))
        self.enterContext(patch.object(phconf, "hermes_home", return_value=home))
        self.enterContext(patch.object(phconf, "secret", side_effect=fake_secret))
        self.enterContext(patch.object(mi, "plugin_settings", side_effect=lambda ctx=None: dict(SETTINGS)))


class ConfigTests(Base):
    def test_slots_auto_host_and_skips(self):
        accounts, skipped = mi.load_accounts()
        by = {a.name: a for a in accounts}
        self.assertEqual(set(by), {"acct#1", "acct#2"})
        self.assertEqual((by["acct#1"].host, by["acct#1"].port, by["acct#1"].tls), ("imap.gmail.com", 993, "tls"))
        self.assertEqual(by["acct#1"].login, "me@gmail.com")
        self.assertEqual(by["acct#1"].password(), "s3cret-one")
        self.assertEqual((by["acct#2"].host, by["acct#2"].port, by["acct#2"].tls), ("imap.example.org", 143, "start-tls"))
        self.assertEqual(skipped, {"acct#3": "unknown_host", "acct#4": "no_password"})
        # Nothing secret or personal in the reasons or in repr().
        text = json.dumps(skipped) + repr(accounts)
        for leak in ("s3cret", "me@gmail.com", "work@example.org"):
            self.assertNotIn(leak, text)

    def test_password_read_through_hermes_secret_with_slot_names(self):
        asked = []
        mi.load_accounts(dict(SETTINGS), secret=lambda n: asked.append(n) or "pw")
        self.assertEqual(asked, ["PROACTIVE_MAIL_1_PASSWORD", "PROACTIVE_MAIL_2_PASSWORD", "PROACTIVE_MAIL_4_PASSWORD"])
        mi.load_accounts()
        self.assertEqual([c.args[0] for c in phconf.secret.call_args_list],
                         ["PROACTIVE_MAIL_1_PASSWORD", "PROACTIVE_MAIL_2_PASSWORD", "PROACTIVE_MAIL_4_PASSWORD"])

    def test_auto_host_table(self):
        cases = {"a@gmail.com": "imap.gmail.com", "a@googlemail.com": "imap.gmail.com",
                 "a@icloud.com": "imap.mail.me.com", "a@me.com": "imap.mail.me.com", "a@mac.com": "imap.mail.me.com",
                 "a@outlook.com": None, "a@hotmail.co.uk": None,      # Microsoft: no app passwords, never autofilled
                 "a@live.com": None, "a@fastmail.com": "imap.fastmail.com",
                 "a@yahoo.com": "imap.mail.yahoo.com", "a@Yahoo.co.uk": "imap.mail.yahoo.com",
                 "a@yahoo.co.jp": None, "a@example.org": None, "a@deliver.example": None, "nobody": None}
        for address, host in cases.items():
            self.assertEqual(mi.auto_host(address), host, address)

    def test_unknown_host_needs_server_and_ports(self):
        acc, skipped = mi.load_accounts({"mail_1_address": "x@corp.example"}, secret=lambda n: "pw")
        self.assertEqual((acc, skipped), ([], {"acct#1": "unknown_host"}))
        (acc,), _ = mi.load_accounts({"mail_1_address": "x@corp.example", "mail_1_host": "mail.corp.example"},
                                     secret=lambda n: "pw")
        self.assertEqual((acc.host, acc.port, acc.slot), ("mail.corp.example", 993, 1))
        _, skipped = mi.load_accounts({"mail_1_address": "x@gmail.com", "mail_1_port": "abc"}, secret=lambda n: "pw")
        self.assertEqual(skipped, {"acct#1": "bad_port"})
        _, skipped = mi.load_accounts({"mail_1_address": "not an address"}, secret=lambda n: "pw")
        self.assertEqual(skipped, {"acct#1": "bad_address"})

    def test_microsoft_mailboxes_are_skipped_without_asking_for_a_password(self):
        asked = []
        settings = {"mail_1_address": "a@outlook.com", "mail_2_address": "b@Hotmail.co.uk",
                    "mail_3_address": "c@live.com", "mail_4_address": "d@live.nl",
                    # Microsoft 365 on a company domain: the owner fills in Microsoft's server
                    "mail_5_address": "e@corp.example", "mail_5_host": "outlook.office365.com",
                    "mail_6_address": "f@corp.example", "mail_6_host": "imap-mail.outlook.com"}
        accounts, skipped = mi.load_accounts(settings, secret=lambda n: asked.append(n) or "pw")
        self.assertEqual(accounts, [])
        self.assertEqual(skipped, {f"acct#{n}": "microsoft" for n in range(1, 7)})
        self.assertEqual(asked, [])                                     # a password would not help
        self.assertEqual(mi.REASONS["microsoft"], "Microsoft allows only its own sign-in, no app passwords")
        self.assertIsNone(mi.auto_host("a@outlook.com"))
        # Another mail server on an odd domain, or a company domain that merely starts with "live.", is not blocked
        # once the owner names the server.
        (acc,), skipped = mi.load_accounts({"mail_1_address": "me@live.corp.example", "mail_1_host": "mail.corp.example"},
                                           secret=lambda n: "pw")
        self.assertEqual((acc.host, skipped), ("mail.corp.example", {}))
        self.assertFalse(mi.microsoft_only("me@gmail.com"))
        self.assertFalse(mi.microsoft_only("me@yahoo.com"))
        self.assertFalse(mi.microsoft_only("nobody"))
        self.assertFalse(mi.microsoft_only("x@corp.example", "notoutlook.com"))

    def test_empty_slots_are_ignored_and_secret_errors_skip(self):
        self.assertEqual(mi.load_accounts({}, secret=lambda n: "pw"), ([], {}))
        def refuse(name):
            raise RuntimeError("no scope")
        self.assertEqual(mi.load_accounts({"mail_6_address": "a@gmail.com"}, secret=refuse),
                         ([], {"acct#6": "secret_unavailable"}))

    def test_settings_read_through_ctx_get_config(self):
        ctx = SimpleNamespace(get_config=lambda key, default=None: {"mail_2_address": "b@me.com"}.get(key, default))
        out = REAL_PLUGIN_SETTINGS(ctx)       # Base patches mi.plugin_settings; this is the real one
        self.assertEqual(out["mail_2_address"], "b@me.com")
        self.assertEqual(set(out), set(mi.settings_keys()))
        self.assertNotIn("mail_2_password", out)       # the password never comes from settings

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

    def test_pokes_only_through_the_public_cron_run(self):
        self.write_registry({"id": "abcdef012345"})
        seen = []
        live = lambda j: {"id": j, "state": "active"}  # noqa: E731
        self.assertTrue(mi.poke_radar(cli=lambda j: seen.append(j) or True, job_state=live))
        self.assertIsNone(mi.poke_radar(cli=lambda j: None, job_state=live))   # busy: the debouncer retries
        self.assertEqual(seen, ["abcdef012345"])

    def test_no_job_no_poke_and_dry_run(self):
        self.assertFalse(mi.poke_radar(cli=lambda j: self.fail("poked")))
        self.write_registry({"id": "abcdef012345"})
        self.assertTrue(mi.poke_radar(dry_run=True, cli=lambda j: self.fail("dry"),
                                      job_state=lambda j: {"id": j, "state": "active"}))

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
        self.unload = []

    def on_unload(self, cb):
        self.unload.append(cb)


class GatewayStartTests(Base):
    def tearDown(self):
        mi.stop_service()

    def test_cli_load_never_starts(self):
        ctx = FakeCtx()
        self.assertEqual(mi.start_in_gateway(ctx, gateway=False), "not the gateway")
        self.assertIsNone(mi._SERVICE)

    def test_not_set_up_or_disabled_never_starts(self):
        ctx = FakeCtx()
        self.config.unlink()
        self.assertEqual(mi.start_in_gateway(ctx, gateway=True), "not set up")
        self.config.write_text("{}")
        with patch.object(mi, "plugin_settings", return_value={}):
            self.assertEqual(mi.start_in_gateway(ctx, gateway=True), "no mailboxes")   # no slot filled
        cfg = phconf.load()
        cfg["features"]["mail_push"] = False
        with patch.object(phconf, "CFG", cfg):
            self.assertEqual(mi.start_in_gateway(ctx, gateway=True), "disabled")
        self.assertIsNone(mi._SERVICE)

    def test_passwords_are_read_at_start_not_in_the_imap_threads(self):
        ctx = FakeCtx()
        with patch.object(mi.Service, "run_forever"):
            self.assertEqual(mi.start_in_gateway(ctx, gateway=True), "started")
        reads = phconf.secret.call_count
        self.assertEqual(reads, 3)                     # slots 1, 2 and 4 (3 has no server)
        svc = mi._SERVICE
        self.assertEqual([w.acct.name for w in svc.workers], ["acct#1", "acct#2"])
        self.assertEqual([w.label for w in svc.workers], ["acct#1", "acct#2"])
        self.assertEqual(svc.status.accounts["acct#3"]["last_error_class"], "unknown_host")

    def test_runs_on_its_own_daemon_thread_in_the_loading_context_once(self):
        import contextvars
        ctx = FakeCtx()
        marker = contextvars.ContextVar("marker", default="lost")
        seen = []
        marker.set("profile scope")
        with patch.object(mi.Service, "run_forever", lambda svc: seen.append((marker.get(), threading.current_thread()))):
            first = mi.start_in_gateway(ctx, gateway=True)
            second = mi.start_in_gateway(ctx, gateway=True)
            mi._SERVICE.thread.join(5)
        self.assertEqual((first, second), ("started", "already running"))
        self.assertEqual(ctx.unload, [mi.stop_service])
        (value, thread), = seen
        self.assertEqual(value, "profile scope")            # contextvars.copy_context().run
        self.assertEqual(thread.name, mi.TASK_NAME)
        self.assertTrue(thread.daemon)
        self.assertNotIn("_gateway_runner_ref", Path(mi.__file__).read_text())

    def test_unload_stops_the_service_thread(self):
        ctx = FakeCtx()

        def blocking_idle(conn, seconds, on_resp):
            mi._SERVICE.stop.wait(5) if mi._SERVICE else None

        with patch.object(mi, "connect", lambda a: FakeConn()), patch.object(mi, "idle_once", blocking_idle):
            self.assertEqual(mi.start_in_gateway(ctx, gateway=True), "started")
            svc = mi._SERVICE
            time.sleep(0.2)
            ctx.unload[0]()
            svc.thread.join(10)
        self.assertFalse(svc.thread.is_alive())
        self.assertTrue(svc.stop.is_set())

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


class DoctorTests(Base):
    def test_instant_push_and_15_min_reasons_with_next_steps(self):
        now = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
        hb = self.tmp / "hb.json"
        hb.write_text(json.dumps({"updated": now.isoformat(), "accounts": {
            "acct#1": {"state": "idle"}, "acct#2": {"state": "error", "last_error_class": "AuthError"}}}))
        cfg = phconf.load()
        cfg["mail"]["gmail_api"] = True
        rep = mi.push_report(cfg, now=now, path=hb)
        self.assertEqual(rep["Mailbox 1 (gmail.com)"], {"mode": "instant push"})
        two = rep["Mailbox 2 (example.org)"]
        self.assertEqual((two["mode"], two["reason"]), ("15 min checks", "login refused"))
        self.assertIn("app password", two["next"])
        three = rep["Mailbox 3 (example.net)"]
        self.assertEqual(three["reason"], "unknown mail server for this address")
        self.assertIn("Mailbox 3 IMAP server", three["next"])
        four = rep["Mailbox 4 (icloud.com)"]
        self.assertEqual(four["reason"], "no app password")
        self.assertIn("hermes plugins config proactive-hermes", four["next"])
        for other in ("Gmail API", "himalaya account icloud", "himalaya account work"):
            self.assertEqual(rep[other]["mode"], "15 min checks")
            self.assertIn("Mailbox 5", rep[other]["next"])          # the first free slot
        stale = mi.push_report(cfg, now=now + timedelta(minutes=11), path=hb)
        self.assertEqual(stale["Mailbox 1 (gmail.com)"]["reason"], "push heartbeat is stale")
        missing = mi.push_report(cfg, now=now, path=self.tmp / "none.json")
        self.assertEqual(missing["Mailbox 1 (gmail.com)"]["reason"], "push is not running")
        # Never an address or a password in the report.
        text = json.dumps(rep) + json.dumps(stale)
        for leak in ("s3cret", "me@", "work@", "nohost@", "nopass@"):
            self.assertNotIn(leak, text)

    def test_microsoft_mailbox_says_why_and_stays_on_15_min_checks(self):
        now = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
        hb = self.tmp / "hb.json"
        hb.write_text(json.dumps({"updated": now.isoformat(), "accounts": {"acct#1": {"state": "idle"}}}))
        settings = {"mail_1_address": "me@gmail.com", "mail_5_address": "me@outlook.com",
                    "mail_6_address": "me@hotmail.co.uk", "mail_6_host": ""}
        rep = mi.push_report(phconf.load(), now=now, path=hb, settings=settings, secret=fake_secret)
        for key in ("Mailbox 5 (outlook.com)", "Mailbox 6 (hotmail.co.uk)"):
            row = rep[key]
            self.assertEqual(row["mode"], "15 min checks")
            self.assertEqual(row["reason"], "Microsoft allows only its own sign-in, no app passwords")
            self.assertEqual(row["next"], "Nothing to do: this mailbox stays on 15 min checks.")
            # No "make a new password" loop, no restart or settings errand.
            for loop in ("refused", "new one", "app password in", "restart", "Restart", "Mailbox 5 IMAP", "Mailbox 6 IMAP"):
                self.assertNotIn(loop, row["next"] + row["reason"])
        self.assertEqual(rep["Mailbox 1 (gmail.com)"], {"mode": "instant push"})
        # The same row when the radar runs every 10 minutes, and when the heartbeat is missing or stale.
        cfg = phconf.load()
        cfg["schedule"]["mail_radar"] = "*/10 7-22 * * *"
        rep10 = mi.push_report(cfg, path=self.tmp / "none.json", settings=settings, secret=fake_secret)
        self.assertEqual(rep10["Mailbox 5 (outlook.com)"]["next"], "Nothing to do: this mailbox stays on 10 min checks.")
        self.assertEqual(rep10["Mailbox 5 (outlook.com)"]["reason"], mi.REASONS["microsoft"])
        self.assertNotIn("me@", json.dumps(rep) + json.dumps(rep10))

    def test_slot_filled_after_start_says_restart(self):
        now = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
        hb = self.tmp / "hb.json"
        hb.write_text(json.dumps({"updated": now.isoformat(), "accounts": {"acct#1": {"state": "idle"}}}))
        rep = mi.push_report(phconf.load(), now=now, path=hb)
        self.assertEqual(rep["Mailbox 2 (example.org)"]["reason"], "settings changed after Hermes started")
        self.assertIn("gateway restart", rep["Mailbox 2 (example.org)"]["next"])

    def test_feature_off_and_radar_interval(self):
        cfg = phconf.load()
        cfg["features"]["mail_push"] = False
        cfg["schedule"]["mail_radar"] = "*/10 7-22 * * *"
        rep = mi.push_report(cfg, path=self.tmp / "none.json")
        self.assertEqual(rep["Mailbox 1 (gmail.com)"]["mode"], "10 min checks")
        self.assertEqual(rep["Mailbox 1 (gmail.com)"]["reason"], "mail push is off in features")

    def test_doctor_report_has_mail_push(self):
        import ph_doctor
        with patch.object(ph_doctor, "check_gateway", return_value=(True, "running")), \
             patch.object(ph_doctor, "check_bot_chat", return_value=(True, "found")), \
             patch.object(ph_doctor, "check_mobile", return_value=(True, "")):
            rep = ph_doctor.report()
        self.assertIn("Mailbox 1 (gmail.com)", rep["mail_push"])
        self.assertIn("himalaya account icloud", rep["mail_push"])
        self.assertTrue(all(v["mode"] in ("instant push", "15 min checks") for v in rep["mail_push"].values()))
        self.assertTrue(all("next" in v for v in rep["mail_push"].values() if v["mode"] != "instant push"))
        self.assertNotIn("s3cret", json.dumps(rep))


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
                    {"id": self.JID, "state": "paused"}, {"id": self.JID, "state": "disabled"},
                    {"id": self.JID, "state": "completed"}):
            with self.assertLogs(mi.log, "INFO") as cm:
                self.assertFalse(mi.poke_radar(job_state=lambda j, job=job: job, cli=self.fail_poke))
            self.assertIn("radar paused, poke skipped", "\n".join(cm.output))

    def test_unknown_or_missing_job_is_not_poked(self):
        self.assertFalse(mi.poke_radar(job_state=lambda j: None, cli=self.fail_poke))
        self.assertFalse(mi.poke_radar(job_state=lambda j: {}, cli=self.fail_poke))

    def list_output(self, badge):
        out = f"Scheduled Jobs (profile: x)\n\n  {self.JID} [{badge}]\n    Name:      Proactive · mail radar\n"
        return lambda argv, **k: SimpleNamespace(returncode=0, stdout=out, stderr="")

    def test_reads_the_radar_state_from_the_public_cron_list(self):
        calls = []

        def run(argv, **k):
            calls.append((argv[1:], k["env"]["HERMES_HOME"]))
            return self.list_output("paused")(argv, **k)

        self.assertEqual(mi.radar_job_state(self.JID, run=run), {"id": self.JID, "state": "paused"})
        self.assertEqual(calls, [(["cron", "list", "--all"], str(phconf.hermes_home()))])
        self.assertTrue(mi.job_paused(mi.radar_job_state(self.JID, run=self.list_output("paused"))))
        self.assertFalse(mi.job_paused(mi.radar_job_state(self.JID, run=self.list_output("active"))))
        self.assertEqual(mi.radar_job_state("0" * 12, run=self.list_output("active")), {})
        none = lambda argv, **k: SimpleNamespace(returncode=0, stdout="No scheduled jobs in profile 'x'.\n", stderr="")  # noqa: E731
        self.assertEqual(mi.radar_job_state(self.JID, run=none), {})
        broken = lambda argv, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom")  # noqa: E731
        self.assertIsNone(mi.radar_job_state(self.JID, run=broken))
        garbled = lambda argv, **k: SimpleNamespace(returncode=0, stdout="??", stderr="")  # noqa: E731
        self.assertIsNone(mi.radar_job_state(self.JID, run=garbled))

    def test_never_imports_private_cron_functions(self):
        source = Path(mi.__file__).read_text()
        for name in ("get_job", "trigger_job", "cron.jobs"):
            self.assertNotIn(name, source)


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

    def test_labels_are_slot_numbers(self):
        self.assertEqual(mi.slot_label(2), "acct#2")
        self.assertEqual(mi.password_env(6), "PROACTIVE_MAIL_6_PASSWORD")


if __name__ == "__main__":
    unittest.main()
