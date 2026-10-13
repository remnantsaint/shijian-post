# Mail bot operation

Use the existing checkout. Cloud tasks already run in isolation; create a Git worktree only if the user explicitly requests one.

The bot runs in GitHub Actions. The platform starts and removes temporary runners; this repository does not expose SSH, operating-system shutdown, or a permanent server.

When the user explicitly requests an operation, use `python -m scripts.control_bot` from the repository root. Supported operations are status, send, schedule, plan, pause, cancel-task and cancel-run. Read [the usage guide](README.md) for arguments. Operate only on the user's explicitly selected runtime repository, or the current `GITHUB_REPOSITORY` in Actions. For local CLI calls always pass `--repo owner/name`; never infer the original author's repository as the target. Private repositories are the isolation option. Public repositories permit manual send/status dispatches and manual/scheduled random-plan checks only when both the original and triggering actor are the repository owner. Public runtime metadata is not private. Enabling a workflow does not authorize creating or resuming a sending plan.

- Status queries are read-only. Never infer permission to send a message from a status request.
- Sending mail, creating a repeating plan, and cancelling a run require the user's instruction. Do not create arbitrary schedules or send a demonstration message without that instruction.
- Reuse injected GitHub authentication. Do not print token values, dump the environment, extract proxy credentials, or request new tokens before testing the required operation with the existing binding.
- Keep QQ addresses and SMTP authorization codes in Actions Secrets. Never copy them into scripts, command arguments, summaries or history.
- Comic mail uses `python -m scripts.comic_mail --repo owner/name upload --image path --message-file path --request-id UUID`, with the existing secure GitHub identity. A future task must have its own image/file/terminal tools and authorization. Never assume a connector can upload binary images or create Tasks. Keep the temporary Release draft; never publish it. Reuse the same UUID for the same task and never retry an uncertain upload, dispatch or SMTP call. Query status; never delete request tombstones. Cleanup failure does not justify a resend. See `docs/comic-mail.md`.
- A successful workflow dispatch confirms submission, not execution or delivery. Inspect the matching run before claiming success. Do not retry a dispatch whose result is uncertain.
- Cancelling a run cannot recall a message already accepted by SMTP. Check the send result and inbox before arranging another send.
- All mail kinds use `.mail-bot/tasks.json` on `mail-bot-state`, with content references, target time and optional repeat rules. Random timing only generates timestamps. Preserve task tombstones, the used-letter ledger and immutable legacy snapshots. Failed, uncertain or interrupted attempts must not be retried automatically. Query the task ledger; a green worker step is not SMTP evidence.
- Pause closes repeat tasks; cancel-task cancels a selected one-shot or repeating task. Never disable the entire workflow to cancel a task. Cancel cannot recall accepted mail. Protect assets still used by a live lease. Late tasks send once by default; an explicit deadline expires them. Fixed repeats skip missed periods, random repeats generate the next target only after durable SMTP acceptance.
- Keep legacy letter digest snapshots immutable. Do not regenerate them from changed material arrays or reset the used-letter ledger. Only definite ledger conflicts may be retried; uncertain writes and SMTP sends must not be retried automatically.
- Use actual runtime timestamps and Beijing time (UTC+8) for user-facing dates. Keep target time, SMTP acceptance, query time and delivery distinct.

GPT/ChatGPT scheduled tasks use their own tool and authentication context. This repository cannot create such a task or grant it tools. Check the tools exposed in that context before promising that a future GPT task can execute a command or call GitHub. Report GPT task creation, GitHub scheduling and mail delivery separately.
