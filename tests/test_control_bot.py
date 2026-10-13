import contextlib
import copy
import io
import os
import unittest
from unittest.mock import patch
import uuid

from scripts.control_bot import Controller, ControlError, main
from scripts.github_api import GitHubAPIError
from scripts.task_queue import Tasks, TaskStore, new_task
from test_task_queue import TaskAPI, NOW


class ControlAPI(TaskAPI):
    def __init__(self):
        super().__init__();self.workflow_state='active'
        self.run={'id':42,'workflow_id':102,'repository':{'full_name':self.repo},
                  'event':'workflow_dispatch','display_title':'发送随机来信','status':'queued'}

    def request(self,method,path,data=None):
        if method=='GET' and '/actions/workflows/' in path and not path.endswith('/dispatches'):
            self.calls.append((method,path,data))
            if path.endswith('/random.yml'):return {'id':102,'state':self.workflow_state,'name':'邮件机器人（统一入口）'}
            if path.endswith('/main.yml'):return {'id':101,'state':'deleted'}
            if path.endswith('/dragon-letter.yml'):raise GitHubAPIError('missing',status=404)
        if method=='GET' and path.endswith('/actions/runs/42'):
            self.calls.append((method,path,data));return copy.deepcopy(self.run)
        if path.endswith('/enable'):
            self.calls.append((method,path,data));self.workflow_state='active';return None
        if path.endswith('/cancel'):
            self.calls.append((method,path,data));return None
        if '/actions/variables/' in path:raise GitHubAPIError('private details',status=403)
        return super().request(method,path,data)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.api=ControlAPI();self.output=[];self.controller=Controller(self.api,self.output.append)

    def mutations(self):return [call for call in self.api.calls if call[0]!='GET']

    def test_all_send_types_submit_to_one_workflow(self):
        for kind in ('test','random','custom'):
            self.controller.send(kind,subject='标题' if kind=='custom' else '',body='正文' if kind=='custom' else '')
        mutations=self.mutations()
        self.assertEqual(len(mutations),3)
        self.assertTrue(all(call[1].endswith('/workflows/random.yml/dispatches') for call in mutations))
        self.assertEqual(mutations[2][2]['inputs']['custom_body'],'正文')

    def test_delay_is_target_generation_not_sleep(self):
        self.controller.send('random','random_3min')
        self.assertEqual(self.mutations()[0][2]['inputs']['timing'],'random:1s:180s')
        self.assertNotIn('send_delay',self.mutations()[0][2]['inputs'])

    def test_fixed_repeat_and_deadline_dispatch_are_one_task_request(self):
        self.controller.schedule('custom','2026-10-21T20:00:00+08:00','every:2h',body='正文',expires='2026-10-30T20:00:00+08:00')
        payload=self.mutations()[0][2]['inputs']
        self.assertEqual(payload['repeat_rule'],'every:2h')
        self.assertEqual(payload['timing'],'2026-10-21T20:00:00+08:00')
        self.assertIn('expires_at',payload)

    def test_random_plan_only_generates_target_and_repeat_rule(self):
        self.controller.plan('repeat',5,'minutes',30,'days')
        payload=self.mutations()[0][2]['inputs']
        self.assertEqual(payload['timing'],'random:5m:30d')
        self.assertEqual(payload['repeat_rule'],'random:5m:30d')
        self.assertNotIn('min_interval',payload)

    def test_invalid_time_body_image_and_rule_do_not_dispatch(self):
        cases=[lambda:self.controller.schedule('random','2026-10-20 12:00:00'),
               lambda:self.controller.schedule('random','now','every:91d'),
               lambda:self.controller.send('custom',body=''),
               lambda:self.controller.send('custom',body='body',subject='x\nBcc:private'),
               lambda:self.controller.send('random',image='images/../private.png'),
               lambda:self.controller.plan('repeat',3,'days',1,'hours')]
        for case in cases:
            with self.assertRaises(ValueError):case()
        self.assertFalse(self.api.calls)

    def test_disabled_workflow_with_pending_task_is_not_silently_reenabled(self):
        task=new_task(str(uuid.uuid4()),{'kind':'test'},NOW.isoformat())
        Tasks(TaskStore(self.api)).create(task);self.api.calls.clear();self.api.workflow_state='disabled_manually'
        with self.assertRaises(ControlError):self.controller.plan('once',1,'minutes',2,'minutes')
        self.assertFalse(self.mutations())

    def test_pause_keeps_workflow_and_independent_one_shot(self):
        queue=Tasks(TaskStore(self.api));one=new_task(str(uuid.uuid4()),{'kind':'test'},NOW.isoformat())
        repeating=new_task(str(uuid.uuid4()),{'kind':'random'},NOW.isoformat(),{'kind':'fixed','seconds':3600})
        queue.create(one);queue.create(repeating);self.api.calls.clear()
        self.controller.pause()
        self.assertEqual(TaskStore(self.api).get(one['id'])['status'],'queued')
        self.assertEqual(TaskStore(self.api).get(repeating['id'])['status'],'cancelled')
        self.assertFalse(any(path.endswith('/disable') for _,path,_ in self.api.calls))

    def test_single_task_cancel_retains_tombstone(self):
        task=new_task(str(uuid.uuid4()),{'kind':'test'},NOW.isoformat())
        Tasks(TaskStore(self.api)).create(task);self.controller.cancel_task(task['id'])
        self.assertEqual(TaskStore(self.api).get(task['id'])['status'],'cancelled')

    def test_cancel_run_checks_repository_workflow_kind_and_run_identity(self):
        for changes in ({'repository':{'full_name':'other/repo'}},{'workflow_id':999},
                        {'workflow_id':True},{'display_title':'代码检查'},{'id':43}):
            api=ControlAPI();api.run.update(changes)
            with self.assertRaises(ControlError):Controller(api).cancel_run(42)
            self.assertFalse(any(method!='GET' for method,_,_ in api.calls))
        self.controller.cancel_run(42)
        self.assertEqual(self.mutations()[0][1],f'/repos/{self.api.repo}/actions/runs/42/cancel')

    def test_completed_run_is_not_cancelled_or_retried(self):
        self.api.run['status']='completed';self.controller.cancel_run(42)
        self.assertFalse(self.mutations())

    def test_dispatch_lost_response_does_not_retry_or_echo_credentials(self):
        self.api.fail_dispatch=True
        with patch.dict(os.environ,{'GH_TOKEN':'private-token'},clear=True),patch('scripts.control_bot.GitHubAPI',return_value=self.api),contextlib.redirect_stderr(io.StringIO()) as log:
            self.assertEqual(main(['--repo',self.api.repo,'send']),1)
        self.assertEqual(len(self.mutations()),1)
        self.assertNotIn('private-token',log.getvalue())
        self.assertNotIn('private uncertain',log.getvalue())

    def test_cli_reuses_safe_binding_and_requires_explicit_local_repo(self):
        with patch.dict(os.environ,{'GITHUB_TOKEN':'private-token'},clear=True),patch('scripts.control_bot.GitHubAPI',return_value=self.api) as client,patch('scripts.control_bot.make_report',return_value='状态'),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--repo',self.api.repo]),0)
            client.assert_called_once_with(self.api.repo,'private-token')
            self.assertEqual(main(['send']),1)

    def test_argparse_errors_do_not_echo_secret_values(self):
        with contextlib.redirect_stderr(io.StringIO()) as log:
            self.assertEqual(main(['send','--type','private-credential']),1)
        self.assertNotIn('private-credential',log.getvalue())
