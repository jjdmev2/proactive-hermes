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
        self.enterContext(patch.dict(os.environ, HERMES_HOME=str(self.tmp)))
        global resolve
        resolve = importlib.import_module("ea_resolve")
        # The config and state paths are not settable from the environment: patch the functions.
        self.enterContext(patch.object(resolve.phconf, "config_path", return_value=config))
        self.enterContext(patch.object(resolve.phconf, "state_dir",
                                       side_effect=lambda: state.mkdir(mode=0o700, exist_ok=True) or state))
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
        row = resolve.get('sam')
        version = resolve.draft_version(row) if row else 'unknown'
        self.log_messages({'at': (self.clock - timedelta(hours=1)).isoformat(), 'kind': 'desk_message',
                           'keys': ['plain', 'sam'], 'drafts': {'sam': 'ready'}, 'say_id': 'a' * 24,
                           'draft_versions': {'sam': version}})

    def delivered(self, extra=None):
        def statuses(_notices):
            rows = [json.loads(line) for line in
                    (resolve.phconf.state_dir() / 'ping_log.jsonl').read_text().splitlines() if line.strip()]
            out = {resolve._message_id(r): {'status': 'delivered', 'reply_id': i} for i, r in enumerate(rows, 1)}
            out.update(extra or {})
            return out
        return patch.object(resolve, '_say_statuses', statuses)

    def log_messages(self, *rows):
        (resolve.phconf.state_dir() / 'ping_log.jsonl').write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')

    def test_reply_number_binds_to_bot_reply_order_and_original_draft_version(self):
        sam = resolve.put(self.record)
        other = resolve.put({**self.record, 'key': 'other', 'draft': 'Hello again.'})
        at = lambda h: (self.clock - timedelta(hours=h)).isoformat()  # noqa: E731
        received = {'at': at(2), 'kind': 'desk_message', 'job': 'radar', 'keys': ['sam'], 'drafts': {'sam': 'ready'},
                    'draft_versions': {'sam': resolve.draft_version(sam)}, 'say_id': 'a' * 24}
        queued = {'at': at(1), 'kind': 'desk_message', 'job': 'radar', 'keys': ['other'], 'drafts': {'other': 'ready'},
                  'draft_versions': {'other': resolve.draft_version(other)}, 'say_id': 'b' * 24}
        self.log_messages(received, queued)
        statuses = {'a' * 24: {'status': 'delivered', 'reply_id': 12}}
        self.assertEqual(resolve.number_target(1, statuses=statuses), ('sam', 'ok'))
        for state in ('handed', 'accepted', 'unverified'):
            statuses['b' * 24] = {'status': state}
            self.assertEqual(resolve.number_target(1, statuses=statuses), (None, resolve.AMBIGUOUS))
        statuses['b' * 24] = {'status': 'delivered', 'reply_id': 9}
        self.assertEqual(resolve.number_target(1, statuses=statuses), ('sam', 'ok'))
        statuses['b' * 24]['reply_id'] = 13
        self.assertEqual(resolve.number_target(1, statuses=statuses), ('other', 'ok'))
        resolve.put({**self.record, 'key': 'other', 'draft': 'A different draft.'})
        self.assertEqual(resolve.number_target(1, statuses=statuses), (None, resolve.AMBIGUOUS))
        self.assertEqual(resolve.number_target(1, statuses={}), (None, resolve.AMBIGUOUS))

    def test_reply_number_uses_display_order_even_for_an_older_composed_notice(self):
        old = resolve.put({**self.record, 'key': 'old', 'to': 'old@acme.example', 'draft': 'Older draft.'})
        new = resolve.put({**self.record, 'key': 'new', 'to': 'new@acme.example', 'draft': 'Newer draft.'})
        old_id, new_id = 'a' * 24, 'b' * 24
        self.log_messages(*[
            {'at': (self.clock - timedelta(hours=hours)).isoformat(), 'kind': 'desk_message',
             'keys': [row['key']], 'drafts': {row['key']: 'ready'}, 'say_id': mid,
             'draft_versions': {row['key']: resolve.draft_version(row)}}
            for row, mid, hours in ((old, old_id, 37), (new, new_id, 1))
        ])
        with self.delivered({new_id: {'status': 'delivered', 'reply_order': 1},
                             old_id: {'status': 'delivered', 'reply_order': 2}}):
            self.assertIn('To: old@acme.example', self.cli('show', '--n', '1')[1])
            for state in ('missing', 'stale', 'closed'):
                with self.subTest(state=state):
                    rows = {new['key']: new}
                    if state == 'stale':
                        rows[old['key']] = {**old, 'created_at': (self.clock - timedelta(days=3)).isoformat()}
                    elif state == 'closed':
                        rows[old['key']] = {**old, 'dropped_at': self.clock.isoformat()}
                    with patch.object(resolve, '_load', return_value=rows):
                        output = self.cli('show', '--n', '1')[1]
                    self.assertIn('Ask the owner which item', output)
                    self.assertNotIn('new@acme.example', output)

    def test_reply_number_ignores_only_unresolved_relays_proven_older_in_bot_chat(self):
        import desk_say

        sam = resolve.put(self.record)
        latest = resolve.put({**self.record, 'key': 'latest', 'to': 'latest@acme.example', 'draft': 'Latest draft.'})
        old_id, new_id = 'a' * 24, 'b' * 24
        old_text, new_text = 'Old numbered notice.', 'New numbered notice.'
        self.log_messages(*[
            {'kind': 'desk_message', 'keys': [row['key']], 'drafts': {row['key']: 'ready'},
             'say_id': mid, 'draft_versions': {row['key']: resolve.draft_version(row)}}
            for row, mid in ((latest, new_id), (sam, old_id))  # composition order is not display order
        ])
        say_log = self.tmp / 'say_log.jsonl'
        header = '[Cronjob "Proactive · say" output — scheduled job, not the user.]\n\n'
        earlier = [(51, 'user', header + desk_say.job_output(old_text, old_id)),
                   (50, 'assistant', 'A paraphrase without a receipt.')]
        later = [(11, 'user', header + desk_say.job_output(new_text, new_id)), (10, 'assistant', new_text)]
        with patch.multiple(desk_say, STATE=self.tmp, SAY_LOG=say_log, OUTBOX=self.tmp / 'outbox.jsonl'), \
                patch.object(desk_say, 'bot_chat_turns') as transcript:
            for status in ('unverified', 'handed'):
                with self.subTest(status=status):
                    say_log.write_text(''.join(json.dumps(row) + '\n' for row in (
                        {'id': old_id, 'status': status, 'text': old_text, 'epoch': desk_say.time.time()},
                        {'id': new_id, 'status': 'delivered', 'text': new_text, 'reply_id': 10},
                    )))
                    transcript.return_value = earlier + later
                    self.assertIn('To: latest@acme.example', self.cli('show', '--n', '1')[1])
                    for turns in (later + earlier, later, None, earlier + later + [(52, *earlier[0][1:])]):
                        transcript.return_value = turns
                        self.assertEqual(resolve.number_target(1), (None, resolve.AMBIGUOUS))
                    self.assertEqual(desk_say.status_of(old_id), status)

    def test_reply_number_ignores_a_legacy_notice_only_if_its_display_order_is_proven(self):
        desk_say = importlib.import_module('desk_say')

        old_text = 'Morgan needs a receipt by Friday Draft ready, reply 1.'
        new_text = 'Riley needs a receipt by Friday. Draft ready, reply 1.'
        old = {'kind': 'desk_message', 'job': 'audit', 'keys': ['Morgan'], 'text': old_text,
               'drafts': {'Morgan': 'ready'}, 'delivery': 'delivered', 'row_id': None}
        latest = resolve.put({**self.record, 'key': 'Riley', 'to': 'Riley@example.org'})
        new_id = 'b' * 24
        new = {'kind': 'desk_message', 'keys': ['Riley'], 'text': new_text, 'say_id': new_id,
               'drafts': {'Riley': 'ready'}, 'draft_versions': {'Riley': resolve.draft_version(latest)}}
        self.log_messages(old, new)
        say_log = self.tmp / 'say_log.jsonl'
        say_log.write_text(''.join(json.dumps(row) + '\n' for row in (
            {'id': resolve._message_id(old), 'status': 'delivered', 'key': 'audit:Morgan',
             'session_id': 'local-legacy', 'row_id': None, 'push': 'door', 'door_status': 'queued'},
            {'id': new_id, 'status': 'delivered', 'reply_id': 6, 'text': new_text},
        )))
        # The old internal relay was hidden; the exact assistant reply was visible.
        earlier = (4, 'assistant', old_text)
        later = (6, 'assistant', new_text)
        with patch.multiple(desk_say, STATE=self.tmp, SAY_LOG=say_log, OUTBOX=self.tmp / 'outbox.jsonl'), \
                patch.object(desk_say, 'bot_chat_turns') as transcript:
            for turns in ([later, earlier], [later], None, [earlier, later, (8, 'assistant', old_text)]):
                transcript.return_value = turns
                self.assertEqual(resolve.number_target(1), (None, resolve.AMBIGUOUS))
            transcript.return_value = [earlier, later]
            self.assertEqual(resolve.number_target(1), ('Riley', 'ok'))
            self.assertIn('To: Riley@example.org', self.cli('show', '--n', '1')[1])
            transcript.return_value = [earlier]
            self.assertEqual(resolve.number_target(1), (None, resolve.AMBIGUOUS))

    def test_later_numbered_notice_without_a_draft_does_not_select_an_older_draft(self):
        sam = resolve.put(self.record)
        at = self.clock.isoformat()
        self.log_messages({'at': at, 'kind': 'desk_message', 'keys': ['sam'], 'say_id': 'a' * 24,
                           'drafts': {'sam': 'ready'}, 'draft_versions': {'sam': resolve.draft_version(sam)}},
                          {'at': at, 'kind': 'desk_message', 'keys': ['x', 'y'], 'say_id': 'b' * 24})
        receipts = {'a' * 24: {'status': 'delivered', 'reply_order': 1, 'reply_id': 50},
                    'b' * 24: {'status': 'delivered', 'reply_order': 2, 'reply_id': 20}}
        self.assertEqual(resolve.number_target(1, statuses=receipts), (None, resolve.AMBIGUOUS))

    def test_show_asks_when_the_number_is_ambiguous(self):
        resolve.put(self.record)
        at = lambda h: (self.clock - timedelta(hours=h)).isoformat()  # noqa: E731
        self.log_messages(
            {'at': at(2), 'kind': 'desk_message', 'keys': ['plain', 'sam'], 'drafts': {'sam': 'ready'}, 'say_id': 'a' * 24},
            {'at': at(1), 'kind': 'desk_message', 'keys': ['x', 'y'], 'drafts': {'y': 'ready'}, 'say_id': 'b' * 24})
        with self.delivered({'b' * 24: {'status': 'handed'}}):
            code, out = self.cli('show', '--n', '2')
        self.assertEqual(code, 0)
        self.assertIn('Ask the owner which item', out)
        self.assertNotIn(self.record['draft'], out)
        self.assertIsNone(resolve.get('sam').get('shown_at'))

    def test_legacy_rows_get_the_id_desk_say_gave_them(self):
        sys_path = str(Path(resolve.__file__).resolve().parent)
        import sys
        sys.path.insert(0, sys_path)
        import desk_say
        row = {'job': 'radar', 'keys': ['a', 'b'], 'text': 'Two things.\n'}
        self.assertEqual(resolve._message_id(row), desk_say.msg_id('radar:a,b', 'Two things.'))

    def test_show_number_maps_display_position_and_marks_shown(self):
        resolve.put(self.record)
        self.say_fixture()
        self.enterContext(self.delivered())
        code, out = self.cli('show', '--n', '2')
        self.assertEqual(code, 0)
        self.assertIn(self.record['draft'], out)
        self.assertEqual(resolve.get('sam')['shown_at'], self.clock.isoformat())

    def test_unknown_number_is_polite(self):
        self.say_fixture()
        self.enterContext(self.delivered())
        self.assertIn('Ask the owner', self.cli('show', '--n', '1')[1])
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
