"""Idempotent, non-sending migration. Legacy sources remain untouched."""
import base64
import copy
import json
import uuid

from .github_api import GitHubAPIError
from .legacy_plan import validate_state
from .state_store import STATE_BRANCH
from .task_queue import TaskError, TaskStore, new_task
from .time_utils import utc_now


def read(api,path):
    try:
        response=api.request('GET',f'/repos/{api.repo}/contents/{path}?ref={STATE_BRANCH}')
    except GitHubAPIError as exc:
        if exc.status==404:return None
        raise
    if isinstance(response,list):return response
    try:return json.loads(base64.b64decode(response['content']))
    except (ValueError,KeyError,TypeError):raise TaskError('旧状态无法解析；迁移已停止，不覆盖源文件。') from None


def migrate(api,*,dry_run=False):
    store=TaskStore(api)
    current=store.load()[0]
    if current.get('legacy_migrated'):
        return {'imported':0,'statuses':{},'source_files_preserved':True,'sends':0}
    imported=[]
    old=read(api,'.mail-bot/random.json')
    if old is not None:
        validate_state(old)
        rid=str(uuid.uuid5(uuid.NAMESPACE_URL,f'mail-bot:{api.repo.casefold()}:legacy-random'))
        status={'scheduled':'queued','sending':'uncertain','completed':'accepted',
                'paused':'uncertain','disabled':'cancelled'}[old['state']]
        if not old['enabled'] and status=='queued':status='cancelled'
        repeat={'kind':'random','min_seconds':old['min_minutes']*60,'max_seconds':old['max_minutes']*60} if old['mode']=='repeat' else None
        task=new_task(rid,{'kind':'dragon'},old.get('next_send_at'),repeat,status=status)
        task['legacy']={'source':'.mail-bot/random.json','state':copy.deepcopy(old)}
        for entry in old.get('history',[]):
            task['history'].append({**entry,'sequence':0})
        task['history']=task['history'][-20:]
        if old.get('last_send',{}):
            task['last_result']=old['last_send']['status']
            if old['last_send'].get('time_source')=='smtp_accepted':
                task['accepted_at']=old['last_send']['occurred_at']
        imported.append(task)
    files=read(api,'.mail-bot/image-requests') or []
    if not isinstance(files,list):raise TaskError('旧图片请求目录格式无效。')
    for file in files:
        if file.get('type')!='file' or not file.get('name','').endswith('.json'):continue
        old=read(api,'.mail-bot/image-requests/'+file['name'])
        rid=old['id']
        status={'accepted':'accepted','failed':'failed','uncertain':'uncertain','claimed':'uncertain',
                'upload_failed':'failed'}.get(old['status'],'blocked')
        task=new_task(rid,{'kind':'comic','image_request_id':rid},old.get('created_at') or None,status=status)
        task['legacy']={'source':'.mail-bot/image-requests/'+file['name'],'state':copy.deepcopy(old)}
        task['reason']='legacy_payload_unavailable' if status=='blocked' else 'legacy_imported'
        task['cleanup']=old.get('cleanup','pending')
        if old.get('accepted_at'):task['accepted_at']=old['accepted_at']
        if old.get('run_id'):
            task['history']=[{'run_id':old['run_id'],'attempt':int(old.get('run_attempt',1)),
                              'sequence':0,'status':status,'occurred_at':old.get('accepted_at') or old.get('finished_at') or old['created_at'],
                              'time_source':'smtp_accepted' if old.get('accepted_at') else 'result_recorded'}]
        imported.append(task)
    existing=current['tasks']
    fresh=[task for task in imported if task['id'] not in existing]
    if not dry_run:
        def change(tasks):
            for task in fresh:tasks.setdefault(task['id'],task)
            return len(fresh)
        store.update(change,metadata={'legacy_migrated':True})
    return {'imported':len(fresh),'statuses':{s:sum(t['status']==s for t in fresh) for s in {t['status'] for t in fresh}},
            'source_files_preserved':True,'sends':0}
