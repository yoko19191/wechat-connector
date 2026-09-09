"""Initialization, migration and MCP contract tests with isolated synthetic users."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import xml.etree.ElementTree as ET
import zstandard
import sys
import unittest
from unittest.mock import Mock, patch

from mcp import Client, StdioServerParameters

from test_runtime import FixtureDB
import test_runtime as fixtures
from wechat_connector.cipher_db import fingerprint, load_keys, save_keys
from wechat_connector.errors import ConnectorError
from wechat_connector import initialize
from wechat_connector.server import ReaderSession, create_server


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RuntimeTests('test_runtime_has_no_capture_dependency')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.base = self.fixture.base
        self.home = self.base / 'new-user'
        (self.home / '.local').mkdir(parents=True, mode=0o755)
        self.keys = self.home / '.local/wechat-connector-keys.jsonl'
        self.state = self.home / '.local/share/wechat-connector'
        self.snapshots = self.state / 'snapshots'

    def init(self, **options):
        return initialize.run_init(root=self.fixture.source, keys_path=self.keys,
            destination_root=self.snapshots, state_dir=self.state, notify=lambda _: None, **options)

    def migrated(self):
        existing = self.fixture.refresh()
        self.init(import_keys=self.fixture.keyfile, snapshot_source=existing)
        return existing

    def test_import_atomic_permissions_reuse_and_conflict(self):
        original_digest = fingerprint(self.fixture.keyfile)
        existing = self.migrated()
        self.assertEqual(load_keys(self.keys), load_keys(self.fixture.keyfile))
        self.assertEqual(self.keys.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.keys.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(fingerprint(self.fixture.keyfile), original_digest)
        with patch.object(initialize, 'capture_account', side_effect=AssertionError('capture forbidden')):
            self.init(snapshot_source=existing)
        self.assertFalse(list(self.snapshots.rglob('plaintext')))
        self.assertFalse(list(self.keys.parent.glob('.wechat-keys-*')))
        before = self.keys.read_bytes()
        with self.assertRaises(ConnectorError):
            save_keys(load_keys(self.fixture.keyfile), self.keys)
        self.assertEqual(self.keys.read_bytes(), before)
        self.keys.chmod(0o644)
        with patch.object(initialize, 'capture_account') as capture:
            with self.assertRaises(ConnectorError) as caught:
                self.init(snapshot_source=existing)
            self.assertEqual(caught.exception.code, 'KEYS_INVALID')
            capture.assert_not_called()

    def test_fresh_init_saves_only_complete_keys_and_resumes_snapshot(self):
        records = load_keys(self.fixture.keyfile)
        with patch.object(initialize, 'capture_account', return_value=records) as capture, \
             patch.object(initialize, 'require_quit'), \
             patch('wechat_connector.snapshot.require_quit'):
            self.init()
            capture.assert_called_once()
        with patch.object(initialize, 'capture_account') as capture, \
             patch.object(initialize, 'require_quit'), \
             patch('wechat_connector.snapshot.require_quit'):
            self.init()
            capture.assert_not_called()
        previous = self.keys.read_bytes()
        with patch.object(initialize, 'create_snapshot', side_effect=RuntimeError('copy still open')), \
             patch.object(initialize, 'require_quit'), patch.object(initialize, 'capture_account') as capture:
            with self.assertRaises(RuntimeError):
                self.init()
            capture.assert_not_called()
        self.assertEqual(previous, self.keys.read_bytes())

    def test_incomplete_capture_timeout_interrupt_and_decline_do_not_publish(self):
        for failure in (ConnectorError('INIT_INCOMPLETE', 'fixture timeout'), KeyboardInterrupt()):
            with patch.object(initialize, 'capture_account', side_effect=failure):
                with self.assertRaises(type(failure)):
                    self.init()
            self.assertFalse(self.keys.exists())
        fake_app = self.base / 'original.app'
        fake_app.mkdir()
        with patch.object(initialize, 'APP', fake_app), patch.object(initialize.sys.stdin, 'isatty', return_value=True), \
             patch.object(initialize.importlib.util, 'find_spec', return_value=object()), \
             patch.object(initialize.subprocess, 'run') as runner:
            with self.assertRaises(ConnectorError) as caught:
                initialize.capture_account(self.fixture.source, 'test-account', prompt=lambda _: 'NO', notify=lambda _: None)
            self.assertEqual(caught.exception.code, 'INIT_CANCELLED')
            runner.assert_not_called()
        with patch.object(initialize, 'APP', fake_app), patch.object(initialize.sys.stdin, 'isatty', return_value=False):
            with self.assertRaises(ConnectorError) as caught:
                initialize.capture_account(self.fixture.source, 'test-account')
            self.assertEqual(caught.exception.code, 'INIT_INTERACTIVE_REQUIRED')

    def test_bridge_timeout_and_interruption_cleanup_without_writing_keys(self):
        from wechat_connector.capture import capture_process
        fake = Mock()
        device = fake.get_local_device.return_value
        session = device.attach.return_value
        session.create_script.return_value.exports_sync.arm.return_value = True
        page = (self.fixture.source / self.fixture.records[-1]['database']).read_bytes()[:4096]
        with patch.dict(sys.modules, {'frida': fake}), patch('wechat_connector.capture.threading.Event') as event:
            event.return_value.wait.return_value = False
            with self.assertRaises(ConnectorError) as caught:
                capture_process(Path('/synthetic-only'), {'fixture': page}, seconds=1, notify=lambda _: None)
            self.assertEqual(caught.exception.code, 'INIT_INCOMPLETE')
            event.return_value.wait.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                capture_process(Path('/synthetic-only'), {'fixture': page}, notify=lambda _: None)
        self.assertEqual(session.detach.call_count, 2)
        device.kill.assert_not_called()
        self.assertFalse(self.keys.exists())

    def test_new_shard_reuse_stops_without_capture(self):
        self.migrated()
        path = self.fixture.source / 'test-account/db_storage/message/message_2.db'
        db = FixtureDB(path, bytes(range(32)))
        db.execute('CREATE TABLE new_messages(id INTEGER)')
        db.close()
        before = fingerprint(self.keys)
        with patch.object(initialize, 'capture_account') as capture, \
             patch.object(initialize, 'require_quit'), patch('wechat_connector.snapshot.require_quit'):
            with self.assertRaises(ConnectorError) as caught:
                self.init()
            self.assertEqual(caught.exception.code, 'KEY_MISSING')
            capture.assert_not_called()
        self.assertEqual(before, fingerprint(self.keys))

    def test_mcp_missing_keys_registration_and_later_removal(self):
        async def scenario():
            server = create_server(keys_path=self.keys, snapshots_root=self.snapshots)
            async with Client(server) as client:
                tools = (await client.list_tools()).tools
                self.assertEqual({t.name for t in tools}, {'wechat_list_chats','wechat_get_chat_history'})
                for tool in tools:
                    self.assertTrue(tool.annotations.read_only_hint)
                    self.assertFalse(tool.annotations.destructive_hint)
                    self.assertFalse(tool.annotations.open_world_hint)
                result = await client.call_tool('wechat_list_chats', {})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['code'], 'KEYS_NOT_FOUND')
                self.migrated()
                result = await client.call_tool('wechat_list_chats', {})
                self.assertFalse(result.is_error)
                self.assertFalse(result.structured_content['live'])
                text = json.dumps(result.structured_content)
                for row in self.fixture.records:
                    self.assertNotIn(row['key'], text)
                self.keys.unlink()
                result = await client.call_tool('wechat_list_chats', {})
                self.assertEqual(result.structured_content['code'], 'KEYS_NOT_FOUND')
                bad = await client.call_tool('wechat_list_chats', {'limit': 101})
                self.assertTrue(bad.is_error)
        asyncio.run(scenario())

    def test_mcp_other_key_failures(self):
        self.migrated()
        reader = ReaderSession(self.keys, self.snapshots)
        original = self.keys.read_bytes()
        self.keys.write_text('invalid JSON')
        self.assertEqual(reader.call('chats').structured_content['code'], 'KEYS_INVALID')
        self.keys.write_bytes(original)
        rows = [json.loads(line) for line in original.splitlines()]
        rows.pop()
        self.keys.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        self.assertEqual(reader.call('chats').structured_content['code'], 'KEY_MISSING')
        self.keys.write_bytes(original)
        rows = [json.loads(line) for line in original.splitlines()]
        rows[0]['key'] = bytes(32).hex()
        self.keys.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        self.assertEqual(reader.call('chats').structured_content['code'], 'KEY_MISMATCH')
        self.keys.write_bytes(original)
        self.assertEqual(ReaderSession(self.keys, self.base/'absent').call('chats').structured_content['code'], 'SNAPSHOT_NOT_FOUND')

    def test_pagination_ties_cursor_scope_and_snapshot_pinning(self):
        for record in self.fixture.records[-2:]:
            db = FixtureDB(self.fixture.source / record['database'], bytes.fromhex(record['key']))
            for index in range(2, 8):
                db.execute(f'INSERT INTO "{self.fixture.table}" VALUES({index},100,1,30,2,2,\'tie {index}\');')
            db.close()
        self.migrated()
        reader = ReaderSession(self.keys, self.snapshots)
        first = reader.call('history', chat_id='test-chat', limit=3).structured_content
        cursor = first['next_cursor']
        rows = list(first['rows'])
        while cursor:
            result = reader.call('history', chat_id='test-chat', limit=3, cursor=cursor).structured_content
            rows += result['rows']
            cursor = result['next_cursor']
        self.assertEqual(len(rows), 14)
        self.assertEqual(len({(r['database'],r['local_id']) for r in rows}), 14)
        self.assertEqual(rows[0]['local_id'], 7)
        self.assertTrue(rows[0]['database'].endswith('message_1.db'))
        bad = reader.call('history', chat_id='other-chat', cursor=first['next_cursor'])
        self.assertEqual(bad.structured_content['code'], 'INVALID_CURSOR')
        bad = reader.call('chats', cursor=first['next_cursor'])
        self.assertEqual(bad.structured_content['code'], 'INVALID_CURSOR')
        # A second snapshot does not switch the running session or its cursor.
        new_source = self.fixture.refresh()
        initialize.import_snapshot(new_source, keys_path=self.keys, destination_root=self.snapshots)
        self.assertEqual(reader.call('chats').structured_content['snapshot_id'], first['snapshot_id'])
        fresh = ReaderSession(self.keys, self.snapshots)
        bad = fresh.call('history', chat_id='test-chat', cursor=first['next_cursor'])
        self.assertEqual(bad.structured_content['code'], 'INVALID_CURSOR')

    def test_stdio_from_another_working_directory_without_frida(self):
        self.migrated()
        async def scenario():
            code = ('from wechat_connector.server import create_server;'
                    f'create_server(keys_path={str(self.keys)!r},snapshots_root={str(self.snapshots)!r}).run()')
            params = StdioServerParameters(command=sys.executable,args=['-c',code],cwd=self.base)
            async with Client(params) as client:
                result = await client.call_tool('wechat_list_chats', {'limit':1})
                self.assertFalse(result.is_error)
                self.assertEqual(len(result.structured_content['rows']), 1)
        self.assertIsNone(importlib.util.find_spec('frida'))
        asyncio.run(scenario())

    def test_partial_capture_return_is_never_published(self):
        partial = load_keys(self.fixture.keyfile)
        partial.pop(next(iter(partial)))
        with patch.object(initialize, 'capture_account', return_value=partial):
            with self.assertRaises(ConnectorError) as caught:
                self.init()
        self.assertEqual(caught.exception.code, 'INIT_INCOMPLETE')
        self.assertFalse(self.keys.exists())

    def test_multiple_accounts_and_cursor_account_binding(self):
        shutil.copytree(self.fixture.source/'test-account', self.fixture.source/'second-account')
        self.fixture.records += [{**row, 'database':row['database'].replace('test-account/', 'second-account/', 1)}
                                 for row in list(self.fixture.records)]
        self.fixture.save_keys()
        with patch.object(initialize, 'capture_account') as capture:
            with self.assertRaises(ConnectorError) as caught:
                self.init()
            self.assertEqual(caught.exception.code, 'ACCOUNT_REQUIRED')
            capture.assert_not_called()
        self.migrated()
        reader = ReaderSession(self.keys, self.snapshots)
        first = reader.call('chats', limit=1).structured_content
        second = reader.call('chats', limit=1, cursor=first['next_cursor']).structured_content
        self.assertNotEqual(first['rows'][0]['account'], second['rows'][0]['account'])
        self.assertFalse(second['has_more'])
        self.assertEqual(reader.call('history',chat_id='test-chat').structured_content['code'], 'ACCOUNT_REQUIRED')
        page = reader.call('history',chat_id='test-chat',account='test-account',limit=1).structured_content
        bad = reader.call('history',chat_id='test-chat',account='second-account',cursor=page['next_cursor'])
        self.assertEqual(bad.structured_content['code'], 'INVALID_CURSOR')

    def test_ten_synthetic_question_answers_via_mcp(self):
        groups = [
            [('项目代号','Orchid'),('预算','1200'),('语言','Python'),('样本数','48'),('报告负责人','林岚'),('里程碑','M2')],
            [('演示时间','周五15:00'),('验收阈值','0.95'),('交付格式','PDF'),('数据版本','v3'),('复核人','周宁'),('会议室','B203')],
        ]
        for record, fields in zip(self.fixture.records[-2:], groups):
            db = FixtureDB(self.fixture.source/record['database'],bytes.fromhex(record['key']))
            for index,(label,value) in enumerate(fields,1):
                text=(label+'：'+value).encode()
                encoded = "X'"+zstandard.ZstdCompressor().compress(text).hex()+"'"
                if index == 1:
                    db.execute(f'UPDATE "{self.fixture.table}" SET message_content={encoded};')
                else:
                    db.execute(f'INSERT INTO "{self.fixture.table}" VALUES({index},100,1,30,2,2,{encoded});')
            db.close()
        self.migrated()
        async def collect():
            async with Client(create_server(keys_path=self.keys,snapshots_root=self.snapshots)) as client:
                chats=(await client.call_tool('wechat_list_chats',{})).structured_content['rows']
                rows=[];cursor=None
                while True:
                    result=await client.call_tool('wechat_get_chat_history',{
                        'chat_id':chats[0]['chat_id'],'account':chats[0]['account'],'limit':3,'cursor':cursor})
                    self.assertFalse(result.is_error)
                    page=result.structured_content
                    rows+=page['rows'];cursor=page['next_cursor']
                    if not cursor: return rows
        rows=asyncio.run(collect())
        fields=dict(row['message_content'].split('：',1) for row in rows)
        actual=[fields['项目代号']+'/'+fields['数据版本'],str(int(fields['预算'])//int(fields['样本数'])),
                str(int(fields['预算'])//(int(fields['样本数'])+12)),fields['报告负责人']+'/'+fields['复核人'],
                fields['演示时间'],fields['交付格式']+'/'+fields['语言'],str(int(float(fields['验收阈值'])*100))+'%',
                rows[0]['message_content'].split('：',1)[1],str(rows[-1]['local_id']),str(len(rows))]
        expected=[node.findtext('answer') for node in ET.parse(Path(__file__).with_name('evaluations.xml')).getroot()]
        self.assertEqual(actual,expected)


if __name__ == '__main__':
    unittest.main()
