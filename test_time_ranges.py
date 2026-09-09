"""Time-window semantics for the shared reader, CLI and MCP (synthetic data)."""

import asyncio
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from mcp import Client
import test_runtime as fixtures
from wechat_connector import read_chat
from wechat_connector.errors import ConnectorError
from wechat_connector.server import ReaderSession, create_server


def at(second):
    return f'1970-01-01T00:00:{second:02d}Z'


class TimeRangeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RuntimeTests('test_runtime_has_no_capture_dependency')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def snapshot(self):
        return self.fixture.load(self.fixture.refresh())

    def test_timezone_validation_and_exact_subsecond_bounds(self):
        utc = read_chat.parse_time_range(at(10), at(20))
        offset = read_chat.parse_time_range('1970-01-01T08:00:10+08:00','1970-01-01T08:00:20+08:00')
        self.assertEqual(utc, offset)
        self.assertEqual((utc['start_seconds'],utc['end_seconds']), (10,20))
        for start,end in [(at(20),at(10)), (at(10),at(10)), ('1970-01-01T00:00:10',None),
                          ('1970-01-01',None), ("' OR 1=1--",None), (42,None)]:
            with self.assertRaises(ConnectorError) as caught:
                read_chat.parse_time_range(start,end)
            self.assertEqual(caught.exception.code,'INVALID_TIME_RANGE')
        fractional = read_chat.parse_time_range('1970-01-01T00:00:10.000001Z','1970-01-01T00:00:10.999999Z')
        self.assertEqual((fractional['start_seconds'],fractional['end_seconds']), (11,11))
        before_epoch = read_chat.parse_time_range('1969-12-31T23:59:59.5Z',at(1))
        self.assertEqual(before_epoch['start_seconds'],0)

    def test_inclusive_start_exclusive_end_open_bounds_and_empty_ranges(self):
        s = self.snapshot()
        def times(**options):
            return [row['create_time'] for row in read_chat.messages(s,'test-chat',10,**options)]
        self.assertEqual(times(), [20,10])
        self.assertEqual(times(start_time=at(10),end_time=at(20)),[10])
        self.assertEqual(times(start_time=at(20)),[20])
        self.assertEqual(times(end_time=at(20)),[10])
        self.assertEqual(times(start_time=at(11),end_time=at(19)),[])
        self.assertEqual(times(start_time='1970-01-01T00:00:10.000001Z',end_time='1970-01-01T00:00:10.999999Z'),[])

    def test_sql_filters_before_limit_and_pagination_binds_normalized_window(self):
        for index,record in enumerate(self.fixture.records[-2:]):
            db = fixtures.FixtureDB(self.fixture.source/record['database'],bytes.fromhex(record['key']))
            db.execute(f'''INSERT INTO "{self.fixture.table}" VALUES(2,2,1,25,1,2,'inside'),
                (3,3,1,30,1,2,'exclusive end'),(4,4,1,20,1,2,'inclusive start');''')
            db.close()
        self.snapshot()
        reader = ReaderSession(self.fixture.keyfile,self.fixture.output)
        bounds={'start_time':at(20),'end_time':at(30)}
        first=reader.call('history',chat_id='test-chat',limit=1,**bounds).structured_content
        rows=list(first['rows']);cursor=first['next_cursor']
        self.assertEqual(first['time_range'],bounds)
        # Equivalent timezone spellings are the same filter, so the cursor remains valid.
        equivalent={'start_time':'1970-01-01T08:00:20+08:00','end_time':'1970-01-01T08:00:30+08:00'}
        while cursor:
            result=reader.call('history',chat_id='test-chat',limit=2,cursor=cursor,**equivalent)
            self.assertFalse(result.is_error)
            page=result.structured_content;rows+=page['rows'];cursor=page['next_cursor']
        self.assertEqual(len(rows),5)
        self.assertEqual(len({(r['database'],r['local_id']) for r in rows}),5)
        self.assertTrue(all(20<=r['create_time']<30 for r in rows))
        self.assertEqual(rows[0]['create_time'],25)
        for changes in ({'start_time':at(21),'end_time':at(30)},{}):
            result=reader.call('history',chat_id='test-chat',cursor=first['next_cursor'],**changes)
            self.assertEqual(result.structured_content['code'],'INVALID_CURSOR')
        # The old message must still be returned even with newer rows in its shard.
        result=reader.call('history',chat_id='test-chat',limit=1,start_time=at(0),end_time=at(20))
        self.assertEqual([r['create_time'] for r in result.structured_content['rows']],[10])

    def test_cli_time_filters_and_rejects_filters_without_conversation(self):
        s=self.snapshot()
        output=io.StringIO()
        with patch.object(read_chat,'load_snapshot',return_value=s),contextlib.redirect_stdout(output):
            read_chat.main(['--chat','test-chat','--start-time',at(10),'--end-time',at(20)])
        result=json.loads(output.getvalue())
        self.assertEqual([r['create_time'] for r in result['rows']],[10])
        self.assertEqual(result['time_range'],{'start_time':at(10),'end_time':at(20)})
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as caught:
            read_chat.main(['--start-time',at(10)])
        self.assertEqual(caught.exception.code,2)

    def test_mcp_schema_and_errors(self):
        self.snapshot()
        async def scenario():
            async with Client(create_server(keys_path=self.fixture.keyfile,snapshots_root=self.fixture.output)) as client:
                tools=(await client.list_tools()).tools
                history=next(t for t in tools if t.name=='wechat_get_chat_history')
                self.assertIn('start_time',history.input_schema['properties'])
                self.assertIn('end_time',history.input_schema['properties'])
                result=await client.call_tool('wechat_get_chat_history',{'chat_id':'test-chat','start_time':at(10),'end_time':at(20)})
                self.assertFalse(result.is_error)
                self.assertEqual([r['create_time'] for r in result.structured_content['rows']],[10])
                result=await client.call_tool('wechat_get_chat_history',{'chat_id':'test-chat','start_time':'2026-09-01'})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['code'],'INVALID_TIME_RANGE')
        asyncio.run(scenario())


if __name__=='__main__':
    unittest.main()
