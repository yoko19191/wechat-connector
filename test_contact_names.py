"""Contact metadata is attached by account and username, never by display name."""

import asyncio
import shutil
import unittest

from mcp import Client
import test_runtime as fixtures
from wechat_connector.cipher_db import sql_text
from wechat_connector.errors import ConnectorError
from wechat_connector.read_chat import all_chats
from wechat_connector.server import create_server


class ContactNameTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RuntimeTests('test_runtime_has_no_capture_dependency')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def write_contacts(self, records):
        record = self.fixture.records[0]
        db = fixtures.FixtureDB(self.fixture.source/record['database'],bytes.fromhex(record['key']))
        try:
            for username,nickname,alias,remark in records:
                values=','.join('NULL' if value is None else sql_text(value) for value in (username,nickname,alias,remark))
                db.execute(f'INSERT INTO contact VALUES({values});')
        finally:
            db.close()

    def test_metadata_and_display_fallbacks_preserve_chat_id(self):
        combinations = [('昵称','custom_id','备注','备注'),('昵称','custom_id','','昵称'),
                        ('','custom_id',None,'custom_id'),('',None,'','test-chat')]
        for nickname,alias,remark,expected in combinations:
            with self.subTest(expected=expected):
                record=self.fixture.records[0]
                db=fixtures.FixtureDB(self.fixture.source/record['database'],bytes.fromhex(record['key']))
                db.execute('DELETE FROM contact;');db.close()
                self.write_contacts([('test-chat',nickname,alias,remark)])
                snapshot=self.fixture.load(self.fixture.refresh())
                row=all_chats(snapshot)[0]
                self.assertEqual(row['chat_id'],'test-chat')
                self.assertEqual(row['username'],'test-chat')
                self.assertEqual(row['nickname'],nickname or None)
                self.assertEqual(row['alias'],alias or None)
                self.assertEqual(row['remark'],remark or None)
                self.assertEqual(row['display_name'],expected)

    def test_missing_or_conflicting_contact_never_guesses(self):
        self.write_contacts([('other-id','test-chat',None,None)])
        snapshot=self.fixture.load(self.fixture.refresh())
        row=all_chats(snapshot)[0]
        self.assertEqual(row['display_name'],'test-chat')
        self.assertIsNone(row['nickname'])
        self.assertIsNone(row['remark'])
        self.write_contacts([('test-chat','姓名A',None,None),('test-chat','姓名B',None,None)])
        snapshot=self.fixture.load(self.fixture.refresh())
        with self.assertRaises(ConnectorError) as caught:
            all_chats(snapshot)
        self.assertEqual(caught.exception.code,'CONTACT_AMBIGUOUS')

    def test_account_isolation_and_no_nickname_matching(self):
        self.write_contacts([('test-chat','账号一昵称','号一','账号一备注'),('unrelated','同名',None,None)])
        shutil.copytree(self.fixture.source/'test-account',self.fixture.source/'second-account')
        records=[{**row,'database':row['database'].replace('test-account/','second-account/',1)}
                 for row in self.fixture.records]
        self.fixture.records+=records;self.fixture.save_keys()
        db=fixtures.FixtureDB(self.fixture.source/records[0]['database'],bytes.fromhex(records[0]['key']))
        db.execute("UPDATE contact SET nick_name='账号二昵称',remark='账号二备注' WHERE username='test-chat';")
        db.close()
        snapshot=self.fixture.load(self.fixture.refresh())
        rows={r['account']:r for r in all_chats(snapshot)}
        self.assertEqual(len(rows),2)
        self.assertEqual(rows['test-account']['display_name'],'账号一备注')
        self.assertEqual(rows['second-account']['display_name'],'账号二备注')
        self.assertEqual([r['account'] for r in all_chats(snapshot,'second-account')],['second-account'])

    def test_mcp_exposes_names_and_accepts_returned_id_for_history(self):
        self.write_contacts([('test-chat','测试昵称','test_alias','测试备注')])
        self.fixture.refresh()
        async def scenario():
            async with Client(create_server(keys_path=self.fixture.keyfile,snapshots_root=self.fixture.output)) as client:
                result=await client.call_tool('wechat_list_chats',{'limit':1})
                self.assertFalse(result.is_error)
                row=result.structured_content['rows'][0]
                self.assertEqual(row['display_name'],'测试备注')
                result=await client.call_tool('wechat_get_chat_history',{'chat_id':row['chat_id'],'account':row['account']})
                self.assertFalse(result.is_error)
                self.assertEqual(len(result.structured_content['rows']),2)
        asyncio.run(scenario())


if __name__=='__main__':
    unittest.main()
