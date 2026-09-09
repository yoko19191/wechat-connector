"""Local-only replay metrics. Never prints or exports chat text, identities or credentials."""

import argparse
import base64
import importlib.metadata
import json
import math
from pathlib import Path
import time

from mcp.types import CallToolResult, TextContent

from wechat_connector.message_content import normalize
from wechat_connector.read_chat import all_chats, history_candidates, history_order, load_snapshot
from wechat_connector.server import MAX_RESULT_BYTES, ReaderSession, result_bytes


def audit(queries, limit, encoding):
    import tiktoken
    tokenizer = tiktoken.get_encoding(encoding)
    tokens = lambda text: len(tokenizer.encode(text, disallowed_special=()))
    dumps = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    snapshot = load_snapshot()
    baseline_lookup_pages = math.ceil(len(all_chats(snapshot))/100)
    reader = ReaderSession()
    cases = []
    for index, query in enumerate(queries, 1):
        old_chat = all_chats(snapshot, query_text=query)
        if len(old_chat) != 1:
            raise ValueError('Replay requires a uniquely matched conversation')
        chat = old_chat[0]
        raw = history_candidates(snapshot, chat['chat_id'], limit+1, chat['account'])
        more = len(raw) > limit
        raw = raw[:limit]
        started = time.perf_counter()
        lookup = reader.call('chats', query=query, limit=5)
        if lookup.is_error or len(lookup.structured_content['rows']) != 1:
            raise ValueError('Name lookup failed')
        sid = lookup.structured_content['snapshot_id']
        old_cursor = (base64.urlsafe_b64encode(dumps({'scope':[sid,'history',chat['account'],chat['chat_id']],
                      'after':history_order(raw[-1])}).encode()).decode() if more else None)
        old = {'snapshot_id':sid,'snapshot_created_at':snapshot['created_at'],'live':False,
               'untrusted_chat_data':True,'rows':raw,'has_more':more,'next_cursor':old_cursor,
               'time_range':{'start_time':None,'end_time':None}}
        legacy = CallToolResult(is_error=False, structured_content=old,
                                content=[TextContent(type='text',text=json.dumps(old,ensure_ascii=False))])
        expected = {(r['database'],r['local_id']):normalize(r['local_type'],r['message_content']) for r in raw}
        if any(row['status'] != 'ok' for row in expected.values()):
            raise ValueError('Cannot claim semantic preservation for unsupported replay content')
        results=[];found=set();cursor=None
        while len(found)<len(raw):
            result=reader.call('history',chat_id=chat['chat_id'],account=chat['account'],
                               limit=min(limit,len(raw)-len(found)),cursor=cursor)
            if result.is_error:raise ValueError('History replay failed')
            results.append(result)
            page=result.structured_content
            if not page['rows']:raise ValueError('Replay ended early')
            for message in page['rows']:
                locator=reader.references[message['message_ref']][1]
                identity=(locator['database'],locator['local_id'])
                parts=[message['text']]
                content_cursor=message.get('next_content_cursor')
                while content_cursor:
                    detail=reader.call('message',message_ref=message['message_ref'],cursor=content_cursor)
                    if detail.is_error:raise ValueError('Detail replay failed')
                    results.append(detail)
                    parts.append(detail.structured_content['message']['text'])
                    content_cursor=detail.structured_content['next_cursor']
                if identity in found or ''.join(parts)!=expected[identity]['text']:
                    raise ValueError('Replay changed, omitted or duplicated readable content')
                if message.get('reply_to')!=expected[identity].get('reply_to'):
                    raise ValueError('Replay changed the direct quotation metadata')
                found.add(identity)
            cursor=page['next_cursor']
            if not page['has_more']:break
        if found!=set(expected):raise ValueError('Replay coverage mismatch')
        if any(result_bytes(r)>MAX_RESULT_BYTES or len(r.content[0].text)>200 for r in [lookup,*results]):
            raise ValueError('Response budget violated')
        new_json=[dumps(r.structured_content) for r in results]
        cases.append({'case':index,'rows_preserved':len(found),'lookup_calls':1,'history_and_detail_calls':len(results),
            'old_structured_bytes':len(dumps(old).encode()),'new_structured_bytes':sum(len(x.encode()) for x in new_json),
            'old_structured_tokens':tokens(dumps(old)),'new_structured_tokens':sum(tokens(x) for x in new_json),
            'old_complete_result_bytes':result_bytes(legacy),'new_complete_result_bytes':sum(result_bytes(r) for r in results),
            'max_new_result_bytes':max(result_bytes(r) for r in [lookup,*results]),
            'new_complete_result_tokens':sum(tokens(r.model_dump_json(by_alias=True)) for r in results),
            'elapsed_seconds':round(time.perf_counter()-started,3),'content_preserved':True})
    old_bytes=sum(c['old_structured_bytes'] for c in cases)
    new_bytes=sum(c['new_structured_bytes'] for c in cases)
    reduction=round(100*(1-new_bytes/old_bytes),2) if old_bytes else 0
    return {'tokenizer':f'tiktoken {importlib.metadata.version("tiktoken")} / {encoding}',
            'token_note':'Local comparison encoding; not asserted to be the active model tokenizer.',
            'baseline_lookup_full_scan_pages':baseline_lookup_pages,'new_lookup_calls':len(queries),
            'cases':cases,'structured_bytes_reduction_percent':reduction,'target_60_percent_met':reduction>=60,
            'chat_content_exported':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query',action='append',required=True)
    parser.add_argument('--limit',type=int,default=50)
    parser.add_argument('--encoding',default='o200k_base',choices=['o200k_base','cl100k_base'])
    args=parser.parse_args()
    if not 1<=args.limit<=100:parser.error('limit must be 1..100')
    try:
        result=audit(args.query,args.limit,args.encoding)
    except Exception:
        parser.exit(2,'Replay failed; private query, content and diagnostic details withheld.\n')
    print(json.dumps(result,indent=2))
