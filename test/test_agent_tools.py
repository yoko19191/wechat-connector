"""Agent-facing semantics and hard response limits. All samples are synthetic."""

import asyncio
import hashlib
from html import escape
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from mcp import Client
import zstandard

from . import test_runtime as fixtures
from wechat_connector import read_chat
from wechat_connector.cipher_db import sql_text
from wechat_connector.errors import ConnectorError
from wechat_connector.message_content import clean_url, normalize
from wechat_connector.server import (MAX_RESULT_BYTES, ReaderSession, create_server,
                                     cursor_encode, result_bytes, tool_result)

SECRET = 'SYNTHETIC_PRIVATE_CREDENTIAL_7x'


def app(kind, title, extra=''):
    return f'<msg><appmsg><type>{kind}</type><title>{escape(title)}</title>{extra}</appmsg></msg>'


class ProjectionTests(unittest.TestCase):
    def test_text_file_and_direct_quote_content(self):
        literal = '<configuration>this is user-authored text</configuration>'
        self.assertEqual(normalize(1, literal)['text'], literal)
        attachment = f'<appattach><fileext>pdf</fileext><totallen>123</totallen><aeskey>{SECRET}</aeskey><fileuploadtoken>{SECRET}</fileuploadtoken></appattach>'
        file = normalize(49, app(6, 'report.pdf', attachment))
        self.assertEqual(file['text'], '[文件]\n名称：report.pdf\n类型：pdf\n大小：123 字节')
        quoted = app(57, 'original reply', f'<refermsg><type>1</type><content>deeper chain excluded</content></refermsg>{attachment}')
        reply = app(57, 'current reply', f'<refermsg><type>49</type><displayname>Alice</displayname><svrid>9223372036854775807</svrid><content>{escape(quoted)}</content></refermsg>{attachment}')
        result = normalize(49, reply)
        self.assertEqual(result['text'], 'current reply\n\n引用（Alice）：\noriginal reply')
        self.assertEqual(result['reply_to']['server_id'], '9223372036854775807')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_media_system_unknown_and_malformed(self):
        cases = [(3, f'<msg><img cdnthumbwidth="80" cdnthumbheight="60" aeskey="{SECRET}" /></msg>', 'image', '80×60'),
                 (34, f'<msg><voicemsg voicelength="2500" aeskey="{SECRET}" /></msg>', 'voice', '2500 毫秒'),
                 (43, f'<msg><videomsg playlength="12" aeskey="{SECRET}" /></msg>', 'video', '12 秒'),
                 (47, f'<msg><emoji desc="wave" cdnurl="{SECRET}" /></msg>', 'emoji', 'wave'),
                 (10000, 'Alice joined the group', 'system', 'Alice joined'),
                 (10002, '<sysmsg><revokemsg><replacemsg>撤回了一条消息</replacemsg></revokemsg></sysmsg>', 'system', '撤回')]
        for kind, body, expected, text in cases:
            result = normalize(kind, body)
            self.assertEqual(result['kind'], expected)
            self.assertIn(text, result['text'])
            self.assertNotIn(SECRET, json.dumps(result))
        for kind, body in [(49, '<malformed'), (49, app(99, SECRET)), (99999, SECRET), (None, SECRET)]:
            result = normalize(kind, body)
            self.assertNotEqual(result['status'], 'ok')
            self.assertNotIn(SECRET, json.dumps(result))

    def test_url_credentials_and_entities_never_escape(self):
        public = 'https://mp.weixin.qq.com/s?__biz=YWJj&mid=123&idx=1&sn=' + 'a'*32
        source = public + f'&pass_ticket={SECRET}&exportkey={SECRET}#secret'
        url, sanitized = clean_url(source)
        self.assertTrue(sanitized)
        self.assertNotIn(SECRET, url)
        self.assertIn('mid=123', url)
        self.assertEqual(clean_url(f'https://user:{SECRET}@example.org/path?id=42#fragment'), ('https://example.org/path', True))
        body = app(5, 'Article', f'<des>Description</des><url>{escape(source)}</url><nested><token>{SECRET}</token></nested>')
        result = normalize(49, body)
        self.assertEqual(result['text'], 'Article\nDescription\n链接：' + url)
        for evil in [f'<!DOCTYPE msg [<!ENTITY x "{SECRET}">]><msg><appmsg><type>5</type><title>&x;</title></appmsg></msg>',
                     f'<msg><appmsg><type>5</type><title><aeskey>{SECRET}</aeskey></title></appmsg></msg>',
                     '<!DOCTYPE msg SYSTEM "https://example.org/external"><msg/>']:
            with patch('socket.create_connection') as network:
                result = normalize(49, evil)
                network.assert_not_called()
            self.assertEqual(result['status'], 'MESSAGE_PARSE_FAILED')
            self.assertNotIn(SECRET, json.dumps(result))

    def test_uniform_16_mib_decode_limit_and_bounded_errors(self):
        for raw in ['字' * (read_chat.MAX_MESSAGE_BYTES//3+1), zstandard.ZstdCompressor().compress(b'x'*(read_chat.MAX_MESSAGE_BYTES+1))]:
            with self.assertRaises(ConnectorError) as caught:
                read_chat.decode(raw)
            self.assertEqual(caught.exception.code, 'MESSAGE_TOO_LARGE')
        result = tool_result({'code':'E','message':'x'*100000}, error=True)
        self.assertLessEqual(result_bytes(result), MAX_RESULT_BYTES)
        self.assertLessEqual(len(result.content[0].text),200)


class AgentToolTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RuntimeTests('test_runtime_has_no_capture_dependency')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def update(self, index, sql):
        record = self.f.records[index]
        db = fixtures.FixtureDB(self.f.source/record['database'],bytes.fromhex(record['key']))
        try:
            db.execute(sql)
        finally:
            db.close()

    def ready(self):
        self.f.refresh()
        return ReaderSession(self.f.keyfile,self.f.output)

    def result(self, value):
        self.assertLessEqual(result_bytes(value),MAX_RESULT_BYTES)
        self.assertLessEqual(len(value.content[0].text),200)
        self.assertFalse(value.is_error, value.structured_content)
        return value.structured_content

    def test_search_filters_before_counts_and_preserves_namesakes(self):
        self.update(0, "INSERT INTO contact VALUES ('test-chat','Straße','literal%_id','同名备注');")
        reader = self.ready()
        with patch.object(read_chat,'query',wraps=read_chat.query) as sql:
            result = self.result(reader.call('chats',query='  STRASSE '))
            self.assertEqual(len(result['rows']),1)
            self.assertNotIn('username',result['rows'][0])
            self.assertTrue(any('count(*) AS n' in args.args[2] for args in sql.call_args_list))
        with patch.object(read_chat,'query',wraps=read_chat.query) as sql:
            result = self.result(reader.call('chats',query='absent'))
            self.assertEqual(result['rows'],[])
            self.assertFalse(any('count(*) AS n' in args.args[2] for args in sql.call_args_list))
        self.assertEqual(len(self.result(reader.call('chats',query='%_'))['rows']),1)
        self.assertEqual(reader.call('chats',query='  ').structured_content['code'],'INVALID_QUERY')
        # Same visible name for distinct IDs must not collapse into a single person.
        other='other-chat';table='Msg_'+hashlib.md5(other.encode()).hexdigest()
        self.update(0, "INSERT INTO contact VALUES ('other-chat','Different',NULL,'同名备注');")
        self.update(-1, f'INSERT INTO Name2Id VALUES (\'{other}\'); CREATE TABLE "{table}" AS SELECT * FROM "{self.f.table}";')
        reader = self.ready()
        first=self.result(reader.call('chats',query='同名备注',limit=1))
        second=self.result(reader.call('chats',query='同名备注',limit=1,cursor=first['next_cursor']))
        self.assertNotEqual(first['rows'][0]['chat_id'],second['rows'][0]['chat_id'])
        self.assertEqual(reader.call('chats',query='Different',cursor=first['next_cursor']).structured_content['code'],'INVALID_CURSOR')

    def test_complete_messages_reduce_page_count_without_truncation(self):
        for index in (-2,-1):
            for local_id in range(2,7):
                text=f'{index}:{local_id}|'+('中' * 900)
                self.update(index,f'INSERT INTO "{self.f.table}" VALUES({local_id},2,1,30,2,2,{sql_text(text)});')
        reader=self.ready()
        rows=[];cursor=None;pages=0
        while True:
            page=self.result(reader.call('history',chat_id='test-chat',limit=100,cursor=cursor))
            pages+=1;rows+=page['rows']
            self.assertTrue(all(row['content_complete'] for row in page['rows']))
            cursor=page['next_cursor']
            if not cursor: break
        self.assertGreater(pages,1)
        self.assertEqual(len(rows),12)
        self.assertEqual(len({row['message_ref'] for row in rows}),12)
        self.assertTrue(all(len(row['text'])>900 for row in rows[:-2]))

    def test_list_budget_paginates_without_losing_candidates(self):
        label='名'*280
        for index in range(15):
            username=f'person-{index}'
            table='Msg_'+hashlib.md5(username.encode()).hexdigest()
            self.update(0, f'INSERT INTO contact VALUES({sql_text(username)},{sql_text(label)},{sql_text(label)},{sql_text(label)});')
            self.update(-1, f'INSERT INTO Name2Id VALUES({sql_text(username)}); CREATE TABLE "{table}" AS SELECT * FROM "{self.f.table}";')
        reader=self.ready();seen=set();cursor=None;pages=0
        while True:
            page=self.result(reader.call('chats',limit=100,cursor=cursor))
            pages+=1
            for row in page['rows']:
                self.assertNotIn(row['chat_id'],seen)
                seen.add(row['chat_id'])
                if row['chat_id'].startswith('person-'):
                    self.assertTrue(row['labels_truncated'])
            cursor=page['next_cursor']
            if not cursor:break
        self.assertEqual(len(seen),16)
        self.assertGreater(pages,1)

    def test_oversized_database_value_is_not_fetched_or_disguised_as_complete(self):
        snapshot=self.f.load(self.f.refresh())
        header={'database':self.f.records[-1]['database'],'chat_id':'test-chat','local_id':1}
        with patch.object(read_chat,'query',return_value=[{'storage':'text','bytes':read_chat.MAX_MESSAGE_BYTES+1,'content_hex':None}]) as sql, patch.object(read_chat,'decode') as decoder:
            with self.assertRaises(ConnectorError) as caught:
                read_chat.read_message(snapshot,header)
            self.assertEqual(caught.exception.code,'MESSAGE_TOO_LARGE')
            self.assertIn('CASE WHEN',sql.call_args.args[2])
            decoder.assert_not_called()
        reader=ReaderSession(self.f.keyfile,self.f.output)
        with patch('wechat_connector.server.read_message',side_effect=ConnectorError('MESSAGE_TOO_LARGE','size limit')):
            page=self.result(reader.call('history',chat_id='test-chat'))
            self.assertFalse(page['rows'][0]['content_complete'])
            self.assertEqual(page['rows'][0]['status'],'MESSAGE_TOO_LARGE')
            detail=reader.call('message',message_ref=page['rows'][0]['message_ref'])
            self.assertTrue(detail.is_error)
            self.assertEqual(detail.structured_content['code'],'MESSAGE_TOO_LARGE')
            self.assertLessEqual(result_bytes(detail),MAX_RESULT_BYTES)

    def test_single_large_message_chunk_roundtrip_and_independent_history_cursor(self):
        body='start|'+('字🙂\\"\n'*9000)+'|end'
        self.update(-1,f'UPDATE "{self.f.table}" SET message_content={sql_text(body)},server_id=9223372036854775807;')
        reader=self.ready()
        with patch.object(read_chat,'decode',wraps=read_chat.decode) as decoder:
            page=self.result(reader.call('history',chat_id='test-chat',limit=100))
            self.assertEqual(decoder.call_count,1)  # No eager body reads from the older shard.
        self.assertEqual(len(page['rows']),1)
        message=page['rows'][0]
        self.assertFalse(message['content_complete'])
        self.assertTrue(page['has_more'])
        cursor=message['next_content_cursor'];parts=[message['text']];offset=len(parts[0])
        while cursor:
            detail=self.result(reader.call('message',message_ref=message['message_ref'],cursor=cursor))
            self.assertEqual(detail['message']['content_offset'],offset)
            parts.append(detail['message']['text']);offset+=len(parts[-1]);cursor=detail['next_cursor']
            self.assertEqual(detail['source']['server_id'],'9223372036854775807')
        self.assertEqual(''.join(parts),body)
        older=self.result(reader.call('history',chat_id='test-chat',cursor=page['next_cursor']))
        self.assertEqual(len(older['rows']),1)
        bad=reader.call('message',message_ref=older['rows'][0]['message_ref'],cursor=message['next_content_cursor'])
        self.assertEqual(bad.structured_content['code'],'INVALID_CURSOR')
        if shutil.which('node'):
            subprocess.run(['node','-e',"const d=JSON.parse(process.argv[1]);if(d.id!=='9223372036854775807')process.exit(1)",json.dumps({'id':detail['source']['server_id']})],check=True)

    def test_reference_expiry_reuse_and_key_recheck(self):
        reader=self.ready()
        page=self.result(reader.call('history',chat_id='test-chat'))
        ref=page['rows'][0]['message_ref']
        repeated=self.result(reader.call('history',chat_id='test-chat'))
        self.assertEqual(repeated['rows'][0]['message_ref'],ref)
        header=dict(reader.references[ref][1]);sid=page['snapshot_id']
        for i in range(4097):
            reader.reference({**header,'local_id':10000+i},sid)
        self.assertEqual(len(reader.references),4096)
        self.assertEqual(reader.call('message',message_ref=ref).structured_content['code'],'MESSAGE_REF_EXPIRED')
        fresh=ReaderSession(self.f.keyfile,self.f.output)
        self.assertEqual(fresh.call('message',message_ref=ref).structured_content['code'],'MESSAGE_REF_EXPIRED')
        self.f.keyfile.unlink()
        self.assertEqual(fresh.call('message',message_ref=ref).structured_content['code'],'KEYS_NOT_FOUND')

    def test_sanitized_details_statuses_and_old_cursor_rejection(self):
        file=app(6,'notes.pdf',f'<appattach><totallen>12</totallen><aeskey>{SECRET}</aeskey><fileuploadtoken>{SECRET}</fileuploadtoken></appattach>')
        self.update(-1,f'UPDATE "{self.f.table}" SET local_type=49,message_content={sql_text(file)};')
        reader=self.ready()
        page=self.result(reader.call('history',chat_id='test-chat'))
        message=page['rows'][0]
        detail=self.result(reader.call('message',message_ref=message['message_ref']))
        self.assertEqual(detail['message']['text'],message['text'])
        self.assertNotIn(SECRET,json.dumps(detail))
        self.assertNotIn('<appmsg',json.dumps(detail))
        old=__import__('base64').urlsafe_b64encode(json.dumps({'scope':[],'after':[]}).encode()).decode()
        self.assertEqual(reader.call('history',chat_id='test-chat',cursor=old).structured_content['code'],'CURSOR_VERSION_MISMATCH')

    def test_held_out_mixed_messages_via_sdk_and_large_validation_errors(self):
        # Independent acceptance examples, not the unit parser examples above.
        samples=[(10000,'Bob invited Carol'),(49,app(5,'Study B',f'<url>https://example.net/p?auth={SECRET}</url>')),
                 (49,app(57,'Agreed', '<refermsg><type>1</type><content>Use method B</content></refermsg>')),
                 (49,'<invalid'),(47,f'<msg><emoji desc="smile" aeskey="{SECRET}"/></msg>')]
        for i,(kind,content) in enumerate(samples,2):
            self.update(-1,f'INSERT INTO "{self.f.table}" VALUES({i},2,{kind},30,2,2,{sql_text(content)});')
        self.f.refresh()
        async def run():
            async with Client(create_server(keys_path=self.f.keyfile,snapshots_root=self.f.output)) as client:
                page=await client.call_tool('wechat_get_chat_history',{'chat_id':'test-chat','limit':100})
                data=self.result(page)
                self.assertEqual(len(data['rows']),7)
                self.assertIn('system',{r['kind'] for r in data['rows']})
                self.assertIn('MESSAGE_PARSE_FAILED',{r['status'] for r in data['rows']})
                self.assertNotIn(SECRET,page.model_dump_json())
                self.assertNotIn('Use method B',page.content[0].text)
                self.assertTrue(any('Use method B' in r['text'] for r in data['rows']))
                for args in [{'query':SECRET*10000},{'limit':100000},{'unexpected':SECRET}]:
                    error=await client.call_tool('wechat_list_chats',args)
                    self.assertTrue(error.is_error)
                    self.assertLessEqual(result_bytes(error),MAX_RESULT_BYTES)
                    self.assertNotIn(SECRET,error.model_dump_json())
        asyncio.run(run())


if __name__=='__main__':
    unittest.main()
