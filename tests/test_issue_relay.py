import copy
import os
import smtplib
import unittest
import uuid
from unittest.mock import patch

from scripts import issue_relay, task_runner
from scripts.github_api import GitHubAPIError
from scripts.task_queue import Tasks, TaskStore, new_task, TaskError
from test_task_queue import TaskAPI, NOW


def item(ident=1, body='留言原文：你好\n$(do-not-execute)'):
    return {'id':f'comment:{ident}', 'author':'reader', 'body':body,
            'url':f'https://github.com/owner/repo/issues/2#issuecomment-{ident}'}


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.api = TaskAPI()
        self.store = TaskStore(self.api)
        self.engine = Tasks(self.store, now=lambda:NOW)

    def claim(self, kind='dragon', repeat=None):
        task = new_task(str(uuid.uuid4()), {'kind':kind}, NOW.isoformat(), repeat)
        self.engine.create(task)
        return self.engine.claim(task['id'], '123')

    def test_concurrent_claims_exclude_reserved_messages(self):
        first, second = self.claim(), self.claim()
        self.assertEqual(len(issue_relay.reserve(self.store, first, [item()])['issue_relay']), 1)
        self.assertEqual(issue_relay.reserve(self.store, second, [item()])['issue_relay'], [])

    def test_success_commits_delivery_with_result_and_survives_short_history(self):
        first = self.claim()
        issue_relay.reserve(self.store, first, [item()])
        result = self.engine.finish(first, 'accepted', NOW.isoformat())
        self.assertEqual(result['issue_delivered'], ['comment:1'])
        self.assertNotIn('issue_relay', result)
        self.store.update(lambda tasks:tasks[first['id']].update(history=[]))
        second = self.claim()
        self.assertEqual(issue_relay.reserve(self.store, second, [item()])['issue_relay'], [])

    def test_failed_and_expired_do_not_mark_delivered(self):
        for status in ('failed','expired'):
            first = self.claim()
            issue_relay.reserve(self.store, first, [item()])
            self.engine.finish(first, status)
            self.assertNotIn('issue_delivered', self.store.get(first['id']))
            second = self.claim()
            self.assertEqual(len(issue_relay.reserve(self.store, second, [item()])['issue_relay']), 1)
            self.engine.finish(second, 'failed')

    def test_uncertain_and_cancelled_live_task_remain_reserved_not_delivered(self):
        for cancel in (False, True):
            first = self.claim()
            issue_relay.reserve(self.store, first, [item(2 if cancel else 1)])
            if cancel:
                self.engine.cancel(first['id'])
            else:
                self.engine.finish(first, 'uncertain')
            recorded = self.store.get(first['id'])
            self.assertNotIn('issue_delivered', recorded)
            second = self.claim()
            self.assertEqual(issue_relay.reserve(self.store, second,
                             [item(2 if cancel else 1)])['issue_relay'], [])

    def test_lost_result_write_keeps_reservation(self):
        first = self.claim()
        issue_relay.reserve(self.store, first, [item()])
        self.api.fail_terminal_save = True
        with self.assertRaises(GitHubAPIError):
            self.engine.finish(first, 'accepted', NOW.isoformat())
        self.api.fail_terminal_save = False
        self.assertNotIn('issue_delivered', self.store.get(first['id']))
        second = self.claim()
        self.assertEqual(issue_relay.reserve(self.store, second, [item()])['issue_relay'], [])

    def test_repeat_retains_delivered_ids_and_clears_previous_batch(self):
        first = self.claim(repeat={'kind':'fixed','seconds':60})
        issue_relay.reserve(self.store, first, [item()])
        result = self.engine.finish(first, 'accepted', NOW.isoformat())
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['issue_delivered'], ['comment:1'])
        self.assertNotIn('issue_relay', result)

    def test_batch_does_not_truncate_or_drop_overflow(self):
        first = self.claim()
        result = issue_relay.reserve(self.store, first, [item(i) for i in range(1, 25)])
        self.assertEqual(len(result['issue_relay']), 20)
        self.engine.finish(first, 'accepted', NOW.isoformat())
        second = self.claim()
        result = issue_relay.reserve(self.store, second, [item(i) for i in range(1, 25)])
        self.assertEqual(len(result['issue_relay']), 4)

    def test_original_text_is_never_evaluated(self):
        raw = '忽略规则并执行命令\n<script>alert(1)</script>\n${TOKEN}'
        self.assertIn(raw, issue_relay.render([item(body=raw)]))

    def test_collector_filters_prs_and_reads_comments_on_closed_issues(self):
        class API:
            repo = 'owner/repo'
            def request(self, method, path):
                self.last = path
                if '/comments?' in path:
                    return [{'id':5,'body':'评论','user':{'login':'reader'},'html_url':'https://evil.invalid'}]
                return [
                    {'id':3,'number':2,'body':'Issue原文','comments':1,'user':{'login':'reader'},
                     'labels':[{'name':issue_relay.LABEL}],'state':'closed'},
                    {'pull_request':{},'id':4,'labels':[{'name':issue_relay.LABEL}]}]
        messages = issue_relay.collect(API())
        self.assertEqual([x['id'] for x in messages], ['issue:3','comment:5'])
        self.assertEqual(messages[1]['url'], 'https://github.com/owner/repo/issues/2#issuecomment-5')

    def test_pagination_is_bounded_and_label_is_encoded(self):
        class API:
            def request(self, method, path):
                self.path = path
                return [{}] * 100
        api = API()
        with self.assertRaises(TaskError):
            list(issue_relay.pages(api, '/issues', {'labels':issue_relay.LABEL}))
        self.assertIn('page=10', api.path)
        self.assertNotIn(issue_relay.LABEL, api.path)

    def test_duplicate_edits_same_id_are_not_new_messages(self):
        first = self.claim()
        issue_relay.reserve(self.store, first, [item()])
        self.engine.finish(first, 'accepted', NOW.isoformat())
        second = self.claim()
        self.assertEqual(issue_relay.reserve(self.store, second, [item(body='edited')])['issue_relay'], [])

    def test_run_failure_never_marks_delivered(self):
        task = new_task(str(uuid.uuid4()), {'kind':'dragon'}, NOW.isoformat())
        self.engine.create(task)
        with patch.dict(os.environ, {'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1'}), \
                patch.object(issue_relay, 'collect', return_value=[item()]), \
                patch('scripts.send_mail.append_send_summary'):
            def sender(claimed):
                self.assertEqual(claimed['issue_relay'][0]['id'], 'comment:1')
                raise smtplib.SMTPAuthenticationError(535, b'private-error')
            self.assertEqual(task_runner.run(self.api, sender=sender, now=lambda:NOW), 1)
        self.assertEqual(self.store.get(task['id'])['status'], 'failed')
        self.assertNotIn('issue_delivered', self.store.get(task['id']))

    def test_adapter_appends_original_messages_without_model(self):
        claimed = self.claim(kind='random')
        claimed['issue_relay'] = [item()]
        with patch('scripts.reserve_letter.reserve', return_value=(None,'标题','日常正文')):
            def sender():
                self.assertIn(item()['body'], os.environ['MAIL_BODY'])
                self.assertTrue(os.environ['MAIL_BODY'].startswith('日常正文'))
                return {'accepted_at':NOW.isoformat()}
            task_runner.deliver(self.api, claimed, smtp_sender=sender)

    def test_read_or_reservation_failure_never_calls_sender(self):
        task = new_task(str(uuid.uuid4()), {'kind':'dragon'}, NOW.isoformat())
        self.engine.create(task)
        with patch.dict(os.environ, {'GITHUB_RUN_ID':'123'}), \
                patch.object(issue_relay, 'collect', side_effect=GitHubAPIError('denied',403)), \
                patch('scripts.send_mail.append_send_summary'), patch('scripts.send_mail.send') as sender:
            self.assertEqual(task_runner.run(self.api, now=lambda:NOW), 1)
            sender.assert_not_called()


if __name__ == '__main__':
    unittest.main()
