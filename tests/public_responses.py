"""Public response regressions; real temporary stores, no live sessions or DBs."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mailbox_public_test', REPO / 'plugin.py')
assert spec and spec.loader
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        (self.home / 'runtime').mkdir()
        self.registry = self.home / 'runtime/active_sessions.json'
        self.registry.write_text(json.dumps({'entries': [{'session_id': sid} for sid in ('sender', 'receiver', 'other')]}), encoding='utf-8')
        self.tools = plugin.MailboxTools(REPO, self.home / 'mailbox.sqlite3', self.home)

    def send(self, key='one', body='peer payload', **kw):
        args = dict(sender_profile='default', sender_session='sender', recipient_profile='default',
                    recipient_session='receiver', body=body, key=key, reply_to=None)
        args.update(kw)
        return self.tools.send(**args)

    def call(self, name, args):
        spec = next(s for s in plugin.TOOL_SPECS if s[0] == 'session_mailbox_' + name)
        schema = plugin._schema(*spec)
        return json.loads(plugin.make_handler(self.tools, spec[0], schema)(args))

    def test_send_body_free_idempotent_receipt_preserves_stored_body(self):
        first = self.send(body='가' * 2000)
        retry = self.send(body='가' * 2000)
        self.assertNotIn('body', first)
        self.assertNotIn('body', retry)
        self.assertEqual(first['id'], retry['id'])
        self.assertTrue(retry['deduplicated'])
        self.assertEqual(self.tools.mailbox().inbox('default', 'receiver')['messages'][0]['body'], '가' * 2000)
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.send(body='changed')

    def test_public_wake_projection_keeps_detail_receipt_not_internal_owner(self):
        sent = self.send()
        self.assertEqual(sent['wake'], {'state': 'pending'})
        wakes = self.tools.wakes()
        self.assertTrue(wakes.claim(sent['id'], 'private-owner'))
        receipt = {'accepted': True, 'session_id': 'receiver'}
        wakes.finish(sent['id'], 'unknown', 'uncertain dispatch; do not retry', receipt)
        result = self.tools.message_status(sent['id'])
        self.assertEqual(result['wake'], {'state': 'unknown', 'detail': 'uncertain dispatch; do not retry', 'receipt': receipt})
        self.assertEqual(wakes.status(sent['id'])['owner_key'], 'private-owner')
        self.assertEqual(self.send()['wake'], result['wake'])
        self.assertFalse(wakes.claim(sent['id'], 'another-owner'))

    def test_public_ack_minimal_atomic_idempotent(self):
        first, second = self.send(), self.send('two')
        other = self.send('other', recipient_session='other')
        ids = [first['id'], second['id']]
        with self.assertRaisesRegex(ValueError, 'exact recipient'):
            self.tools.ack('default', 'receiver', [first['id'], other['id']])
        self.assertEqual(self.tools.mailbox().status(first['id'])['status'], 'queued')
        result = self.tools.ack('default', 'receiver', ids + ids)
        self.assertEqual(result['acknowledged_count'], 2)
        self.assertEqual([r['id'] for r in result['messages']], ids)
        for row in result['messages']:
            self.assertEqual(set(row), {'id', 'status', 'acknowledged_at'})
            self.assertEqual(row['status'], 'acknowledged')
            self.assertIsNotNone(row['acknowledged_at'])
        self.assertEqual(self.tools.ack('default', 'receiver', ids), result)

    def test_inbox_recipient_once_with_exact_sender_and_full_body(self):
        sent = self.send(body='whole body')
        result = self.tools.inbox('default', 'receiver', 20, None)
        self.assertEqual(result.get('recipient_profile'), 'default')
        self.assertEqual(result.get('recipient_session'), 'receiver')
        row = result['messages'][0]
        self.assertNotIn('recipient_profile', row)
        self.assertNotIn('recipient_session', row)
        self.assertEqual(row['sender_profile'], 'default')
        self.assertEqual(row['sender_session'], 'sender')
        self.assertEqual(row['id'], sent['id'])
        self.assertEqual(row['body'], 'whole body')
        self.assertEqual(row['status'], 'queued')
        self.assertTrue(result['peer_content_untrusted'])
        # One owner: the store itself returns the public shape, no adapter re-projection.
        self.assertEqual(self.tools.mailbox().inbox('default', 'receiver'), result)

    def test_inbox_byte_budget_whole_messages_exact_pagination_after_ack(self):
        payloads = ['X' * 7000, '가' * 2300, '\u0001' * 1100]
        sent = [self.send('bulk-' + str(i), payloads[i % 3]) for i in range(23)]
        expected = [row['id'] for row in sent]
        after, seen = None, []
        while True:
            result = self.call('inbox', dict(profile='default', session='receiver', limit=100, after=after))
            self.assertNotIn('error', result, result)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode('utf-8')), 32768)
            self.assertTrue(result['messages'])
            for row in result['messages']:
                self.assertEqual(row['body'], payloads[len(seen) % 3])
                seen.append(row['id'])
            if not result['has_more']:
                self.assertIsNone(result['next_cursor'])
                break
            self.assertEqual(result['next_cursor'], result['messages'][-1]['id'])
            after = result['next_cursor']
            self.tools.ack('default', 'receiver', [r['id'] for r in result['messages']])
        self.assertEqual(seen, expected)
        self.assertEqual(len(set(seen)), len(expected))
        # The cursor anchor still exists after explicit ACK; wrong recipients never qualify.
        other = self.send('other', recipient_session='other')
        with self.assertRaisesRegex(ValueError, 'cursor'):
            self.tools.inbox('default', 'receiver', 1, other['id'])
        small = self.call('inbox', dict(profile='default', session='receiver', limit=1))
        self.assertEqual(len(small['messages']), 1)
        self.assertLessEqual(len(json.dumps(small, ensure_ascii=False).encode('utf-8')), 32768)

    def test_inbox_budget_is_tight_and_refuses_an_oversized_stored_message(self):
        import sqlite3, uuid
        sent = [self.send('fill-' + str(i), 'X' * 7000) for i in range(5)]
        first = self.call('inbox', dict(profile='default', session='receiver', limit=100))
        self.assertEqual([r['id'] for r in first['messages']], [r['id'] for r in sent[:4]])
        # The next whole message would cross the budget: paging is not merely conservative.
        grown = dict(first, messages=first['messages'] + [dict(first['messages'][0], id=sent[4]['id'])])
        self.assertGreater(len(json.dumps(grown, ensure_ascii=False).encode('utf-8')), 32768)
        self.tools.ack('default', 'receiver', [r['id'] for r in sent])
        # Only an out-of-band write can store a body beyond the 8192-byte send bound.
        from contextlib import closing
        with closing(sqlite3.connect(self.home / 'mailbox.sqlite3')) as db:
            db.execute("INSERT INTO messages (id, sender_profile, sender_session, recipient_profile, "
                       "recipient_session, body, key, created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), 'default', 'sender', 'default', 'receiver', 'Y' * 40000, 'raw', 'now'))
            db.commit()
        result = self.call('inbox', dict(profile='default', session='receiver'))
        self.assertEqual(result['error'], 'stored message exceeds the inbox response byte budget')

    def test_discovery_groups_warnings_and_bounds_distinct_groups(self):
        self.registry.write_text(json.dumps({'entries': [{'session_id': ' invalid'}] * 25}), encoding='utf-8')
        result = self.tools.list_sessions()
        self.assertEqual(result['warning_count'], 25)
        self.assertEqual(result['warnings'], [{'profile': 'default', 'source': 'registry',
                                             'error': 'invalid stored session ID skipped', 'count': 25}])
        for index in range(22):
            profile = self.home / 'profiles' / ('p' + str(index))
            (profile / 'runtime').mkdir(parents=True)
            (profile / 'config.yaml').write_text('{}', encoding='utf-8')
            (profile / 'runtime/active_sessions.json').write_text('{bad', encoding='utf-8')
        result = self.tools.list_sessions()
        self.assertEqual(result['warning_count'], 47)
        self.assertEqual(len(result['warnings']), 20)
        self.assertEqual(result['warning_group_count'], 23)
        self.assertEqual(len({(w['profile'], w['source'], w['error']) for w in result['warnings']}), 20)

    def test_public_detailed_listing_single_unverified_liveness_field(self):
        rows = self.tools.list_sessions(detailed=True)['sessions']
        self.assertEqual({row['session'] for row in rows}, {'sender', 'receiver', 'other'})
        for row in rows:
            self.assertNotIn('stored_session_id', row)
            self.assertNotIn('verified_alive', row)
            self.assertNotIn('active_hint', row)
            self.assertEqual(row['liveness'], 'unverified_registry_hint')

    def test_schema_bounds_reject_before_opening_stores(self):
        for name, args in (
            ('list_sessions', {'limit': 101}), ('list_sessions', {'limit': 0}),
            ('list_sessions', {'offset': -1}), ('list_sessions', {'offset': 10001}),
            ('list_sessions', {'query': 'x' * 257}),
            ('inbox', {'profile': 'default', 'session': 'receiver', 'limit': 101}),
            ('ack', {'profile': 'default', 'session': 'receiver', 'message_ids': []}),
            ('ack', {'profile': 'default', 'session': 'receiver', 'message_ids': ['x'] * 101}),
        ):
            with self.subTest(name=name, args=args):
                result = self.call(name, args)
                self.assertIn('error', result)
                self.assertIsNone(self.tools._mailbox, 'schema-bound error opened store')
        specs = {s[0]: plugin._schema(*s) for s in plugin.TOOL_SPECS}
        props = specs['session_mailbox_list_sessions']['parameters']['properties']
        self.assertEqual((props['limit'].get('minimum'), props['limit'].get('maximum')), (1, 100))
        self.assertEqual((props['offset'].get('minimum'), props['offset'].get('maximum')), (0, 10000))
        for limit in (1, 100):
            self.assertEqual(plugin._bind(specs['session_mailbox_list_sessions'], {'limit': limit})['limit'], limit)
        self.assertEqual(plugin._bind(specs['session_mailbox_list_sessions'], {'offset': 10000})['offset'], 10000)

    def test_utf8_request_bound_is_bytes_not_characters(self):
        # 3000 characters fit a character ceiling but exceed the UTF-8 byte contract.
        with self.assertRaisesRegex(ValueError, '8192 bytes'):
            self.send(body='가' * 3000)
        with self.assertRaisesRegex(ValueError, 'including routing metadata'):
            self.send(body='X' * 8100)

    def test_error_classification_once_and_real_setting_names(self):
        from unittest.mock import patch
        # Exercise installed registry helper and the stdlib-only fallback.
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                context = patch.dict('sys.modules', {'tools.registry': None}) if fallback else __import__('contextlib').nullcontext()
                with context:
                    result = json.loads(plugin._error(ValueError('invalid request')))
                self.assertEqual(result['error'], 'invalid request')
                self.assertEqual(result['error_type'], 'ValueError')
        cls = plugin._stores_module(REPO).Mailbox
        with self.assertRaisesRegex(ValueError, 'db_path and hermes_root settings'):
            cls(Path('relative.db'), self.home)
        with self.assertRaisesRegex(ValueError, 'hermes_root setting'):
            cls(self.home / 'mailbox.sqlite3', self.home / 'missing')

    def test_tool_descriptions_keep_relevant_trust_warnings_without_wake_boilerplate(self):
        captured = {}
        class Context:
            def register_tool(self, **kw):
                captured[kw['name']] = kw['schema']['description']
        plugin.register_tools(Context(), self.tools)
        for text in captured.values():
            self.assertIn('not authentication', text)
            self.assertIn('untrusted', text)
            self.assertIn('not authorization', text)
        self.assertNotIn('wake', captured['session_mailbox_list_sessions'].lower())
        self.assertNotIn('wake', captured['session_mailbox_ack'].lower())
        self.assertIn('scheduling', captured['session_mailbox_send'])
        self.assertLess(sum(map(len, captured.values())), 2100)

    def test_wake_projection_empty_receipts_and_absent_wake_preserve_semantics(self):
        for state in ('queued', 'submitted', 'refused', 'unknown', 'acknowledged'):
            with self.subTest(state=state):
                sent = self.send(state)
                wakes = self.tools.wakes()
                if state != 'acknowledged':
                    self.assertTrue(wakes.claim(sent['id'], 'internal-owner'))
                wakes.finish(sent['id'], state, 'meaningful detail', {})
                self.assertEqual(self.tools.message_status(sent['id'])['wake'],
                                 {'state': state, 'detail': 'meaningful detail', 'receipt': {}})
        raw = self.tools.mailbox().send('default', 'sender', 'default', 'receiver', 'no wake', 'raw-only')
        result = self.tools.message_status(raw['id'])
        self.assertIsNone(result['wake'])
        self.assertNotIn('body', result)

    def test_committed_send_survives_wake_request_failure(self):
        from unittest.mock import patch
        with patch.object(self.tools.wakes(), 'request', side_effect=OSError('fixture failure')):
            result = self.send()
        self.assertNotIn('body', result)
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['wake'], {'state': 'error',
                         'detail': 'wake request was not recorded; message remains stored', 'error_type': 'OSError'})
        self.assertEqual(self.tools.mailbox().status(result['id'])['status'], 'queued')
        self.assertEqual(self.send()['id'], result['id'])

    def test_empty_inbox_has_recipient_context_and_no_cursor(self):
        result = self.call('inbox', {'profile': 'default', 'session': 'receiver'})
        self.assertEqual(result, {'messages': [], 'has_more': False, 'next_cursor': None,
                                 'peer_content_untrusted': True,
                                 'recipient_profile': 'default', 'recipient_session': 'receiver'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
