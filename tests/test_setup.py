"""Hermetic coverage for the installer and the chat setup answer format."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import cli
import jobs

REAL_API_PROBLEM = jobs.api_problem
REAL_TOOLSETS_SETTING = jobs.cron_toolsets_setting


class FakeCron:
    """`hermes cron` and cron.plugin_api.ensure_named_job (the only public way to set enabled_toolsets)."""
    def __init__(self):
        self.calls = []
        self.live = {}
        self.toolsets = {}
        self.names = {}
        self.ensured = []
        self.states = {}
        self.executions = {}
        self.next_id = 0

    def ensure(self, name, *, prompt, schedule, deliver=None, enabled_toolsets=None, home=None):
        assert home == os.environ["HERMES_HOME"], home
        self.ensured.append(name)
        for jid, known in self.names.items():
            if known == name and jid in self.live:
                return {"id": jid, "name": name, "enabled_toolsets": self.toolsets.get(jid)}
        self.next_id += 1
        jid = f"{self.next_id:012x}"
        self.live[jid] = ["ensure", "--name", name, "--deliver", deliver, "--", schedule, prompt]
        self.toolsets[jid], self.names[jid] = enabled_toolsets, name
        return {"id": jid, "name": name, "enabled_toolsets": enabled_toolsets}

    def option(self, jid, flag):
        argv = self.live[jid]
        return argv[argv.index(flag) + 1] if flag in argv else None

    def job(self, name):
        """The live job id named NAME."""
        (jid,) = [jid for jid in self.live if self.names.get(jid) == name]
        return jid

    def schedule_of(self, jid):
        argv = self.live[jid]
        return self.option(jid, "--schedule") if argv[2:3] == ["edit"] else argv[argv.index("--") + 1]

    def prompt_of(self, jid):
        argv = self.live[jid]
        return self.option(jid, "--prompt") if argv[2:3] == ["edit"] else argv[-1]

    def __call__(self, argv, *, env, **kwargs):
        assert argv[:2] == ["hermes", "cron"], argv
        assert env["HERMES_HOME"] == os.environ["HERMES_HOME"], argv
        self.calls.append(argv)
        action = argv[2]
        if action == "list":
            assert argv[3:] == ["--all"], argv
            stdout = "Scheduled Jobs (profile: custom)\n\n" + "".join(
                f"  {jid} [{self.states.get(jid, 'active')}]\n    Name:      {self.names.get(jid, '(unnamed)')}\n"
                f"    Deliver:   local\n\n" for jid in self.live
            ) if self.live else "No scheduled jobs in profile 'custom'.\n"
        elif action == "runs":
            assert argv[4:] == ["--limit", "500"], argv
            jid = argv[3]
            stdout = "".join(f"{eid}  {state}  job={jid}  source=builtin  2026-11-26T12:00:00Z\n"
                             for eid, state in self.executions.get(jid, [])) or "No cron execution attempts recorded.\n"
        elif action == "create":
            self.next_id += 1
            jid = f"{self.next_id:012x}"
            self.live[jid] = argv
            self.names[jid] = argv[argv.index("--name") + 1]
            stdout = f"Created job: {jid}\n  Name: Proactive · fixture\n  ⚠  Scheduler is not ready.\n"
        elif action == "edit":
            jid = argv[3]
            self.live[jid] = argv   # an edit keeps enabled_toolsets (verified against Hermes)
            self.names[jid] = argv[argv.index("--name") + 1]
            stdout = f"Updated job: {jid}\n"
        elif action == "remove":
            jid = argv[3]
            del self.live[jid]
            self.states.pop(jid, None)
            stdout = f"Removed job: Proactive · fixture ({jid})\n"
        elif action in ("pause", "resume"):
            jid = argv[3]
            assert jid in self.live, argv
            self.states[jid] = "paused" if action == "pause" else "active"
            stdout = f"{action.capitalize()}d job: Proactive · fixture ({jid})\n"
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def real_name(name):
    """The name a job was created under, without setup's unique creation suffix."""
    return re.sub(r" \(setup [0-9a-f]+\)$", "", name)


class JobSetupTests(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"HERMES_HOME": home}, clear=True))
        self.home = Path(home)
        self.run = FakeCron()
        self.enterContext(patch.object(jobs, "_plugin_ensure", self.run.ensure))
        self.enterContext(patch.object(jobs, "api_problem", return_value=None))
        self.enterContext(patch.object(jobs, "cron_toolsets_setting", return_value=None))   # never read a real config
        self.enterContext(redirect_stdout(StringIO()))   # setup's progress lines; tests that read them capture their own
        self.cfg = cli.base_config("custom")
        self.cfg["timezone"] = "Europe/Berlin"
        self.cfg["features"].update({key: key == "health" for key in self.cfg["features"]})

    SCRIPTS = {"Proactive · health": "ea_health.py", "Proactive · tick": "desk_tick.py", "Proactive · say": "say_job.py"}

    def by_name(self):
        return {self.run.option(jid, "--name"): jid for jid in self.run.live}

    def test_first_apply_creates_local_script_jobs_then_second_apply_is_read_only(self):
        self.assertEqual([r["action"] for r in jobs.apply(self.cfg, run=self.run)].count("create"), 3)
        for name, jid in self.by_name().items():
            script = self.SCRIPTS[name]
            argv = self.run.live[jid]
            for flag, value in (("--deliver", "bot-chat" if script == "say_job.py" else "local"),
                                ("--failure-deliver", "local"), ("--timezone", "Europe/Berlin"),
                                ("--catch-up", "off"), ("--script", f"proactive-hermes/{script}")):
                self.assertEqual(argv[argv.index(flag) + 1], value)
            self.assertIn("--no-wrap-response", argv)
            self.assertIn("--no-agent", argv)
            self.assertTrue((self.home / "scripts" / "proactive-hermes" / script).is_file())
        self.assertEqual(set(self.by_name()), set(self.SCRIPTS))
        self.assertEqual(self.run.ensured, [])   # no-agent jobs have no toolsets: plain `hermes cron create`
        self.assertTrue((self.home / "plugin-data" / "proactive-hermes" / "jobs.json").is_file())
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        self.assertFalse([argv for argv in self.run.calls if argv[2] != "list"])
        self.assertEqual(len(self.run.live), 3)

    def test_failed_registry_write_recovers_fresh_say_by_staging_name(self):
        save = jobs._save_registry
        def fail_after_create(reg):
            if reg.get("creating", {}).get("say", {}).get("new"):
                raise OSError("injected write failure")
            save(reg)
        with patch.object(jobs, "_save_registry", side_effect=fail_after_create):
            with self.assertRaisesRegex(OSError, "injected write failure"):
                jobs.apply(self.cfg, run=self.run)
        self.assertNotIn("say", self.registry())
        staging = self.registry()["creating"]["say"]["temp"]
        staged = self.run.job(staging)
        self.assertEqual(self.run.option(staged, "--deliver"), "local")
        self.assertNotEqual(self.run.schedule_of(staged), "*/2 * * * *")
        jobs.apply(self.cfg, run=self.run)
        self.assertEqual(self.run.job("Proactive · say"), staged)
        self.assertEqual(self.registry()["say"]["id"], staged)
        self.assertNotIn("creating", self.registry())
        self.assertEqual(len([name for name in self.by_name() if real_name(name) == "Proactive · say"]), 1)

    def test_remove_all_cleans_journaled_fresh_creation_before_ownership(self):
        save = jobs._save_registry
        def fail_after_create(reg):
            if reg.get("creating", {}).get("say", {}).get("new"):
                raise OSError("injected write failure")
            save(reg)
        with patch.object(jobs, "_save_registry", side_effect=fail_after_create):
            with self.assertRaises(OSError):
                jobs.apply(self.cfg, run=self.run)
        self.assertEqual(jobs.remove_all(run=self.run), ["say"])
        self.assertEqual(self.run.live, {})
        self.assertEqual(self.registry(), {})

    def test_partial_history_is_replaced_and_copy_mismatch_preserves_old_job(self):
        _, old = self._old_radar()
        filename = "2026-11-20_08-00-00.md"
        target = self.kept(old, filename)
        target.parent.mkdir(parents=True)
        target.write_text("partial")
        def corrupt_copy(_src, dst):
            dst.write(b"wrong")
        with patch.object(jobs.shutil, "copyfileobj", side_effect=corrupt_copy):
            with self.assertRaisesRegex(RuntimeError, "copy mismatch"):
                jobs.apply(self.cfg, run=self.run)
        self.assertIn(old, self.run.live)
        self.assertEqual(self.registry()["mail_radar"]["id"], old)
        self.assertEqual(target.read_text(), "partial")
        self.assertEqual(list(target.parent.iterdir()), [target])
        jobs.apply(self.cfg, run=self.run)
        self.assertNotIn(old, self.run.live)
        self.assertEqual(target.read_text(), "## Response\n[SILENT]\n")

    def test_say_job_delivers_to_bot_chat_and_only_exists_when_something_speaks(self):
        jobs.apply(self.cfg, run=self.run)
        say = self.by_name()["Proactive · say"]
        self.assertEqual(self.run.option(say, "--deliver"), "bot-chat")
        self.assertEqual(self.run.option(say, "--failure-deliver"), "local")
        self.assertEqual(self.run.schedule_of(say), "*/2 * * * *")   # around the clock; quiet hours live in the script
        self.cfg["features"]["health"] = False
        jobs.apply(self.cfg, run=self.run)
        self.assertEqual(self.run.live, {})

    def test_agent_jobs_get_narrow_toolsets_through_ensure_named_job_then_edit(self):
        self.cfg["features"].update(mail_radar=True, promises=True, lookahead=True)
        self.cfg["mail"]["gmail_api"] = True
        jobs.apply(self.cfg, run=self.run)
        names = self.by_name()
        want = {"Proactive · mail radar": ["terminal", "file", "skills"],
                "Proactive · promise tracker": ["terminal", "file", "skills", "session_search"],
                "Proactive · look-ahead": ["terminal", "file", "skills", "web"]}
        for name, toolsets in want.items():
            jid = names[name]
            self.assertEqual(self.run.toolsets[jid], toolsets)
            self.assertNotIn("browser", self.run.toolsets[jid])
            # The edit right after ensure_named_job sets what ensure cannot: script, skills, timezone, wrap, catch-up.
            self.assertEqual(self.run.live[jid][2], "edit")
            self.assertEqual(self.run.option(jid, "--timezone"), "Europe/Berlin")
        self.assertEqual(self.run.option(names["Proactive · mail radar"], "--script"), "proactive-hermes/ea_radar_gate.py")
        self.assertCountEqual([real_name(n) for n in self.run.ensured], list(want))
        self.assertTrue(all(n != real_name(n) for n in self.run.ensured))   # created under a name nobody has
        for name in ("Proactive · health", "Proactive · tick", "Proactive · say"):
            self.assertIsNone(self.run.toolsets.get(names[name]))
        reg = json.loads((self.home / "plugin-data" / "proactive-hermes" / "jobs.json").read_text())
        self.assertEqual(reg["mail_radar"]["toolsets"], want["Proactive · mail radar"])

        # A prompt change edits in place: `hermes cron edit` keeps enabled_toolsets.
        self.cfg["owner"]["name"] = "Sam"
        self.run.calls.clear()
        self.run.ensured.clear()
        jobs.apply(self.cfg, run=self.run)
        self.assertEqual(self.by_name(), names)
        self.assertEqual(self.run.ensured, [])
        self.assertEqual(self.run.toolsets[names["Proactive · mail radar"]], want["Proactive · mail radar"])

    def kept(self, old, name):
        """A saved run of the replaced job OLD, as kept in plugin-data/proactive-hermes/job-history/."""
        return self.home / "plugin-data" / "proactive-hermes" / "job-history" / "mail_radar" / old / name

    def registry(self):
        return json.loads((self.home / "plugin-data" / "proactive-hermes" / "jobs.json").read_text())

    def _old_radar(self, state="active"):
        """A mail radar installed by a release before toolsets (2f7c535b): no toolsets recorded, one saved run."""
        self.cfg["features"]["mail_radar"] = True
        self.cfg["mail"]["gmail_api"] = True
        jobs.apply(self.cfg, run=self.run)
        path = self.home / "plugin-data" / "proactive-hermes" / "jobs.json"
        reg = json.loads(path.read_text())
        old = reg["mail_radar"]["id"]
        self.run.toolsets[old] = None
        self.run.states[old] = state
        del reg["mail_radar"]["toolsets"]
        path.write_text(json.dumps(reg))
        out = self.home / "cron" / "output" / old
        out.mkdir(parents=True)
        (out / "2026-11-20_08-00-00.md").write_text("## Response\n[SILENT]\n")
        return path, old

    def test_upgrade_without_recorded_toolsets_replaces_the_agent_job(self):
        path, old = self._old_radar()
        self.run.calls.clear()
        rows = {r["job"]: r["action"] for r in jobs.apply(self.cfg, run=self.run)}
        self.assertEqual(rows["Proactive · mail radar"], "replace")
        new = json.loads(path.read_text())["mail_radar"]["id"]
        self.assertNotEqual(new, old)
        self.assertNotIn(old, self.run.live)
        self.assertEqual(self.run.toolsets[new], ["terminal", "file", "skills"])
        self.assertEqual(self.run.states.get(new, "active"), "active")
        # The new job exists before the old one goes; the old one is paused during the swap so both never run.
        steps = [(a[2], a[3] if len(a) > 3 else "") for a in self.run.calls if a[2] != "list"]
        self.assertLess(steps.index(("pause", old)), steps.index(("edit", new)))
        self.assertLess(steps.index(("edit", new)), steps.index(("remove", old)))
        # The old job's saved runs are kept in plugin-data (`hermes cron remove` deletes the old folder), never
        # written into Hermes's own cron/output.
        self.assertEqual(self.kept(old, "2026-11-20_08-00-00.md").read_text(), "## Response\n[SILENT]\n")
        self.assertFalse((self.home / "cron" / "output" / new).exists())

    def test_replace_waits_for_a_late_history_writer_even_when_latest_attempt_was_refused(self):
        _, old = self._old_radar()
        late = self.home / "cron" / "output" / old / "last-run.md"
        self.run.executions[old] = [("b" * 32, "missed"), ("a" * 32, "running")]
        archive = self.kept(old, late.name)

        def finish():
            late.write_text("Latest completed job output")
            self.run.executions[old][-1] = ("a" * 32, "completed")

        def real_cli(argv, **kwargs):
            if argv[2:4] == ["remove", old]:
                if self.run.executions[old][-1][1] == "running":
                    # Reproduce the old race: the writer finishes after verification, just before removal.
                    finish()
                self.assertEqual(archive.read_text(), late.read_text())
                jobs.shutil.rmtree(late.parent)
            return self.run(argv, **kwargs)

        with patch.object(jobs.time, "sleep", side_effect=lambda _: finish()) as wait:
            jobs.apply(self.cfg, run=real_cli)
        wait.assert_called_once()
        self.assertFalse(late.exists())
        self.assertEqual(archive.read_text(), "Latest completed job output")

    def test_busy_swap_stays_paused_and_journaled_until_recovery_can_copy_the_late_run(self):
        _, old = self._old_radar()
        self.run.executions[old] = [("a" * 32, "claimed")]
        late = self.home / "cron" / "output" / old / "during-recovery.md"
        with patch.object(jobs, "IDLE_WAIT_S", 0):
            self.assertIn("deferred removal", self.settle())
            new = self.registry()["mail_radar"]["id"]
            self.assertNotEqual(old, new)
            self.assertEqual(self.registry()["replacing"]["mail_radar"]["old"], old)
            self.assertEqual(self.run.states[old], "paused")
            self.assertFalse(self.kept(old, "2026-11-20_08-00-00.md").exists())
            self.assertIn("deferred removal", self.settle())
            self.assertIn(old, self.run.live)
        late.write_text("late saved run")
        self.run.executions[old] = [("a" * 32, "completed")]
        self.assertIn("finished an interrupted job swap", self.settle())
        self.assertEqual(self.assert_swapped_cleanly(old, active=True), new)
        self.assertEqual(self.kept(old, late.name).read_text(), "late saved run")

    def test_a_paused_job_stays_paused_through_the_replace(self):
        path, old = self._old_radar(state="paused")
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        new = json.loads(path.read_text())["mail_radar"]["id"]
        self.assertEqual(self.run.states[new], "paused")
        steps = [(a[2], a[3] if len(a) > 3 else "") for a in self.run.calls if a[2] != "list"]
        self.assertNotIn(("pause", old), steps)            # already paused: left as it was until removed
        self.assertNotIn(("resume", old), steps)
        self.assertLess(steps.index(("pause", new)), steps.index(("edit", new)))   # paused before its real schedule
        self.assertTrue(self.kept(old, "2026-11-20_08-00-00.md").is_file())
        self.assertFalse((self.home / "cron" / "output" / new).exists())

    def test_a_failed_replace_leaves_the_old_job_as_it_was(self):
        path, old = self._old_radar()
        real_edit = self.run.__call__

        def failing(argv, **kwargs):
            if argv[2] == "edit" and argv[3] != old and argv[3] in self.run.names and \
                    self.run.names[argv[3]].endswith(")"):
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
            return real_edit(argv, **kwargs)
        with self.assertRaisesRegex(RuntimeError, "boom"):
            jobs.apply(self.cfg, run=failing)
        self.assertEqual(json.loads(path.read_text())["mail_radar"]["id"], old)
        self.assertEqual(self.run.states[old], "active")           # resumed
        self.assertEqual([j for j in self.run.live if self.run.names[j].startswith("Proactive · mail radar")], [old])
        self.assertTrue((self.home / "cron" / "output" / old / "2026-11-20_08-00-00.md").is_file())
        self.assertNotIn("replacing", self.registry())   # a swap that failed cleanly leaves no record behind

    # --- run history stays in plugin-data; Hermes's cron/output is only read ---------------------------------------

    def tree(self, root):
        return sorted((str(f.relative_to(root)), f.read_bytes()) for f in root.rglob("*") if f.is_file()) \
            if root.exists() else []

    def test_replace_copies_old_runs_to_plugin_data_and_never_writes_into_cron_output(self):
        path, old = self._old_radar()
        sub = self.home / "cron" / "output" / old / "extra"
        sub.mkdir()
        (sub / "2026-11-21_08-00-00.md").write_text("second run")
        (self.home / "cron" / "output" / old / "link.md").symlink_to(sub / "2026-11-21_08-00-00.md")
        before = self.tree(self.home / "cron")
        said = StringIO()
        with redirect_stdout(said):
            jobs.apply(self.cfg, run=self.run)
        new = self.registry()["mail_radar"]["id"]
        self.assertNotEqual(new, old)
        self.assertEqual(self.tree(self.home / "cron"), before)   # Hermes's own folder: not one byte written
        self.assertEqual(sorted(p.name for p in (self.home / "cron" / "output").iterdir()), [old])
        history = self.kept(old, "2026-11-20_08-00-00.md").parent
        self.assertEqual(self.tree(history), [("2026-11-20_08-00-00.md", b"## Response\n[SILENT]\n"),
                                              ("extra/2026-11-21_08-00-00.md", b"second run")])   # symlink skipped
        for folder in (history, history.parent, history.parent.parent, history / "extra"):
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700, folder)
        # Said once, with the place and the reason.
        lines = [ln for ln in said.getvalue().splitlines() if ln.startswith("Run history:")]
        self.assertEqual(len(lines), 1)
        self.assertIn("2 saved run file(s)", lines[0])
        self.assertIn(str(history.parent.parent), lines[0])
        self.assertIn("never writes into it", lines[0])
        # Nothing to say when nothing is replaced, and a second pass copies nothing and overwrites nothing.
        again = StringIO()
        with redirect_stdout(again):
            jobs.apply(self.cfg, run=self.run)
        self.assertNotIn("Run history", again.getvalue())
        self.assertEqual(jobs._keep_history("mail_radar", old), 0)

    def test_history_folder_names_are_safe_for_any_job_key(self):
        old = "0123456789ab"
        root = self.home / "plugin-data" / "proactive-hermes" / "job-history"
        self.assertEqual(jobs._history_dir("mail_radar", old), root / "mail_radar" / old)
        for key in ("watch:../../etc/x y", "watch:Drop/It", "watch:", ".."):
            folder = jobs._history_dir(key, old)
            self.assertEqual(folder.parent.parent, root, key)
            self.assertRegex(folder.parent.name, r"^[A-Za-z0-9_.-]+$")
        self.assertNotEqual(jobs._history_dir("watch:a b", old), jobs._history_dir("watch:a/b", old))

    def test_nothing_in_jobs_py_writes_under_cron_output(self):
        source = (Path(jobs.__file__)).read_text()
        self.assertEqual(re.findall(r"_output_dir\(([^)]*)\)", source), ["jid: str", "old"])   # its def, and the one read
        self.assertNotIn("output_dir(new", source)

    # --- an interrupted replace is finished or undone by the next run ------------------------------------------

    def interrupt_on(self, action, *, when):
        """A `hermes cron` runner that dies with KeyboardInterrupt (a Ctrl-C, no cleanup) on the first ACTION call for
        which WHEN(argv) is true; everything else goes to the fake."""
        real = self.run.__call__
        fired = []

        def run(argv, **kwargs):
            if argv[2] == action and when(argv) and not fired:
                fired.append(argv)
                raise KeyboardInterrupt
            return real(argv, **kwargs)
        run.fired = fired
        return run

    def setup_names(self):
        return [self.run.names[j] for j in self.run.live if "(setup " in self.run.names[j]]

    def settle(self, *, run=None):
        said = StringIO()
        with redirect_stdout(said):
            jobs.apply(self.cfg, run=run or self.run)
        return said.getvalue()

    def assert_swapped_cleanly(self, old, *, active):
        reg = self.registry()
        new = reg["mail_radar"]["id"]
        self.assertNotIn("replacing", reg)
        self.assertNotEqual(new, old)
        self.assertNotIn(old, self.run.live)
        self.assertEqual(self.run.toolsets[new], ["terminal", "file", "skills"])
        self.assertEqual(self.run.states.get(new, "active"), "active" if active else "paused")
        self.assertEqual(self.setup_names(), [])   # no leftover "(setup ...)" job
        self.assertEqual([self.run.names[j] for j in self.run.live if "mail radar" in self.run.names[j]],
                         ["Proactive · mail radar"])
        return new

    def test_interrupt_after_the_pause_then_rerun_leaves_an_active_radar_active(self):
        path, old = self._old_radar()   # active
        with patch.object(jobs, "_plugin_ensure", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            jobs.apply(self.cfg, run=self.run)
        # Left half swapped: the old job is paused, and the registry says why.
        self.assertEqual(self.run.states[old], "paused")
        reg = self.registry()
        self.assertEqual(reg["mail_radar"]["id"], old)
        self.assertEqual((reg["replacing"]["mail_radar"]["old"], reg["replacing"]["mail_radar"]["was_active"]),
                         (old, True))
        said = self.settle()
        self.assertIn("undid an interrupted job swap for mail_radar", said)
        new = self.assert_swapped_cleanly(old, active=True)   # not paused: the half-swapped state is not the truth
        self.assertEqual(self.run.states.get(new, "active"), "active")
        self.assertTrue(self.kept(old, "2026-11-20_08-00-00.md").is_file())
        self.run.calls.clear()
        self.settle()   # and now a no-op
        self.assertEqual([a[2] for a in self.run.calls], ["list"])

    def test_interrupt_while_the_new_job_is_half_made_removes_the_setup_temp_job(self):
        path, old = self._old_radar()
        dying = self.interrupt_on("edit", when=lambda argv: argv[3] != old)
        with self.assertRaises(KeyboardInterrupt):
            jobs.apply(self.cfg, run=dying)
        self.assertEqual(len(self.setup_names()), 1)               # the temp job is still there
        temp = self.registry()["replacing"]["mail_radar"]
        self.assertEqual(self.run.names[temp["new"]], temp["temp"])
        self.assertRegex(temp["temp"], r" \(setup [0-9a-f]+\)$")
        self.assertEqual(self.run.states[old], "paused")
        self.settle()
        self.assert_swapped_cleanly(old, active=True)

    def test_interrupt_after_the_registry_points_at_the_new_job_finishes_the_swap(self):
        path, old = self._old_radar()
        dying = self.interrupt_on("remove", when=lambda argv: argv[3] == old)
        with self.assertRaises(KeyboardInterrupt):
            jobs.apply(self.cfg, run=dying)
        reg = self.registry()
        new = reg["mail_radar"]["id"]
        self.assertNotEqual(new, old)
        self.assertIn(old, self.run.live)                          # old, paused, not yet removed
        self.assertEqual(reg["replacing"]["mail_radar"]["new"], new)
        said = self.settle()
        self.assertIn("finished an interrupted job swap for mail_radar", said)
        self.assertEqual(self.assert_swapped_cleanly(old, active=True), new)   # the new job is kept, the old one goes
        self.assertTrue(self.kept(old, "2026-11-20_08-00-00.md").is_file())

    def test_a_paused_radar_stays_paused_after_an_interrupted_replace(self):
        path, old = self._old_radar(state="paused")
        dying = self.interrupt_on("edit", when=lambda argv: argv[3] != old)
        with self.assertRaises(KeyboardInterrupt):
            jobs.apply(self.cfg, run=dying)
        self.assertFalse(self.registry()["replacing"]["mail_radar"]["was_active"])
        self.run.calls.clear()
        self.settle()
        self.assert_swapped_cleanly(old, active=False)
        self.assertNotIn(("resume", old), [(a[2], a[3]) for a in self.run.calls if len(a) > 3])

    def test_an_undo_that_cannot_resume_the_old_job_keeps_the_record_and_stops_setup(self):
        path, old = self._old_radar()
        with patch.object(jobs, "_plugin_ensure", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            jobs.apply(self.cfg, run=self.run)
        real = self.run.__call__
        broken = lambda argv, **kw: (subprocess.CompletedProcess(argv, 1, stdout="", stderr="no")  # noqa: E731
                                     if argv[2] == "resume" else real(argv, **kw))
        with self.assertLogs(jobs.logger, "WARNING") as logged, self.assertRaisesRegex(RuntimeError, "could not put it back"):
            jobs.apply(self.cfg, run=broken)
        self.assertIn("could not resume", logged.output[0])
        self.assertIn("mail_radar", self.registry()["replacing"])
        self.assertEqual(self.run.states[old], "paused")
        self.settle()   # cron works again: the record is still there and still right
        self.assert_swapped_cleanly(old, active=True)

    def _die_after_ensure_before_note(self):
        """Setup dies after ensure_named_job made the temp job but before the record learned its id (only `temp`)."""
        path, old = self._old_radar()
        real_ensure = self.run.ensure

        def ensure_then_die(name, **kwargs):
            real_ensure(name, **kwargs)
            raise KeyboardInterrupt
        with patch.object(jobs, "_plugin_ensure", ensure_then_die), self.assertRaises(KeyboardInterrupt):
            jobs.apply(self.cfg, run=self.run)
        record = self.registry()["replacing"]["mail_radar"]
        self.assertNotIn("new", record)
        self.assertEqual(self.setup_names(), [record["temp"]])   # the half-made job, known only by its temp name
        return old, record

    def test_an_interrupted_swap_with_only_the_temp_name_removes_that_job(self):
        old, record = self._die_after_ensure_before_note()
        temp_id = self.run.job(record["temp"])
        # A job of someone else with a similar name is never touched.
        self.run.live["abcdef123456"] = ["someone else's job"]
        self.run.names["abcdef123456"] = record["temp"] + " "   # even trailing whitespace is a different name
        said = self.settle()
        self.assertIn(f"the half-made job {temp_id} is removed", said)
        self.assertNotIn(temp_id, self.run.live)
        self.assertIn("abcdef123456", self.run.live)
        self.assertNotIn("may still exist", said)
        del self.run.live["abcdef123456"]
        self.assert_swapped_cleanly(old, active=True)

    def test_the_temp_record_stays_until_its_job_could_be_removed(self):
        old, record = self._die_after_ensure_before_note()
        temp_id = self.run.job(record["temp"])
        real = self.run.__call__
        broken = lambda argv, **kw: (subprocess.CompletedProcess(argv, 1, stdout="", stderr="busy")  # noqa: E731
                                     if argv[2] == "remove" and argv[3] == temp_id else real(argv, **kw))
        with self.assertLogs(jobs.logger, "WARNING"), self.assertRaisesRegex(RuntimeError, "could not put it back"):
            jobs.apply(self.cfg, run=broken)
        self.assertEqual(self.registry()["replacing"]["mail_radar"]["temp"], record["temp"])   # kept until done
        self.assertIn(temp_id, self.run.live)
        self.assertEqual(self.run.states[old], "paused")    # do not resume until the half-made job is gone
        self.settle()   # cron works again: the temp job goes and the swap completes
        self.assertNotIn(temp_id, self.run.live)
        self.assert_swapped_cleanly(old, active=True)

    def test_remove_all_also_removes_the_temp_named_job_of_an_interrupted_swap(self):
        old, record = self._die_after_ensure_before_note()
        jobs.remove_all(run=self.run)
        self.assertEqual(self.run.live, {})
        self.assertEqual(self.registry(), {})

    def test_job_names_reads_the_name_row_under_each_header(self):
        listing = ("Scheduled Jobs (profile: custom)\n\n  0123456789ab [active]\n    Name:      Proactive · say\n"
                   "    Schedule:  */2 * * * *\n\n  ba9876543210 [paused]\n    Name:      Proactive · x (setup 00ff)  \n"
                   "    Deliver:   local\n")
        run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=listing, stderr="")  # noqa: E731
        self.assertEqual(jobs._job_names(run), {"0123456789ab": "Proactive · say", "ba9876543210": "Proactive · x (setup 00ff)  "})
        listing += "\n  cccccccccccc [active]\n    Deliver:   local\n"
        with self.assertRaisesRegex(RuntimeError, "Could not read job names"):
            jobs._job_names(run)

    def test_remove_all_also_removes_the_jobs_of_an_interrupted_swap(self):
        path, old = self._old_radar()
        dying = self.interrupt_on("edit", when=lambda argv: argv[3] != old)
        with self.assertRaises(KeyboardInterrupt):
            jobs.apply(self.cfg, run=dying)
        self.run.live["abcdef123456"] = ["someone else's job"]
        self.run.names["abcdef123456"] = "Proactive · mail radar (setup of somebody else)"
        jobs.remove_all(run=self.run)
        self.assertEqual(set(self.run.live), {"abcdef123456"})
        self.assertEqual(self.registry(), {})

    # --- the toolset warning ----------------------------------------------------------------------------------

    def test_toolset_warning_is_one_line_naming_the_extra_toolsets(self):
        self.cfg["features"].update(mail_radar=True, promises=True, lookahead=True)
        self.cfg["mail"]["gmail_api"] = True
        line = jobs.toolset_warning(self.cfg, ["terminal", "file", "skills"])
        self.assertNotIn("\n", line)
        self.assertTrue(line.startswith("Warning:"))
        self.assertIn("platform_toolsets.cron = [terminal, file, skills]", line)
        self.assertIn("2 Proactive Hermes job(s) also ask for session_search, web", line)
        self.assertIn("agent.disabled_toolsets", line)
        # Nothing beyond the setting, no setting, or nothing to ask for: no warning.
        self.assertIsNone(jobs.toolset_warning(self.cfg, ["terminal", "file", "skills", "session_search", "web"]))
        self.assertIsNone(jobs.toolset_warning(self.cfg, None))
        self.cfg["features"].update({key: False for key in self.cfg["features"]})
        self.assertIsNone(jobs.toolset_warning(self.cfg, ["terminal"]))   # only no-agent jobs: no toolsets asked
        # A Slack setup asks for connections on every agent job.
        self.cfg["features"]["morning_brief"] = True
        self.cfg["slack"]["enabled"] = True
        self.assertIn("1 Proactive Hermes job(s) also ask for connections",
                      jobs.toolset_warning(self.cfg, ["terminal", "file", "skills"]))

    def test_setup_prints_the_toolset_warning_once_and_dry_run_prints_none(self):
        self.cfg["features"].update(promises=True, lookahead=True)
        self.cfg["mail"]["gmail_api"] = True
        with patch.object(jobs, "cron_toolsets_setting", return_value=["terminal", "file", "skills"]):
            said = StringIO()
            with redirect_stdout(said):
                jobs.apply(self.cfg, run=self.run)
            self.assertEqual(len([ln for ln in said.getvalue().splitlines() if ln.startswith("Warning:")]), 1)
            dry = StringIO()
            with redirect_stdout(dry):
                jobs.apply(self.cfg, dry_run=True, run=self.run)
            self.assertNotIn("Warning", dry.getvalue())
        quiet = StringIO()
        with redirect_stdout(quiet):
            jobs.apply(self.cfg, run=self.run)   # setUp's default: no setting readable
        self.assertNotIn("Warning", quiet.getvalue())

    def test_cron_toolsets_setting_reads_hermes_config_through_the_readonly_loader(self):
        def with_config(config=None, *, boom=False):
            module = types.ModuleType("hermes_cli.config")

            def load_config_readonly():
                if boom:
                    raise RuntimeError("unreadable")
                return config
            module.load_config_readonly = load_config_readonly
            return patch.dict(sys.modules, {"hermes_cli.config": module, "hermes_cli": types.ModuleType("hermes_cli")})
        read = REAL_TOOLSETS_SETTING
        with with_config({"platform_toolsets": {"cron": ["terminal", " file ", "", "web"]}}):
            self.assertEqual(read(), ["terminal", "file", "web"])
        for config in ({}, {"platform_toolsets": {}}, {"platform_toolsets": {"cron": []}},
                       {"platform_toolsets": {"cron": "terminal"}}, {"platform_toolsets": None}, None,
                       {"platform_toolsets": {"cron": ["hermes-cron"]}},
                       {"platform_toolsets": {"cron": ["terminal", "hermes-cli"]}},
                       # every other bundle Hermes's toolsets.py defines: no name comparison, no warning
                       {"platform_toolsets": {"cron": ["terminal", "debugging"]}},
                       {"platform_toolsets": {"cron": ["safe"]}}, {"platform_toolsets": {"cron": ["coding"]}},
                       {"platform_toolsets": {"cron": ["all"]}}, {"platform_toolsets": {"cron": ["file", "*"]}},
                       {"platform_toolsets": {"cron": ["hermes"]}}, {"platform_toolsets": {"cron": ["hermes-gateway"]}}):
            with self.subTest(config=config), with_config(config):
                self.assertIsNone(read())
        with with_config(boom=True):
            self.assertIsNone(read())
        # Plain toolsets stay comparable, so the warning still works for them.
        for name in ("terminal", "file", "web", "search", "browser", "skills", "session_search", "connections", "kanban"):
            self.assertFalse(jobs.toolset_bundle(name), name)

    # --- one writer of the registry at a time ------------------------------------------------------------------

    def hold_lock(self):
        """The registry lock held by another run (a separate open file, as another process would hold it)."""
        import fcntl
        path = self.home / "plugin-data" / "proactive-hermes" / "jobs.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    def test_a_second_setup_waits_then_refuses_clearly_and_changes_nothing(self):
        held = self.hold_lock()
        err = StringIO()
        with patch.object(jobs, "LOCK_WAIT_S", 0.5), redirect_stderr(err), \
                self.assertRaisesRegex(jobs.RegistryBusy, "still running for this profile.*Nothing was changed"):
            jobs.apply(self.cfg, run=self.run)
        self.assertIn("waiting for it to finish", err.getvalue())
        self.assertEqual(self.run.calls, [])                          # not one `hermes cron` call
        self.assertFalse((self.home / "plugin-data" / "proactive-hermes" / "jobs.json").exists())
        with patch.object(jobs, "LOCK_WAIT_S", 0.5), redirect_stderr(StringIO()), self.assertRaises(jobs.RegistryBusy):
            jobs.remove_all(run=self.run)
        held.close()
        jobs.apply(self.cfg, run=self.run)                            # free again: runs normally
        self.assertEqual(len(self.run.live), 3)

    def test_a_second_setup_waits_for_the_first_to_finish(self):
        import threading
        held = self.hold_lock()
        threading.Timer(0.6, held.close).start()
        err = StringIO()
        started = time.monotonic()
        with redirect_stderr(err):
            jobs.apply(self.cfg, run=self.run)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assertIn("waiting for it to finish", err.getvalue())
        self.assertEqual(len(self.run.live), 3)

    def test_watch_add_and_remove_refuse_before_saving_config_when_registry_is_busy(self):
        with self.hold_lock(), patch.object(jobs, "LOCK_WAIT_S", 0.1), \
                patch.object(cli, "write_config") as save, patch.object(jobs, "apply") as apply:
            for command in ("add", "remove"):
                with self.subTest(command=command), redirect_stderr(StringIO()) as err:
                    self.assertEqual(cli.handle_watch(types.SimpleNamespace(watch_command=command), jobs), 1)
                    self.assertIn("Nothing was changed", err.getvalue())
            save.assert_not_called()
            apply.assert_not_called()

    def test_a_dry_run_and_a_nested_run_do_not_wait_on_the_lock(self):
        with jobs.registry_lock():                                    # a watch change holds it and calls apply
            jobs.apply(self.cfg, run=self.run)
        held = self.hold_lock()
        with patch.object(jobs, "LOCK_WAIT_S", 0.1):
            jobs.apply(self.cfg, dry_run=True, run=self.run)          # read only: no lock
        held.close()

    def test_another_thread_cannot_enter_the_same_lock(self):
        from concurrent.futures import ThreadPoolExecutor

        def other():
            with jobs.registry_lock(wait_s=0.1):
                self.fail("a different thread entered while the test held the lock")

        with jobs.registry_lock(), redirect_stderr(StringIO()), ThreadPoolExecutor(max_workers=1) as workers:
            with self.assertRaisesRegex(jobs.RegistryBusy, "Nothing was changed"):
                workers.submit(other).result(timeout=2)

    def test_two_real_processes_never_apply_at_once(self):
        """A second process blocks on the flock while the first holds it, and goes on once it is released."""
        import textwrap
        lock = self.home / "plugin-data" / "proactive-hermes" / "jobs.lock"
        script = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path(jobs.HERE))!r})
            import jobs
            with jobs.registry_lock(wait_s=float(sys.argv[1])):
                print("got", time.time(), flush=True)
                time.sleep(float(sys.argv[2]))
        """)
        env = {**os.environ, "HERMES_HOME": str(self.home)}
        first = subprocess.Popen([sys.executable, "-c", script, "5", "1.0"], stdout=subprocess.PIPE, text=True, env=env)
        self.assertTrue(first.stdout.readline().startswith("got"))
        self.assertTrue(lock.exists())
        second = subprocess.run([sys.executable, "-c", script, "5", "0"], capture_output=True, text=True, env=env)
        first.wait(10)
        first.stdout.close()
        self.assertEqual(second.returncode, 0)
        self.assertIn("waiting for it to finish", second.stderr)
        refused = subprocess.Popen([sys.executable, "-c", script, "5", "2.0"], stdout=subprocess.PIPE, text=True, env=env)
        refused.stdout.readline()
        busy = subprocess.run([sys.executable, "-c", script, "0.3", "0"], capture_output=True, text=True, env=env)
        refused.wait(10)
        refused.stdout.close()
        self.assertNotEqual(busy.returncode, 0)
        self.assertIn("RegistryBusy", busy.stderr)
        self.assertIn("Nothing was changed", busy.stderr)

    # --- the manifest floor ------------------------------------------------------------------------------------

    def test_the_manifest_loads_on_the_last_release_and_preflight_is_the_gate(self):
        manifest = (Path(jobs.HERE) / "plugin.yaml").read_text()
        self.assertRegex(manifest, r'(?m)^requires_hermes: ">=0\.23\.4"$')   # v0.23.5 does not exist
        self.assertNotIn("0.23.5", manifest)
        self.assertIn("cron.plugin_api", jobs.NEEDS_HERMES)
        for needle in ("current main or canary", "the next release"):
            self.assertIn(needle, jobs.NEEDS_HERMES)
        readme = (Path(jobs.HERE) / "README.md").read_text()
        self.assertNotIn("0.23.5", readme)

    def test_unrecorded_job_with_the_same_name_is_never_adopted_or_touched(self):
        self.cfg["features"]["mail_radar"] = True
        self.cfg["mail"]["gmail_api"] = True
        self.run.live["abcdef123456"] = ["--name", "Proactive · mail radar"]
        self.run.names["abcdef123456"] = "Proactive · mail radar"
        self.run.toolsets["abcdef123456"] = ["terminal", "file", "skills"]   # even matching settings
        jobs.apply(self.cfg, run=self.run)
        self.assertEqual(self.run.live["abcdef123456"], ["--name", "Proactive · mail radar"])
        touched = {a[3] for a in self.run.calls if a[2] in ("edit", "remove", "pause", "resume")}
        self.assertNotIn("abcdef123456", touched)
        reg = json.loads((self.home / "plugin-data" / "proactive-hermes" / "jobs.json").read_text())
        self.assertNotEqual(reg["mail_radar"]["id"], "abcdef123456")
        jobs.remove_all(run=self.run)
        self.assertEqual(set(self.run.live), {"abcdef123456"})

    def test_ensure_returning_a_job_setup_did_not_create_is_refused(self):
        self.cfg["features"]["mail_radar"] = True
        self.cfg["mail"]["gmail_api"] = True
        foreign = lambda name, **kw: {"id": "abcdef123456", "name": "Proactive · mail radar",  # noqa: E731
                                      "enabled_toolsets": kw["enabled_toolsets"]}
        with patch.object(jobs, "_plugin_ensure", foreign), self.assertRaisesRegex(RuntimeError, "did not create"):
            jobs.apply(self.cfg, run=self.run)
        self.assertNotIn("abcdef123456", {a[3] for a in self.run.calls if len(a) > 3})

    def test_say_job_is_created_first(self):
        self.cfg["features"].update(mail_radar=True, morning_brief=True)
        self.cfg["mail"]["gmail_api"] = True
        order = []
        real_ensure = self.run.ensure
        with patch.object(jobs, "_plugin_ensure", lambda name, **kw: order.append(real_name(name)) or
                          real_ensure(name, **kw)):
            real_call = self.run.__call__

            def run(argv, **kw):
                if argv[2] == "create":
                    order.append(argv[argv.index("--name") + 1])
                return real_call(argv, **kw)
            jobs.apply(self.cfg, run=run)
        self.assertEqual(real_name(order[0]), "Proactive · say")

    def test_preflight_refuses_without_cron_plugin_api_and_changes_nothing(self):
        problem = "this Hermes has no cron.plugin_api (ModuleNotFoundError). Nothing was changed."
        with patch.object(jobs, "api_problem", return_value=problem), \
                self.assertRaisesRegex(jobs.IncompatibleHermes, "Nothing was changed"):
            jobs.apply(self.cfg, run=self.run)
        self.assertEqual(self.run.calls, [])
        self.assertFalse(list(self.home.iterdir()))
        # The real check: a bare python with no Hermes on its path cannot import cron.plugin_api.
        with patch.object(jobs.phconf, "agent_root", return_value=self.home / "no-hermes"), \
                patch.dict("sys.modules", {"cron": None, "cron.plugin_api": None}):
            self.assertRegex(REAL_API_PROBLEM() or "", "needs a Hermes build that includes `cron.plugin_api`")

    def test_cli_setup_refuses_before_writing_config(self):
        import argparse
        from contextlib import redirect_stderr
        from io import StringIO
        parser = argparse.ArgumentParser()
        cli.setup_parser(parser)
        answers = self.home.parent / f"{self.home.name}-answers.json"
        answers.write_text("{}")
        self.addCleanup(answers.unlink)
        err = StringIO()
        with patch.object(jobs, "api_problem", return_value="no cron.plugin_api. Nothing was changed."), \
                redirect_stderr(err):
            code = cli.handle(parser.parse_args(["setup", "--preset", "custom", "--answers", str(answers)]))
        self.assertEqual(code, 1)
        self.assertIn("Nothing was changed", err.getvalue())
        self.assertFalse(list(self.home.iterdir()))

    def test_toolsets_cover_what_each_prompt_needs(self):
        """Every agent job's toolsets carry what its prompt and the desk contract use: connections (Slack and
        Linear connectors, the resolve step's Slack search) whenever Slack, Linear or the daily call is set up."""
        cfg = cli.base_config("juan")
        cfg["mail"]["gmail_api"] = True
        cfg["calendar"]["enabled"] = True
        cfg["features"].update(lookahead=True, slack_sweep=True, work_recap=True)
        cfg["watches"] = [{"name": "Drop", "schedule": "*/30 * * * *", "check": "shop page", "when": "in stock",
                           "then": "tell", "urgent": False}]
        contract = (Path(jobs.HERE) / jobs.phconf.CONTRACT).read_text()
        needs = {"connections": re.compile(r"\bSlack\b|\bLinear\b"),
                 "session_search": re.compile(r"told you in chat"),
                 "web": re.compile(r"\bweb\b|organiser|timetable", re.I)}
        desired = jobs.JOBS + [jobs.watch_job(w) for w in cfg["watches"]]
        checked = 0
        for job in desired:
            toolsets = jobs.toolsets_for(job, cfg)
            if toolsets is None:
                continue
            checked += 1
            prompt = jobs.prompt_for(job, cfg)
            for toolset, pattern in needs.items():
                if pattern.search(prompt) and (toolset != "web" or job["key"] in ("lookahead",) or "watch" in job):
                    with self.subTest(job=job["key"], toolset=toolset):
                        self.assertIn(toolset, toolsets)
            self.assertTrue({"terminal", "file", "skills"} <= set(toolsets))
        self.assertGreater(checked, 9)
        self.assertRegex(contract, "connector")   # the contract points every job at the connector tools
        for key in ("morning_brief", "slack_sweep", "work_recap_after_daily", "work_recap_evening", "promises",
                    "meeting_prep", "weekly_review", "evening_brief", "mail_radar"):
            job = next(j for j in jobs.JOBS if j["key"] == key)
            self.assertIn("connections", jobs.toolsets_for(job, cfg), key)
        # Without Slack, Linear or the daily call no job asks for connectors.
        for flag in ("slack", "linear", "daily_call"):
            cfg[flag]["enabled"] = False
        self.assertFalse(any("connections" in (jobs.toolsets_for(j, cfg) or []) for j in desired))

    def test_disabled_feature_removes_managed_jobs_only(self):
        jobs.apply(self.cfg, run=self.run)
        managed = set(self.run.live)
        self.run.live["abcdef123456"] = ["unrelated job"]
        self.cfg["features"]["health"] = False
        rows = jobs.apply(self.cfg, run=self.run)
        self.assertEqual([row["action"] for row in rows].count("remove"), 3)
        self.assertEqual({a[3] for a in self.run.calls if a[2] == "remove"}, managed)
        self.assertEqual(jobs.remove_all(run=self.run), [])
        self.assertEqual(set(self.run.live), {"abcdef123456"})

    def test_schedule_and_timezone_change_edit_existing_jobs(self):
        jobs.apply(self.cfg, run=self.run)
        managed = set(self.run.live)
        self.cfg["schedule"]["health"] = "13 * * * *"
        self.cfg["timezone"] = "Asia/Tokyo"
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        edits = [argv for argv in self.run.calls if argv[2] == "edit"]
        self.assertEqual({argv[3] for argv in edits}, managed)
        self.assertTrue(any(argv[argv.index("--schedule") + 1] == "13 * * * *" for argv in edits))
        for argv in edits:
            self.assertEqual(argv[argv.index("--timezone") + 1], "Asia/Tokyo")
        self.assertEqual(set(self.run.live), managed)
        self.assertCountEqual(jobs.remove_all(run=self.run), ["health", "tick", "say"])
        self.assertEqual(self.run.live, {})

    def test_external_removal_recreates_only_missing_managed_job(self):
        jobs.apply(self.cfg, run=self.run)
        first, *rest = self.run.live
        del self.run.live[first]
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        self.assertEqual([a[2] for a in self.run.calls if a[2] != "list"], ["create", "pause", "edit", "resume"])
        for jid in rest:
            self.assertIn(jid, self.run.live)
        self.assertNotIn(first, self.run.live)

    def test_dry_run_and_legacy_registry_migration_keep_existing_ids(self):
        rows = jobs.apply(self.cfg, dry_run=True, run=self.run)
        self.assertEqual([row["action"] for row in rows].count("create"), 3)
        self.assertFalse(list(self.home.iterdir()))
        self.assertEqual(self.run.calls, [])

        jobs.apply(self.cfg, run=self.run)
        path = self.home / "plugin-data" / "proactive-hermes" / "jobs.json"
        ids = {key: entry["id"] for key, entry in json.loads(path.read_text()).items()}
        path.write_text(json.dumps(ids))
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        self.assertEqual([argv[2] for argv in self.run.calls], ["list", "edit", "edit", "edit"])
        self.assertEqual({key: entry["id"] for key, entry in json.loads(path.read_text()).items()}, ids)
        self.run.calls.clear()
        jobs.apply(self.cfg, run=self.run)
        self.assertEqual([argv[2] for argv in self.run.calls], ["list"])

    def test_parse_created_id(self):
        self.assertEqual(jobs._created_job_id("notice\nCreated job: 2ec52da04420\n  Name: x\n"), "2ec52da04420")
        for output in ("some warning mentions 2ec52da04420", "Created job: invalid-id\n",
                       "Created job: 2ec52da04420\nCreated job: dacd388d0e04\n"):
            with self.subTest(output=output), self.assertRaisesRegex(RuntimeError, "job ID"):
                jobs._created_job_id(output)


class SetupAnswersTests(unittest.TestCase):
    def test_flat_answers_parse_hours_lists_bools_and_timezone(self):
        cfg = cli.apply_answers(cli.base_config("juan"), {
            "owner.name": "Alex", "timezone": "Europe/Berlin", "quiet_hours": "22-7",
            "holidays": "DE, US,,", "slack.enabled": "y", "slack.channels": "#team, #product",
            "daily_call.enabled": "yes", "daily_call.timezone": "Europe/Berlin",
            "daily_call.poll_start": "10:30", "daily_call.poll_end": "12:00",
            "daily_call.channels": "#team", "mail.gmail_api": "y",
            "mail.himalaya_accounts": "none", "calendar.enabled": "n",
            "features": {"weekly_review": False},
        })
        self.assertEqual(cfg["owner"]["name"], "Alex")
        self.assertEqual(cfg["timezone"], "Europe/Berlin")
        self.assertEqual(cfg["quiet_hours"], {"start": 22, "end": 7})
        self.assertEqual(cfg["holidays"], ["DE", "US"])
        self.assertEqual(cfg["slack"]["channels"], ["#team", "#product"])
        self.assertEqual(cfg["daily_call"]["channels"], ["#team"])
        self.assertEqual(cfg["mail"]["himalaya_accounts"], [])
        self.assertIs(cfg["mail"]["gmail_api"], True)
        self.assertIs(cfg["calendar"]["enabled"], False)
        self.assertIs(cfg["features"]["slack_sweep"], True)
        self.assertIs(cfg["features"]["work_recap"], True)
        self.assertIs(cfg["features"]["weekly_review"], False)
        self.assertEqual(cfg["schedule"]["work_recap_after_daily"], "*/5 10-12 * * 1,2,3,4,5")

    def test_authority_answers_and_mobile_enabled(self):
        cfg = cli.apply_answers(cli.base_config("custom"), {
            "authority.auto": "calendar_blocks, reminders, alarms, leave_time, watch_actions",
            "mobile.enabled": "yes",
        })
        self.assertEqual(cfg["authority"]["auto"], [
            "calendar_blocks", "reminders", "alarms", "leave_time", "watch_actions",
        ])
        self.assertIs(cfg["reminders"]["enabled"], True)
        self.assertEqual(cli.apply_answers(cfg, {"authority.auto": "none"})["authority"]["auto"], [])

    def test_invalid_authority_answers_explain_bad_value(self):
        for answers in ({"authority.auto": "calendar_blocks, payments"},
                        {"authority.auto": ["payments"]},
                        {"authority": {"auto": ["payments"]}}):
            with self.subTest(answers=answers), self.assertRaisesRegex(ValueError, r"authority\.auto.*payments"):
                cli.apply_answers(cli.base_config("custom"), answers)

    def test_interview_retries_an_invalid_authority_answer(self):
        from io import StringIO
        from contextlib import redirect_stdout

        cfg = cli.base_config("custom")
        questions = [q for q in cli.QUESTIONS if q[0] in {"authority.auto", "mobile.enabled"}]
        output = StringIO()
        with patch.object(cli, "detect", return_value={}), patch.object(cli, "QUESTIONS", questions), \
                patch("builtins.input", side_effect=["payments", "reminders", "yes"]), redirect_stdout(output):
            cli.interview(cfg)
        self.assertIn("That did not work", output.getvalue())
        self.assertIn("payments", output.getvalue())
        self.assertEqual(cfg["authority"]["auto"], ["reminders"])
        self.assertIs(cfg["reminders"]["enabled"], True)

    def test_mobile_disabled_strips_phone_authority(self):
        cfg = cli.apply_answers(cli.base_config("juan"), {
            "mobile": {"enabled": False}, "reminders": {"enabled": True},
        })
        self.assertEqual(cfg["authority"]["auto"], ["calendar_blocks", "cleanup"])
        self.assertIs(cfg["reminders"]["enabled"], False)

    def test_juan_preset_authority(self):
        cfg = cli.base_config("juan")
        auto = ["calendar_blocks", "reminders", "alarms", "leave_time", "cleanup"]
        self.assertEqual(cfg["authority"]["auto"], auto)
        cfg = cli.apply_answers(cfg, {"mobile.enabled": True})
        self.assertEqual(cfg["authority"]["auto"], auto)

    def test_invalid_timezone_raises(self):
        for key in ("timezone", "daily_call.timezone"):
            with self.subTest(key=key), self.assertRaises(ZoneInfoNotFoundError):
                cli.apply_answers(cli.base_config("custom"), {key: "Invalid/Zone"})
