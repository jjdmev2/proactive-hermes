"""Offline resolution store and CLI regressions."""

import ast
import io
import importlib
import json
import os
import stat
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(TemporaryDirectory()))
        config = self.tmp / "config.json"
        config.write_text(json.dumps({"owner": {"name": "Alex", "work_emails": ["alex@acme.example"]},
                                      "timezone": "UTC"}), encoding="utf-8")
        state = self.tmp / "state"
        self.enterContext(patch.dict(os.environ, EA_STATE_DIR=str(state), HERMES_HOME=str(self.tmp),
                                     PROACTIVE_HERMES_CONFIG=str(config)))
        global resolve
        resolve = importlib.import_module("ea_resolve")
        self.enterContext(patch.object(resolve.phconf, "CFG", resolve.phconf.load()))
        self.clock = datetime(2025, 2, 12, 10, tzinfo=resolve.phconf.tz())
        self.enterContext(patch.object(resolve, 'now', lambda: self.clock))
        self.store = state / 'resolutions.json'
        self.log = state / 'resolutions_log.jsonl'
        self.record = {
            'key': 'sam', 'act': 'send', 'to': 'sam@acme.example',
            'channel': 'work', 'thread': 'thread-1', 'draft': 'Hi Sam, here are the questions.',
            'facts': [{'text': 'Casey provided the list.', 'ref': 'slack:123', 'scope': 'work'}],
            'searched': ['search-only-detail'],
        }

    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = resolve.main(args)
        return code, out.getvalue()

    def cli_put(self, **changes):
        payload = self.tmp / 'input.json'
        payload.write_text(json.dumps({**self.record, **changes}), encoding='utf-8')
        code, output = self.cli('put', '--file', str(payload))
        return code, json.loads(output)

    def test_ready_round_trip_and_private_file_mode(self):
        code, rec = self.cli_put()
        self.assertEqual(code, 0)
        self.assertEqual(rec['status'], 'ready')
        self.assertEqual(resolve.get('sam'), rec)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.cli('get', '--key', 'sam')[1]), rec)
        self.assertEqual(resolve.suffix(rec, 2), 'Draft ready, reply 2.')

    def test_both_dashes_rejected_without_changing_store_or_log(self):
        resolve.put(self.record)
        original = self.store.read_bytes(), self.log.read_bytes()
        for dash in ('\u2013', '\u2014'):
            with self.subTest(dash=dash):
                code, result = self.cli_put(draft=f'Hi {dash} see below.')
                self.assertEqual((code, result['status']), (2, 'rejected'))
                self.assertTrue(result['problems'])
                self.assertEqual((self.store.read_bytes(), self.log.read_bytes()), original)

    def test_placeholders_and_draft_length(self):
        for text in ('[INSERT NAME]', '<name here>', 'TBD', 'XXX', 'x' * 2501):
            with self.subTest(text=text[:20]):
                self.assertTrue(resolve.lint_draft(text))
        self.assertEqual(resolve.lint_draft('x' * 2500), [])

    def test_fact_without_ref_is_rejected(self):
        code, result = self.cli_put(facts=[{'text': 'No citation'}])
        self.assertEqual((code, result['status']), (2, 'rejected'))
        self.assertTrue(any('ref' in problem for problem in result['problems']))
        self.assertFalse(self.store.exists())

    def test_missing_gives_partial_and_open_point_suffix(self):
        rec = resolve.put({**self.record, 'missing': ['Confirm timing']})
        self.assertEqual(rec['status'], 'partial')
        self.assertEqual(resolve.suffix(rec, 1), 'Draft ready with 1 open point, reply 1.')
        self.assertEqual(resolve.suffix({**rec, 'draft': ''}, 1), '')

    def test_personal_fact_in_work_channel_gives_partial(self):
        source = {'text': 'Personal detail', 'ref': 'personal:1', 'scope': 'personal'}
        for channel in ('work', 'D00000000AA', 'alex@acme.example'):
            with self.subTest(channel=channel):
                rec = resolve.put({**self.record, 'channel': channel, 'facts': [source]})
                self.assertEqual(rec['status'], 'partial')
                self.assertIn('personal', resolve.render_show(rec).lower())

    def test_none_and_pay_facts_only(self):
        self.assertEqual(resolve.put({**self.record, 'facts': [], 'draft': ''})['status'], 'none')
        self.assertEqual(self.cli_put(act='pay')[0], 2)
        data = {**self.record, 'act': 'pay', 'draft': ''}
        self.assertEqual(resolve.put(data)['status'], 'ready')

    def test_stale_at_48_hours_or_fingerprint_mismatch(self):
        resolve.put(self.record)
        self.assertIsNotNone(resolve.get('sam', item_fp='one'))
        resolve.stamp_fp('sam', 'one')
        self.assertIsNotNone(resolve.get('sam', item_fp='one', now=self.clock + timedelta(hours=47)))
        self.assertIsNone(resolve.get('sam', item_fp='two'))
        self.assertIsNone(resolve.get('sam', now=self.clock + timedelta(hours=48)))

    def say_fixture(self):
        old = (self.clock - timedelta(hours=37)).isoformat()
        recent = (self.clock - timedelta(hours=1)).isoformat()
        rows = [
            {'at': old, 'kind': 'desk_message', 'keys': ['expired'], 'drafts': {'expired': 'ready'}},
            {'at': recent, 'kind': 'desk_message', 'keys': ['plain', 'sam'], 'drafts': {'sam': 'ready'}},
            {'at': self.clock.isoformat(), 'kind': 'desk_message', 'keys': ['new'], 'drafts': {}},
        ]
        (resolve.state_dir() / 'ping_log.jsonl').write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')

    def test_show_number_maps_display_position_and_marks_shown(self):
        resolve.put(self.record)
        self.say_fixture()
        code, out = self.cli('show', '--n', '2')
        self.assertEqual(code, 0)
        self.assertIn(self.record['draft'], out)
        self.assertEqual(resolve.get('sam')['shown_at'], self.clock.isoformat())

    def test_unknown_number_is_polite(self):
        self.say_fixture()
        self.assertEqual(self.cli('show', '--n', '1'), (0, 'No stored draft for that item.\n'))
        self.clock += timedelta(hours=36)
        self.assertIsNone(resolve.key_for_number(2))

    def test_show_keeps_thread_sources_and_open_points_but_excludes_searched(self):
        resolve.put({**self.record, 'missing': ['Confirm timing'], 'assumptions': ['Partner first']})
        out = self.cli('show', '--key', 'sam')[1]
        for value in ('sam@acme.example', 'thread-1', 'slack:123', 'Confirm timing', 'Partner first'):
            self.assertIn(value, out)
        self.assertNotIn('search-only-detail', out)
        self.assertNotIn('searched', out.lower())

    def test_mark_and_list_open(self):
        resolve.put(self.record)
        self.assertEqual(self.cli('mark', '--key', 'sam', '--approved')[0], 0)
        self.assertTrue(resolve.get('sam')['approved_at'])
        self.assertEqual(self.cli('mark', '--key', 'sam', '--sent', 'gmail:proof')[0], 0)
        self.assertIsNone(resolve.get('sam'))
        self.assertEqual(json.loads(self.cli('list', '--open')[1]), [])
        self.assertEqual(json.loads(self.cli('list')[1])[0]['sent_ref'], 'gmail:proof')
        resolve.put({**self.record, 'key': 'drop'})
        self.assertEqual(self.cli('mark', '--key', 'drop', '--dropped', 'No longer needed')[0], 0)
        self.assertIsNone(resolve.get('drop'))

    def test_source_has_no_send_and_only_gmail_search_can_spawn(self):
        source = Path(resolve.__file__).read_text(encoding='utf-8')
        for token in ('smtplib', 'message send', 'gmail send', 'SEND_MESSAGE'):
            self.assertNotIn(token, source)
        # T2 permits the runner in search_gmail only; the rest of T1 is offline.
        node = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == 'search_gmail')
        lines = source.splitlines()
        self.assertNotIn('subprocess', '\n'.join(lines[:node.lineno - 1] + lines[node.end_lineno:]))
