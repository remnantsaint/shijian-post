"""Read GitHub Actions state without SMTP access or any send operation."""

import base64
import binascii
import html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from .github_api import GitHubAPI, GitHubAPIError
from .time_utils import UTC, format_beijing, format_utc, parse_utc, utc_now
from .legacy_plan import UNIFIED_WORKFLOW_NAME
from .task_queue import TaskStore


SEND_STEP = "Send mail through QQ SMTP"
SEND_STEPS = frozenset({SEND_STEP, "Send long personal letter through QQ SMTP",
                        "Send personal letter via QQ SMTP", "Send comic mail through QQ SMTP"})
LETTER_WORKFLOW = "dragon-letter.yml"
LETTER_LABEL = "小龙娘来信"
WAIT_STEP = "Wait before sending"
TASK_STEP = "Execute unified mail tasks via QQ SMTP"
STATES = {"active": "启用", "disabled_manually": "手动停用",
          "disabled_inactivity": "因长期无活动停用", "deleted": "已删除"}


class ReportError(ValueError):
    """A fixed, safe diagnostic; never constructed from an API response."""


def markdown(value):
    value = html.escape(str(value), quote=True).replace("\r", " ").replace("\n", " ")
    for char in "|`[]*_~\\":
        value = value.replace(char, f"&#{ord(char)};")
    return value


def timestamp(value):
    try:
        return parse_utc(value)
    except (ValueError, OverflowError):
        raise ReportError("时间字段无效，无法确认状态。") from None


def beijing(date):
    try:
        return format_beijing(date)
    except (ValueError, OverflowError):
        raise ReportError("时间字段无效，无法确认状态。") from None


def positive_id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ReportError("运行编号无效，无法确认状态。")
    return int(value)


def run_url(repo, run_id, attempt=None):
    url = f"https://github.com/{repo}/actions/runs/{positive_id(run_id)}"
    if attempt is not None:
        url += f"/attempts/{positive_id(attempt)}"
    return url


def step_result(run, jobs):
    """A successful workflow alone is never proof of SMTP acceptance."""
    if run.get("status") in {"queued", "requested", "waiting", "pending"}:
        return "排队或等待启动"
    if jobs is None:
        return "状态未知（步骤查询失败）"
    steps = [step for job in jobs for step in job.get("steps", [])]
    send_steps = [step for step in steps if step.get("name") in SEND_STEPS]
    if any(step.get('name')==TASK_STEP for step in steps):
        return '统一任务执行；以任务账本的结果为准'
    if any(step.get("conclusion") == "success" for step in send_steps):
        return "SMTP 已接受"
    if any(step.get("status") == "in_progress" for step in send_steps):
        return "正在发送"
    if any(step.get("status") == "in_progress" and step.get("name") == WAIT_STEP
           for step in steps):
        return "正在倒计时等待"
    if run.get("status") != "completed":
        return "准备发送（检查或配置中）"
    if any(step.get("conclusion") == "failure" for step in send_steps):
        return "发送失败或结果不确定；查看摘要"
    if any(step.get("conclusion") == "cancelled" for step in send_steps):
        return "发送被取消，结果不确定"
    if send_steps:
        return "未发送（发送步骤未执行）"
    if jobs:
        return "旧版环境测试（无发送步骤）"
    return "状态未知（没有可用步骤）"


def record_details(jobs, fallback=None, fallback_source="运行更新时间"):
    """Keep the source explicit; missing old-attempt dates remain unknown."""
    send_dates = []
    job_dates = []
    for job in jobs or []:
        if job.get("completed_at"):
            try:
                job_dates.append(timestamp(job["completed_at"]))
            except ReportError:
                pass
        for step in job.get("steps", []):
            if step.get("name") in SEND_STEPS and step.get("completed_at"):
                try:
                    send_dates.append(timestamp(step["completed_at"]))
                except ReportError:
                    pass
    if send_dates:
        return max(send_dates), "发送步骤结束"
    if job_dates:
        return max(job_dates), "任务结束"
    return fallback, fallback_source if fallback is not None else "未知"


def record_time(jobs, fallback):
    return record_details(jobs, fallback)[0]


def history_sort_key(entry):
    # This sentinel is solely for sorting, never rendered as a real timestamp.
    return entry["date"] or datetime.min.replace(tzinfo=UTC)


def read_main_runs(api, repo):
    return read_workflow_runs(api, repo, "main.yml", "QQ Mail Bot（手动发送邮件）", optional=True)


def read_workflow_runs(api, repo, filename, label, optional=False, send_only=False):
    path = f"/repos/{repo}/actions/workflows/{filename}"
    workflow = optional_read(api, path) if optional else api.request("GET", path)
    if workflow is None and optional:
        return None, []
    query = "per_page=100&event=workflow_dispatch" if send_only else "per_page=20"
    response = api.request("GET", f"{path}/runs?{query}")
    if not isinstance(workflow, dict) or not isinstance(response, dict):
        raise ReportError("邮件工作流响应格式无效，无法确认状态。")
    runs = response.get("workflow_runs")
    if not isinstance(runs, list):
        raise ReportError("邮件工作流运行列表无效，无法确认状态。")
    if send_only:
        # Unified status/CI/control runs are never interpreted as mail attempts.
        runs = [run for run in runs if run.get("event") == "workflow_dispatch"
                and run.get("display_title") in {"发送测试邮件", "发送随机来信",
                                                   "发送小龙娘来信", "发送自定义邮件", "发送漫画邮件", "指定时间发送", "周期发送"}]
    entries = []
    recent_runs = sorted(runs[:20], key=lambda run: timestamp(
        run.get("updated_at") or run.get("created_at")), reverse=True)
    for run in recent_runs:
        run_id = positive_id(run.get("id"))
        latest_attempt = positive_id(run.get("run_attempt", 1))
        fallback = timestamp(run.get("updated_at") or run.get("created_at"))
        fallback_source = "运行更新时间" if run.get("updated_at") else "运行创建时间"
        event = run.get("event")
        kind = {"workflow_dispatch": "手动", "schedule": "定时"}.get(event, "其他触发")
        for attempt in range(latest_attempt, max(0, latest_attempt - (20 - len(entries))), -1):
            attempt_run = dict(run)
            if attempt != latest_attempt:
                # A previous attempt ended; its outcome comes only from its own steps.
                attempt_run["status"] = "completed"
                attempt_run["conclusion"] = None
            jobs = None
            if attempt_run.get("status") not in {"queued", "requested", "waiting", "pending"}:
                try:
                    data = api.request("GET", f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
                    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
                        jobs = data["jobs"]
                except GitHubAPIError:
                    pass  # Unknown is safer than declaring an inaccessible run unsent.
            date, source = record_details(
                jobs, fallback if attempt == latest_attempt else None, fallback_source)
            entries.append({"run_id": run_id, "run_attempt": attempt,
                            "date": date, "time_source": source,
                            "kind": kind, "result": step_result(attempt_run, jobs),
                            "workflow": label})
        if len(entries) >= 20:
            break
    entries.sort(key=history_sort_key, reverse=True)
    return workflow, entries


def optional_read(api, path):
    try:
        return api.request("GET", path)
    except GitHubAPIError as exc:
        if exc.status == 404:
            return None
        raise


def read_random_state(api, repo):
    workflow = optional_read(api, f"/repos/{repo}/actions/workflows/random.yml")
    file = optional_read(api, f"/repos/{repo}/contents/.mail-bot/random.json?ref=mail-bot-state")
    if file is None:
        return workflow, None, []
    try:
        if file.get("encoding") != "base64" or len(file.get("content", "")) > 1000000:
            raise ValueError
        content = "".join(file["content"].split())
        state = json.loads(base64.b64decode(content, validate=True))
        if (not isinstance(state, dict) or state.get("version") != 1
                or not isinstance(state.get("enabled"), bool)
                or state.get("mode") not in {"once", "repeat"}
                or state.get("state") not in {"scheduled", "sending", "completed", "paused", "disabled"}):
            raise ValueError
        for name in ("min_minutes", "max_minutes"):
            value = state.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError
        if state["min_minutes"] > state["max_minutes"]:
            raise ValueError
        if state.get("next_send_at") is not None:
            timestamp(state["next_send_at"])
        if state["enabled"] and state["state"] == "scheduled" and not state.get("next_send_at"):
            raise ValueError
        history = state.get("history", [])
        if not isinstance(history, list) or len(history) > 100:
            raise ValueError
        entries = []
        for item in history:
            source = item.get("time_source")
            if (item.get("status") not in {"accepted", "uncertain"}
                    or source not in {None, "smtp_accepted", "state_recorded"}
                    or source == "smtp_accepted" and item["status"] != "accepted"):
                raise ValueError
            entries.append({"run_id": positive_id(item.get("run_id")),
                            "run_attempt": positive_id(item.get("run_attempt", 1)),
                            "date": timestamp(item.get("occurred_at")),
                            "time_source": "SMTP 接受" if source == "smtp_accepted" else
                                           "状态记录" if source == "state_recorded" else
                                           "状态记录（旧记录）",
                            "kind": "随机计时", "workflow": "Random Mail Scheduler（随机定时发送）",
                            "result": "SMTP 已接受" if item["status"] == "accepted"
                                      else "结果不确定；检查邮箱和运行摘要"})
        return workflow, state, entries
    except (ValueError, TypeError, AttributeError, KeyError, binascii.Error, UnicodeDecodeError):
        raise ReportError("随机计时状态文件无效，无法确认计划；未执行任何发送。") from None


def random_description(repo, workflow, state, now, interval):
    if state is None:
        return ["随机计时：尚未启用。"]
    labels = {"scheduled": "等待计划时间", "sending": "发送中（需查看运行结果）",
              "completed": "一次性计划已完成", "paused": "已暂停，需核验上次结果",
              "disabled": "已停用"}
    lines = [f"随机计时：{labels[state['state']]}；{'启用' if state['enabled'] else '未启用'}。",
             f"随机间隔：{state['min_minutes']}–{state['max_minutes']} 分钟，"
             f"{'循环' if state['mode'] == 'repeat' else '一次性'}。"]
    if workflow is None:
        lines.append("随机计时工作流不存在，计划目前无法执行。")
    else:
        workflow_state = STATES.get(workflow.get("state"), "未知")
        lines.append(f"随机计时工作流：{workflow_state}。")
        if state['enabled'] and workflow.get("state") != "active":
            lines.append("计划仍标记启用，但随机工作流已停用或状态未确认，当前无法自动发送。"
                         "请先核验历史并关闭旧计划，再按需要重新安排。")
    can_poll = workflow is not None and workflow.get("state") == "active"
    if state.get("next_send_at"):
        target = timestamp(state["next_send_at"])
        lines.append(f"下一次计划发送：北京时间 {beijing(target)}。")
        if state['enabled'] and state['state'] == 'scheduled' and target <= now:
            late_seconds = int((now - target).total_seconds())
            minutes, seconds = divmod(late_seconds, 60)
            suffix = ("正在等待下次检查，GitHub 调度可能延迟。" if can_poll else
                      "工作流未运行，无法等待定时检查执行该计划。")
            lines.append(f"计划时间已到，已晚 {minutes} 分 {seconds} 秒；{suffix}")
    if not can_poll:
        lines.append("随机工作流未启用定时检查。")
    elif interval is None:
        lines.append("检查间隔未确认（无法读取仓库变量）；请查看 Actions 状态报告。GitHub 调度和排队可能延迟，不保证准点。")
    else:
        lines.append(f"检查间隔约 {interval} 分钟；GitHub 调度和排队额外计算，不保证准点。")
    if state.get("claim_run_id") is not None:
        lines.append(f"[查看关联的运行]({run_url(repo, state['claim_run_id'], state.get('claim_run_attempt'))})")
    return lines


def make_report(api, repo, now=None, check_interval="60"):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ReportError("GITHUB_REPOSITORY 格式无效。")
    try:
        query_started = parse_utc(format_utc(utc_now() if now is None else now))
    except (ValueError, OverflowError):
        raise ReportError("查询时间无效，无法确认状态。") from None
    workflow, manual = read_main_runs(api, repo)
    letter_workflow, letters = read_workflow_runs(
        api, repo, LETTER_WORKFLOW, LETTER_LABEL, optional=True)
    random_workflow, state, random_history = read_random_state(api, repo)
    unified = isinstance(random_workflow, dict) and random_workflow.get("name") == UNIFIED_WORKFLOW_NAME
    if unified:
        _, current = read_workflow_runs(api, repo, "random.yml", UNIFIED_WORKFLOW_NAME, send_only=True)
        manual = sorted(manual + current, key=history_sort_key, reverse=True)
    query_completed = query_started if now is not None else utc_now()
    lines = ["# 邮件机器人状态与发送历史", "",
             f"仓库：{markdown(repo)}。",
             f"查询开始：北京时间 {beijing(query_started)}；"
             f"查询完成：北京时间 {beijing(query_completed)}。", "",
             "本报告只读，不发送邮件。SMTP 接受不等于最终投递成功。", "",
             f"主入口工作流：{STATES.get((random_workflow if unified else workflow or {}).get('state'), '未知')}。"]
    if manual:
        latest = manual[0]
        lines.append(f"最近一次主工作流：{latest['result']}；"
                     f"[查看运行]({run_url(repo, latest['run_id'], latest['run_attempt'])})。")
    else:
        lines.append("主工作流尚无运行记录。")
    if letter_workflow is None:
        lines.append(f"{LETTER_LABEL}工作流：尚未配置。")
    else:
        lines.append(f"{LETTER_LABEL}工作流：{STATES.get(letter_workflow.get('state'), '未知')}。")
        if letters:
            latest = letters[0]
            lines.append(f"最近一次{LETTER_LABEL}：{latest['result']}；"
                         f"[查看运行]({run_url(repo, latest['run_id'], latest['run_attempt'])})。")
        else:
            lines.append(f"{LETTER_LABEL}尚无运行记录。")
    queue=TaskStore(api).load()[0]
    for entry in manual:
        proofs=[proof for task in queue['tasks'].values() for proof in task.get('history',[])
                if str(proof['run_id'])==str(entry['run_id'])
                and int(proof.get('attempt',proof.get('run_attempt',1)))==entry['run_attempt']]
        if proofs:
            proof=max(proofs,key=lambda item:item['occurred_at'])
            entry.update(result={'accepted':'SMTP 已接受','failed':'明确失败或未发送','uncertain':'结果不确定，禁止重发','expired':'超过截止时间，未发送'}.get(proof['status'],'未知'),
                         date=timestamp(proof['occurred_at']),time_source='SMTP 接受' if proof.get('time_source')=='smtp_accepted' else '结果记录')
        else:
            matching=[task for task in queue['tasks'].values() if task.get('origin',{}).get('run_id')==str(entry['run_id'])]
            if matching:entry['result']='未确认 SMTP；任务状态：'+', '.join(sorted({task['status'] for task in matching}))
    if queue.get('legacy_migrated') or queue['tasks']:
        labels={'queued':'等待目标时间','sending':'处理中','accepted':'SMTP 已接受','failed':'失败或未发送',
                'uncertain':'结果不确定，禁止重发','cancelled':'已取消','expired':'超过截止时间，未发送',
                'staging':'内容准备中','blocked':'需人工核验内容'}
        lines += ['', '## 统一任务', '',
                  '目标时间是期望执行时间；Actions 排队或 cron 延迟不会自动重发。已错过目标但没有截止时间的任务只补发一次。',
                  '| 任务编号 | 状态 | 目标北京时间 | 实际 SMTP 接受时间 | 重复规则 |',
                  '| --- | --- | --- | --- | --- |']
        for task in sorted(queue['tasks'].values(),key=lambda task:task['created_at'],reverse=True)[:30]:
            due=beijing(timestamp(task['target_at'])) if task.get('target_at') else '原计划无目标时间'
            accepted=beijing(timestamp(task['accepted_at'])) if task.get('accepted_at') else '未确认'
            rule=task.get('repeat')
            repeat=('无' if not rule else f"每 {rule['seconds']} 秒" if rule['kind']=='fixed'
                    else f"随机 {rule['min_seconds']}–{rule['max_seconds']} 秒")
            lines.append('| '+' | '.join(markdown(value) for value in (task['id'],labels[task['status']],due,accepted,repeat))+' |')
            for entry in task.get('history',[]):
                random_history.append({'run_id':positive_id(entry['run_id']),
                    'run_attempt':positive_id(entry.get('attempt',entry.get('run_attempt',1))),
                    'date':timestamp(entry['occurred_at']),'time_source':'SMTP 接受' if entry.get('time_source')=='smtp_accepted' else '结果记录',
                    'kind':'统一任务','workflow':UNIFIED_WORKFLOW_NAME,
                    'result':labels.get(entry['status'],'未知')})
        if not queue['tasks']:lines.append('| — | 尚无任务 | — | — | — |')
        lines.append('旧随机计划和图片请求已迁移；源文件仅作历史快照，不再驱动发送。')
    else:
        lines.extend(random_description(repo, random_workflow, state, query_completed,
                                        None if check_interval is None else
                                        5 if check_interval == "5" else 60))
    unique = {}
    for entry in manual + letters + random_history:
        key = (entry['run_id'], entry['run_attempt'])
        if key not in unique or history_sort_key(entry) > history_sort_key(unique[key]):
            unique[key] = entry
    history = sorted(unique.values(), key=history_sort_key, reverse=True)[:20]
    lines += ["", "## 最近 20 条运行或发送记录", "",
              "| 记录时间（北京时间） | 时间来源 | 触发方式 | 发送状态 | 工作流 | 详情 |",
              "| --- | --- | --- | --- | --- | --- |"]
    for entry in history:
        values = [beijing(entry['date']) if entry['date'] is not None else "时间未知",
                  entry['time_source'], entry['kind'], entry['result'], entry['workflow']]
        lines.append("| " + " | ".join(markdown(value) for value in values) +
                     f" | [运行 #{entry['run_id']} · 第 {entry['run_attempt']} 次]"
                     f"({run_url(repo, entry['run_id'], entry['run_attempt'])}) |")
    if not history:
        lines.append("| — | — | — | 尚无记录 | — | — |")
    lines += ["", "新邮件摘要只记录发送结果、时间和附件信息，不保存主题或正文；旧摘要可能含正文，正文请在邮箱查看。",
              "时间来源区分 SMTP 接受、发送步骤结束、任务结束、运行更新时间和状态记录；均不代表实际收件时间。",
              "旧重跑缺少本次结束时间时显示时间未知，不使用其他重跑的时间。",
              "旧记录不会补出正文。历史受 GitHub 运行记录保留期限限制，快照不会自动刷新。", ""]
    return "\n".join(lines)


def main():
    try:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GH_TOKEN", "")
        if not token:
            raise ReportError("缺少 Actions 内置凭据，无法读取状态。")
        summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
        if not summary:
            raise ReportError("缺少 Actions 摘要路径，无法保存报告。")
        report = make_report(GitHubAPI(repo, token), repo,
                             check_interval=os.environ.get("CHECK_INTERVAL", "60"))
        with Path(summary).open("a", encoding="utf-8") as target:
            target.write(report)
    except ReportError as exc:
        print(f"状态查询失败：{exc}", file=sys.stderr)
        return 1
    except GitHubAPIError as exc:
        code = exc.status if isinstance(exc.status, int) else "未知"
        print(f"状态查询失败：GitHub API 无法读取所需数据（HTTP {code}）；请检查工作流读取权限与网络。", file=sys.stderr)
        return 1
    except OSError:
        print("状态查询失败：无法写入运行摘要。", file=sys.stderr)
        return 1
    except Exception:
        print("状态查询失败：响应格式或运行环境异常；未输出底层内容。", file=sys.stderr)
        return 1
    print("邮件机器人状态和最近历史已写入本次运行摘要；未发送邮件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
