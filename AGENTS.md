# Mail bot operation

Use the existing checkout. Cloud tasks already run in isolation; create a Git worktree only if the user explicitly requests one.

The bot runs in GitHub Actions. The platform starts and removes temporary runners; this repository does not expose SSH, operating-system shutdown, or a permanent server.

When the user explicitly requests an operation, use `python -m scripts.control_bot` from the repository root. Supported operations are status, send, plan, pause and cancel-run. Read [the usage guide](README.md) for arguments. Operate only on the user's explicitly selected runtime repository, or the current `GITHUB_REPOSITORY` in Actions. For local CLI calls always pass `--repo owner/name`; never infer the original author's repository as the target. The public repository distributes code; sending, plans and status workflows require a private repository.

- Status queries are read-only. Never infer permission to send a message from a status request.
- Sending mail, creating a repeating plan, and cancelling a run require the user's instruction. Do not create arbitrary schedules or send a demonstration message without that instruction.
- Reuse injected GitHub authentication. Do not print token values, dump the environment, extract proxy credentials, or request new tokens before testing the required operation with the existing binding.
- Keep QQ addresses and SMTP authorization codes in Actions Secrets. Never copy them into scripts, command arguments, summaries or history.
- A successful workflow dispatch confirms submission, not execution or delivery. Inspect the matching run before claiming success. Do not retry a dispatch whose result is uncertain.
- Cancelling a run cannot recall a message already accepted by SMTP. Check the send result and inbox before arranging another send.
- Long random plans persist in `mail-bot-state`. Preserve existing plans and history. Paused or uncertain sends must not be retried automatically.
- Completed or paused plans can keep background auditing enabled without sending. An explicit pause closes the plan and disables future background checks.
- Keep legacy letter digest snapshots immutable. Do not regenerate them from changed material arrays or reset the used-letter ledger. Only definite ledger conflicts may be retried; uncertain writes and SMTP sends must not be retried automatically.
- Use actual runtime timestamps and Beijing time (UTC+8) for user-facing dates. Keep target time, SMTP acceptance, query time and delivery distinct.

GPT/ChatGPT scheduled tasks use their own tool and authentication context. This repository cannot create such a task or grant it tools. Check the tools exposed in that context before promising that a future GPT task can execute a command or call GitHub. Report GPT task creation, GitHub scheduling and mail delivery separately.
