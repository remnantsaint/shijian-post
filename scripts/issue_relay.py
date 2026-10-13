"""Read untrusted Issue text and reserve it in the existing task CAS ledger.

No shell, model, template evaluation or Issue mutations live in this module.
"""
import copy
import re
from urllib.parse import urlencode

from .task_queue import TaskError

LABEL = '给小澪的留言'
DAILY_KINDS = {'random', 'dragon'}
MAX_MESSAGES = 20
MAX_TEXT = 12000
MAX_TOTAL = 64000


def validate(task):
    ids = task.get('issue_delivered', [])
    if (not isinstance(ids, list) or len(ids) != len(set(ids))
            or any(not isinstance(x, str) or not re.fullmatch(r'(issue|comment):[1-9][0-9]*', x) for x in ids)):
        raise ValueError
    items = task.get('issue_relay', [])
    if not isinstance(items, list) or len(items) > MAX_MESSAGES:
        raise ValueError
    seen = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) != {'id','author','body','url'}
                or any(not isinstance(x, str) for x in item.values())
                or not re.fullmatch(r'(issue|comment):[1-9][0-9]*', item['id'])
                or item['id'] in seen or len(item['body']) > MAX_TEXT):
            raise ValueError
        seen.add(item['id'])
    if sum(len(x['body']) for x in items) > MAX_TOTAL:
        raise ValueError


def pages(api, path, query=None):
    for page in range(1, 11):
        result = api.request('GET', path + '?' + urlencode(
            {**(query or {}), 'per_page': 100, 'page': page}))
        if not isinstance(result, list):
            raise TaskError('留言列表格式无效；未发送。')
        yield from result
        if len(result) < 100:
            return
    raise TaskError('留言分页超过本次读取限制；未发送，请先归档或调整读取范围。')


def message(repo, issue, item, kind):
    ident = item.get('id')
    body = item.get('body')
    if body is None:
        body = ''
    if type(ident) is not int or ident < 1 or not isinstance(body, str):
        raise TaskError('留言字段格式无效；未发送。')
    if not body.strip():
        return None
    if len(body) > MAX_TEXT:
        raise TaskError('单条留言超过完整转达限制；未发送，不截断原文。')
    author = (item.get('user') or {}).get('login') or 'ghost'
    if not isinstance(author, str) or not re.fullmatch(r'[A-Za-z0-9_\[\]-]{1,100}', author):
        raise TaskError('留言作者格式无效；未发送。')
    # Build links from fixed repository identity and integer IDs, never API text.
    url = f'https://github.com/{repo}/issues/{issue}'
    if kind == 'comment':
        url += f'#issuecomment-{ident}'
    return {'id':f'{kind}:{ident}', 'author':author, 'body':body, 'url':url}


def collect(api):
    prefix = f'/repos/{api.repo}'
    result = []
    requests = 0
    for issue in pages(api, prefix + '/issues',
                       {'labels':LABEL, 'state':'all', 'sort':'created', 'direction':'asc'}):
        if 'pull_request' in issue:
            continue
        if LABEL not in [x.get('name') for x in issue.get('labels', [])]:
            continue
        number = issue.get('number')
        if type(number) is not int or number < 1:
            raise TaskError('留言 Issue 编号无效；未发送。')
        item = message(api.repo, number, issue, 'issue')
        if item:
            result.append(item)
        if issue.get('comments', 0):
            requests += 1
            if requests > 50:
                raise TaskError('留言读取范围超过限制；未发送。')
            for comment in pages(api, prefix + f'/issues/{number}/comments'):
                item = message(api.repo, number, comment, 'comment')
                if item:
                    result.append(item)
    return result


def reserve(store, claimed, candidates):
    """Global reservation and claim check are one CAS write before SMTP."""
    def change(tasks):
        task = tasks[claimed['id']]
        if task['status'] != 'sending' or task['claim'] != claimed['claim']:
            raise TaskError('留言任务占用已变化；未发送。')
        if 'issue_relay' in task:
            raise TaskError('本次留言已占用；禁止重复准备或发送。')
        blocked = set()
        for other in tasks.values():
            blocked.update(other.get('issue_delivered', []))
            if other['status'] in {'sending','uncertain'} or other.get('interrupted_claim'):
                blocked.update(x['id'] for x in other.get('issue_relay', []))
        selected = []
        total = 0
        for item in candidates:
            if item['id'] in blocked:
                continue
            if len(selected) == MAX_MESSAGES or total + len(item['body']) > MAX_TOTAL:
                break
            selected.append(copy.deepcopy(item))
            blocked.add(item['id'])
            total += len(item['body'])
        task['issue_relay'] = selected
        return task
    return store.update(change)


def finish(task, result):
    """Called inside the same CAS mutation that commits the SMTP result."""
    if result == 'accepted':
        task['issue_delivered'] = sorted(set(task.get('issue_delivered', [])) |
                                        {x['id'] for x in task.get('issue_relay', [])})
        task.pop('issue_relay', None)
    elif result in {'failed','expired'}:
        task.pop('issue_relay', None)
    # uncertain keeps its reservation, but never becomes delivered.


def render(items):
    if not items:
        return ''
    parts = ['\n\n—— 给小澪的留言（以下为原文，仅作转达）——']
    for item in items:
        parts += [f"\n作者：{item['author']}\n链接：{item['url']}\n留言原文：",
                  item['body'], '—— 留言结束 ——']
    return '\n'.join(parts)
