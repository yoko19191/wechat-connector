"""Deterministic, allow-listed projections of WeChat message contents for agents."""

from datetime import datetime, timezone
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import xml.etree.ElementTree as ET



class InvalidMessage(ValueError):
    pass


def timestamp(value):
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat().replace('+00:00', 'Z')
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def xml_root(content):
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', content, re.I):
        raise InvalidMessage()
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        raise InvalidMessage() from None


def leaf(node, path):
    child = node.find(path)
    if child is None:
        return ''
    if len(child):
        raise InvalidMessage()  # Do not flatten arbitrary nested credential fields.
    return child.text or ''


def number(value):
    return str(int(value)) if isinstance(value, str) and re.fullmatch(r'\d{1,20}', value) else None


def clean_url(value):
    if not value:
        return '', False
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
            return '', True
        host = parts.hostname.lower()
        netloc = f'[{host}]' if ':' in host else host
        if parts.port is not None:
            netloc += f':{parts.port}'
        allowed = {'__biz': r'[A-Za-z0-9+/=]{1,256}', 'mid': r'\d{1,20}',
                   'idx': r'\d{1,6}', 'sn': r'[0-9A-Fa-f]{32}'}
        params = []
        if host == 'mp.weixin.qq.com' and parts.path == '/s':
            for key, item in parse_qsl(parts.query, max_num_fields=1000):
                if key in allowed and re.fullmatch(allowed[key], item):
                    params.append((key, item))
        result = urlunsplit((parts.scheme.lower(), netloc, parts.path, urlencode(params), ''))
        return result, result != value
    except ValueError:
        return '', True


def normalize(local_type, content, *, include_quote=True):
    """text is verbatim for text messages; protocol XML is never passed through."""
    try:
        base = int(local_type) & 0xffffffff
    except (ValueError, TypeError):
        return {'kind': 'unsupported', 'text': '[无效的消息类型]', 'status': 'UNSUPPORTED_MESSAGE'}
    content = content or ''
    if base == 1:
        return {'kind': 'text', 'text': content, 'status': 'ok'}
    if base in (10000, 10002) and not content.lstrip().startswith('<'):
        return {'kind': 'system', 'text': content, 'status': 'ok'}
    kinds = {3: 'image', 34: 'voice', 43: 'video', 62: 'video', 47: 'emoji',
             49: 'app', 10000: 'system', 10002: 'system'}
    if base not in kinds:
        return {'kind': 'unsupported', 'text': '[暂不支持的消息类型]', 'status': 'UNSUPPORTED_MESSAGE'}
    try:
        root = xml_root(content)
        if base == 49:
            app = root if root.tag == 'appmsg' else root.find('appmsg')
            if app is None:
                raise InvalidMessage()
            subtype = leaf(app, 'type')
            title = leaf(app, 'title')
            if subtype == '57':
                result = {'kind': 'reply', 'text': title, 'status': 'ok'}
                quote = app.find('refermsg') if include_quote else None
                if quote is not None:
                    quoted_type = number(leaf(quote, 'type'))
                    quoted = (normalize(int(quoted_type), leaf(quote, 'content'), include_quote=False)
                              if quoted_type else {'text': '[未知引用类型]', 'status': 'UNSUPPORTED_MESSAGE'})
                    # Display name is human-readable text, not a recursively rendered subtree.
                    label = leaf(quote, 'displayname')
                    result['text'] += '\n\n引用' + (f'（{label}）' if label else '') + '：\n' + quoted['text']
                    sid = number(leaf(quote, 'svrid'))
                    result['reply_to'] = {'server_id': sid, 'time': timestamp(leaf(quote, 'createtime')),
                                          'status': quoted['status']}
                return result
            if subtype == '6':
                size = number(leaf(app, 'appattach/totallen'))
                extension = leaf(app, 'appattach/fileext')
                text = '[文件]\n名称：' + title
                if extension:
                    text += '\n类型：' + extension
                if size:
                    text += '\n大小：' + size + ' 字节'
                return {'kind': 'file', 'text': text, 'status': 'ok'}
            if subtype == '5':
                url, redacted = clean_url(leaf(app, 'url'))
                text = title
                description = leaf(app, 'des')
                if description:
                    text += '\n' + description
                if url:
                    text += '\n链接：' + url
                return {'kind': 'link', 'text': text, 'status': 'ok', 'link_sanitized': redacted}
            return {'kind': 'unsupported', 'text': '[暂不支持的应用消息]', 'status': 'UNSUPPORTED_MESSAGE'}
        if base in (10000, 10002):
            text = leaf(root, 'revokemsg/replacemsg') or leaf(root, 'sysmsgtemplate/content_template/plain')
            if not text:
                return {'kind': 'system', 'text': '[未解析的系统消息]', 'status': 'UNSUPPORTED_MESSAGE'}
            return {'kind': 'system', 'text': text, 'status': 'ok'}
        tag = {3: 'img', 34: 'voicemsg', 43: 'videomsg', 62: 'videomsg', 47: 'emoji'}[base]
        node = root if root.tag == tag else root.find(tag)
        if node is None:
            raise InvalidMessage()
        label = {3: '图片', 34: '语音', 43: '视频', 62: '视频', 47: '表情'}[base]
        text = '[' + label + ']'
        if base == 3:
            width, height = number(node.get('cdnthumbwidth')), number(node.get('cdnthumbheight'))
            if width and height:
                text += f'\n缩略图尺寸：{width}×{height}'
        duration = number(node.get('voicelength' if base == 34 else 'playlength'))
        if duration and base in (34, 43, 62):
            text += '\n时长：' + duration + (' 毫秒' if base == 34 else ' 秒')
        description = node.get('desc') if base == 47 else None
        if description:
            text += '\n说明：' + description
        return {'kind': kinds[base], 'text': text, 'status': 'ok'}
    except (InvalidMessage, ValueError, TypeError, RecursionError):
        return {'kind': kinds[base], 'text': '[消息内容无法解析]', 'status': 'MESSAGE_PARSE_FAILED'}
