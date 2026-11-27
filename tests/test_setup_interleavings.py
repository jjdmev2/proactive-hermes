"""Seeded state-machine and interruption checks using the installer's public cron fake."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from io import StringIO
import os
import random
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import cli
import jobs
from tests.test_setup import FakeCron

SEED = 0x8FECE
STEPS = 360
BAD_SCHEDULE = 'not a schedule'
WATCHES = ('Coffee beans', 'Card drop', 'Spare parts')


class FaultCron(FakeCron):
    def __init__(self):
        super().__init__()
        self.reset_faults()

    def reset_faults(self, cut=None, when='before'):
        self.trace = []
        self.cut = cut
        self.when = when
        self.hit = None
        self.rejections = 0

    def invoke(self, action, call):
        self.trace.append(action)
        here = len(self.trace) == self.cut
        if here and self.when == 'before':
            self.hit = action
            raise KeyboardInterrupt('injected before ' + action)
        result = call()
        if here and self.when == 'after':
            self.hit = action
            raise KeyboardInterrupt('injected after ' + action)
        return result

    def __call__(self, argv, **kwargs):
        def command():
            if argv[2] == 'edit' and argv[argv.index('--schedule') + 1] == BAD_SCHEDULE:
                self.rejections += 1
                return subprocess.CompletedProcess(argv, 1, '', 'fixture rejected schedule')
            return super(FaultCron, self).__call__(argv, **kwargs)
        return self.invoke(argv[2], command)

    def ensure(self, name, **kwargs):
        return self.invoke('ensure', lambda: super(FaultCron, self).ensure(name, **kwargs))


class FenceInterleavings(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(jobs, 'api_problem', return_value=None))
        self.enterContext(patch.object(jobs, 'cron_toolsets_setting', return_value=None))
        self.enterContext(patch.object(jobs, 'install_runtime'))
        self.enterContext(patch.object(jobs, 'IDLE_WAIT_S', 0))
        self.enterContext(redirect_stdout(StringIO()))
        self.report = sys.stderr
        self.enterContext(redirect_stderr(StringIO()))
        self.save = jobs._save_registry
        self.archive = jobs._keep_history
        self.apply_jobs = jobs.apply
        self.parser = argparse.ArgumentParser()
        cli.setup_parser(self.parser)

    def initial_config(self):
        cfg = cli.base_config('custom')
        cfg['features'] = dict.fromkeys(cfg['features'], False)
        return cfg

    def reset_world(self):
        self.host = FaultCron()
        self.known = {}
        self.unrelated = 'abcdef123456'
        self.host.live[self.unrelated] = ['unrelated job']
        return self.initial_config()

    def wanted(self, cfg):
        return {job['key']: jobs._job_fields(cfg, job)
                for job in [*jobs.JOBS, *(jobs.watch_job(watch) for watch in cfg['watches'])]
                if job['key'] in jobs.ph_jobs.expected(cfg)}

    def check_job(self, jid, fields):
        self.assertEqual(self.host.names[jid], fields['name'])
        self.assertEqual(self.host.schedule_of(jid), fields['schedule'])
        self.assertEqual(self.host.prompt_of(jid), fields['prompt'])
        self.assertEqual(self.host.option(jid, '--deliver'), fields['deliver'])
        self.assertEqual(self.host.toolsets.get(jid), fields.get('enabled_toolsets'))

    def assert_invariant(self, *, removed=False):
        config = cli.phconf.load()
        wanted = {} if removed else self.wanted(config)
        self.known.update((spec['spec'], fields) for fields, spec in wanted.values())
        reg = jobs._load_registry()
        authority = {key: row for key, row in reg.items() if key not in ('creating', 'replacing')}
        for row in authority.values():
            jid = row['id']
            self.assertIn(jid, self.host.live)
            self.assertEqual(row.get('toolsets'), self.host.toolsets.get(jid))
            if 'spec' in row:
                self.check_job(jid, self.known[row['spec']])
        active = {jid for jid in self.host.live if self.host.states.get(jid, 'active') == 'active'} - {self.unrelated}
        self.assertTrue(active <= {row['id'] for key, row in authority.items() if key in wanted}, (active, authority))
        for key, row in authority.items():
            if row['id'] in active:
                fields, spec = wanted[key]
                self.check_job(row['id'], fields)
                self.assertEqual(row, {'id': row['id'], **spec})
        self.assertEqual(self.host.live[self.unrelated], ['unrelated job'])
        self.assertNotIn(self.unrelated, self.host.states)

    def apply_config(self, cfg, *, publish=True):
        self.known.update((spec['spec'], fields) for fields, spec in self.wanted(cfg).values())
        self.apply_jobs(cfg, run=self.host, ensure=self.host.ensure,
                        publish=(lambda: cli.write_config(cfg)) if publish else None)

    def watch(self, name, *, add):
        args = ['watch', 'add' if add else 'remove', '--name', name]
        if add:
            args += ['--schedule', '*/30 * * * *', '--check', 'Read a synthetic listing',
                     '--when', 'Stock is available', '--then', 'tell']
        # Drive the real CLI validation, config publication and exit-code handling.
        with patch.object(jobs, 'apply', side_effect=lambda cfg, **kw: self.apply_jobs(
                cfg, run=self.host, ensure=self.host.ensure, **kw)):
            return cli.handle(self.parser.parse_args(args))

    def test_seeded_config_changes_and_failures(self):
        self.enterContext(temporary_home())
        self.reset_world()
        rng = random.Random(SEED)
        exercised = Counter()
        for step in range(STEPS):
            cfg = cli.phconf.load() if cli.phconf.config_path().exists() else self.initial_config()
            operation = rng.choice(('feature', 'feature', 'widen', 'narrow', 'typo', 'fix', 'prompt',
                                    'add', 'remove', 'recovery', 'uninstall'))
            fault = rng.choice(('none', 'none', 'interrupt', 'archive', 'busy', 'registry'))
            target = rng.choice(WATCHES)
            removed = operation == 'uninstall'
            self.host.reset_faults(rng.randint(1, 24) if fault == 'interrupt' else None,
                                   rng.choice(('before', 'after')))
            if operation == 'feature':
                key = rng.choice(('health', 'mail_radar', 'promises'))
                cfg['features'][key] = not cfg['features'][key]
            elif operation in ('widen', 'narrow'):
                cfg['slack']['enabled'] = operation == 'widen'
            elif operation in ('typo', 'fix'):
                cfg['features']['mail_radar'] = True
                cfg['schedule']['mail_radar'] = BAD_SCHEDULE if operation == 'typo' else '*/17 * * * *'
            elif operation == 'prompt':
                cfg['owner']['name'] = f'Fixture {step}'
            busy = rng.choice(sorted(set(self.host.live) - {self.unrelated}) or ['none'])
            self.host.executions = {busy: [('1' * 32, 'running')]} if fault == 'busy' else {}
            write_count = 0
            reject_write = rng.randint(1, 5)

            def save(reg):
                nonlocal write_count
                write_count += 1
                if fault == 'registry' and write_count == reject_write:
                    exercised['registry'] += 1
                    raise OSError('injected registry write')
                self.save(reg)

            def archive(key, jid):
                self.assert_invariant(removed=removed)
                if fault == 'archive':
                    exercised['archive'] += 1
                    raise OSError('injected archive copy')
                return self.archive(key, jid)

            with self.subTest(step=step, operation=operation, fault=fault):
                with patch.object(jobs, '_save_registry', side_effect=save), \
                        patch.object(jobs, '_keep_history', side_effect=archive):
                    try:
                        if removed:
                            jobs.remove_all(run=self.host)
                        elif operation in ('add', 'remove'):
                            self.watch(target, add=operation == 'add')
                        else:
                            self.apply_config(cfg, publish=operation != 'recovery' or not cli.phconf.config_path().exists())
                    except (OSError, RuntimeError, KeyboardInterrupt) as exc:
                        self.assertRegex(str(exc), 'injected|fixture rejected schedule')
                self.assert_invariant(removed=removed)
                exercised[operation] += 1
                exercised['interrupt'] += self.host.hit is not None
                exercised['parser'] += self.host.rejections
                exercised['busy'] += ('runs' in self.host.trace and fault == 'busy'
                                      and busy in self.host.live and self.host.states.get(busy) == 'paused')
        for kind in ('archive', 'busy', 'parser', 'registry', 'interrupt', 'add', 'remove', 'uninstall', 'recovery'):
            self.assertGreater(exercised[kind], 0, exercised)
        self.host.reset_faults()
        self.host.executions.clear()
        jobs.remove_all(run=self.host)
        self.assert_invariant(removed=True)
        self.assertEqual(jobs._load_registry(), {})
        self.assertEqual(set(self.host.live), {self.unrelated})
        print(f'\nseed={SEED:#x} steps={STEPS} exercised={dict(sorted(exercised.items()))}', file=self.report)

    def test_interrupt_before_and_after_every_cron_call(self):
        def case(scenario, cut=None, when='before'):
            with temporary_home():
                cfg = self.reset_world()
                if scenario != 'install':
                    cfg['features'].update(health=True, mail_radar=True)
                    cfg['watches'] = [{'name': WATCHES[0], 'schedule': '*/30 * * * *',
                                       'check': 'Fixture', 'when': 'Matches', 'then': 'tell'}]
                    self.apply_config(cfg)
                cfg['features'].update(health=False, mail_radar=True)
                cfg['watches'] = []
                cfg['slack']['enabled'] = True
                cfg['schedule']['say'] = '*/3 * * * *'
                self.host.reset_faults(cut, when)
                try:
                    if scenario == 'uninstall':
                        jobs.remove_all(run=self.host)
                    else:
                        self.apply_config(cfg)
                except KeyboardInterrupt:
                    self.assertIsNotNone(self.host.hit)
                self.assert_invariant(removed=scenario == 'uninstall')
                if cut is not None:
                    self.assertIsNotNone(self.host.hit)
                return self.host.trace

        checks = 0
        observed = set()
        for scenario in ('install', 'update', 'uninstall'):
            trace = case(scenario)
            for cut, action in enumerate(trace, 1):
                for when in ('before', 'after'):
                    with self.subTest(scenario=scenario, cut=cut, action=action, when=when):
                        case(scenario, cut, when)
                        checks += 1
                        observed.add(action)
        self.assertEqual(observed, {'list', 'pause', 'create', 'ensure', 'edit', 'resume', 'runs', 'remove'})
        print(f'\ninterrupted calls={checks} actions={sorted(observed)}', file=self.report)


@contextmanager
def temporary_home():
    """Set HERMES_HOME before execution and delete every fixture home on exit."""
    with TemporaryDirectory() as home, patch.dict(os.environ, {'HERMES_HOME': home}):
        yield
