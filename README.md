# QQ 邮件机器人

一个 GitHub Actions 工作流、一份持久任务账本、一套发送生命周期。

自定义 GPT 的安全调用配置见 [GPT Actions 接入说明](docs/gpt-actions.md)。该配置复用 GitHub API，仍需在 GPT 编辑器配置认证；自动上传 GPT 新生成图片尚未打通。

随机来信和小龙娘日常来信会转达带 `给小澪的留言` 标签的 Issue 正文及新评论，复用统一任务账本去重。配置、失败保护和隐私边界见 [Issue 留言转达](docs/issue-relay.md)。

普通 ChatGPT 若有 GitHub `create_file` 写入工具，可通过 [请求文件入口](mail-requests/README.md) 提交一次邮件任务，无需自定义 GPT Action。公开请求不适合隐私内容；仅允许仓库所有者，复用同一账本防重复。
通过 Python 标准库和 QQ SMTP SSL 发送中文 UTF-8 邮件，支持随机长信、小龙娘来信、自定义内容及图片漫画。
不需要常驻服务器或第三方 Python 依赖。

## Secrets 与使用权限

在仓库 **Settings → Secrets and variables → Actions** 配置：

| Secret | 内容 |
| --- | --- |
| `QQ_EMAIL` | 启用 SMTP 的完整 QQ/Foxmail 邮箱地址 |
| `QQ_SMTP_AUTH_CODE` | 对应邮箱的 SMTP 授权码，不是登录密码 |
| `MAIL_TO` | 单个完整收件邮箱地址 |

不要把凭据放入代码、聊天或触发参数。工作流只向发送步骤传递邮箱 Secrets。
公开仓库的手动操作和定时执行仅允许仓库所有者；漫画任务始终要求所有者身份。
私有仓库的有权协作者仍能操作普通任务。每个人使用自己的私有仓库才能隔离个人记录，不能共享作者的 Secrets。

公开仓库的任务编号、时间、状态与哈希可以被读取，但自定义正文、延期发送的图片快照存放在认证可见的草稿 Release，
不会提交到 Git 或写入公开摘要。触发参数本身不是 Secrets，不能承载凭据或高敏感信息。
旧日志、旧摘要与公开图片不会因本次重构自动隐藏；有权协作者和 GitHub 服务仍可访问草稿内容。

## 唯一入口

打开 **Actions → 邮件机器人（统一入口） → Run workflow**。
`action` 默认是 **查看状态**，不发送、不修改任务。

| 操作 | 内容 |
| --- | --- |
| 发送测试邮件 | 测试内容，也通过任务账本占用并记录结果 |
| 发送随机来信 | 从现有素材选择未使用的文案 |
| 发送小龙娘来信 | 保留小龙娘显示名称与长信风格 |
| 发送自定义邮件 | 使用标题、正文和可选图片 |
| 发送漫画邮件 | 使用上传请求编号，或 `draft:图片文件名` |
| 安排一次 | 创建随机目标时间的单次长信任务 |
| 循环随机发送 | 创建带随机重复规则的长信任务 |
| 指定时间发送 | 自定义邮件，需要填写目标时间 |
| 周期发送 | 自定义邮件，需要填写重复规则 |
| 关闭计划 | 关闭重复任务；不关闭单次任务或整个工作流 |
| 取消任务 | 填 `task_id`，取消指定任务 |
| 运行代码检查 | 只读离线测试，无邮箱 Secrets |

所有发送操作均使用以下相同的时间字段，不再有独立随机调度器或运行器内倒计时：

| 字段 | 例子 | 含义 |
| --- | --- | --- |
| `timing` | 留空或 `now` | 内容保存完成后尽快发送 |
| `timing` | `after:2m` | 从任务创建时起两分钟后的目标时间 |
| `timing` | `2026-10-25T20:00:00+08:00` | 指定带时区的时间；示例需替换为希望的日期 |
| `timing` | `random:5m:2d` | 在 5 分钟到 2 天中随机生成目标时间一次 |
| `repeat_rule` | 留空 | 单次任务 |
| `repeat_rule` | `every:2h` | 固定周期；错过多个周期时不会集中补发 |
| `repeat_rule` | `random:5m:30d` | 成功后生成下一随机目标时间 |
| `expires_at` | 带时区的时间 | 可选截止时间，超过后不发送 |

间隔支持 `s/m/h/d`，每个间隔不超过 90 天。随机范围和固定周期都只生成时间，生命周期完全相同。
“安排一次/循环随机发送”留空目标时间时使用 `random:1m:90d`；循环未填重复规则时使用目标随机范围，
若首目标不是随机时间，则默认后续 `random:1m:90d`。
自定义正文必填，标题可留空；其他发送方式由现有内容模板生成。

**GitHub Actions 不保证精确到秒。** 默认约每小时检查；变量 `MAIL_BOT_POLL_MINUTES=5` 切换为约每 5 分钟。
GitHub 排队、平台跳过调度或长时间无活动停用都会造成延迟。`after:2m` 是目标时间，不保证两分钟收到。
没有截止时间时，迟到任务只补发一次；填写截止时间时，超期任务标记 `expired`，不自动补发。
周期任务跳过错过的时段，下一目标保持周期锚点，不连续发送积压邮件。

## 一套任务状态和防重复

`.mail-bot/tasks.json` 保存每个任务的内容描述/私有内容引用、目标时间、可选重复规则、截止时间、状态和结果历史。
自定义文字和延期图片为不可变草稿附件引用；随机文字保留现有生成与文案去重策略，逐次选择未用文案。

`staging → queued → sending → accepted / failed / uncertain / expired`。
重复任务仅在 SMTP 已接受且结果持久化后返回 `queued`，增加轮次并生成下一目标时间。
`cancelled` 和 `blocked` 不执行。每个任务保留最多 20 条最近结果，迁移源历史另外完整保存。

- 持久 CAS 占用必须先成功，才能调用 SMTP。任务编号加轮次阻止并发重复发送。
- 同一个 GitHub 运行的重跑不创建新任务；立即发送也不绕过占用。漫画附件 ID 对应稳定请求编号。
- 仅明确的状态 SHA 冲突重读有限次数；上传、工作流触发、SMTP 或不确定写入不自动重试。
- 认证等明确失败记为 `failed`；提交期间断线、中断或结果未确认记为 `uncertain`，不会自动续发或重发。
- 中断恢复先查询旧运行，仍在执行或状态未知时不抢占；确认结束后只记不确定，不重新发送。
- 取消正在处理的任务不能撤回邮件；会保留不确定状态并保护仍在使用的附件。
- 清理是独立步骤，不改变发送结果；重复任务保留内容与图片直到结束/取消。
- 草稿存储写入未确认、无内容的旧请求会阻止发送，需要人工核验，不能强行清空任务编号。

SMTP 接受不是最终投递或已读。状态页以任务结果为依据，工作流绿色不等于发过邮件。
目标时间、SMTP 接受时间、结果记录时间分别显示；内部 UTC，页面和写信时间为北京时间 UTC+8。
运行器使用实际系统时间，登录后更新邮件 Date，并在提交邮件前再次检查截止时间。
截止时间检查也不能保证最终投递时间。

## 图片和漫画

普通邮件 `image_path` 可填仓库内 `images/photo.png`，文件不超过 5 MiB，支持 PNG/JPEG/GIF/WebP。
创建任务时验证并私有保存快照，以免延迟期间文件变动改变附件；公开仓库原图片仍然公开。
漫画入口支持 PNG/JPEG/静态 WebP，并额外检查结构、尺寸和 SHA-256。

手机路径：先上传并保存 GitHub 草稿附件，再选择 **发送漫画邮件**，`image_path` 填 `draft:完整文件名`，
填写正文，上传请求编号留空。可以同时填写 `timing` 和 `repeat_rule`，同样进入统一任务。
自动工具仍可使用 `scripts.comic_mail upload/register`，它们只管理上传及触发，不再维护独立发送状态机。
见 [漫画上传、手机操作和工具授权](docs/comic-mail.md)。

不要发布 `mail-bot-temporary-images-v1` 草稿 Release。发送/取消后的临时文件由独立清理步骤删除；
失败上传或不确定准备可能留下草稿文件，可按编号人工清理，但不能删掉请求/任务去重记录。

## 助手与命令行

使用运行环境已有的 `GH_TOKEN`/`GITHUB_TOKEN`，不要把令牌放入参数或聊天。
本地明确指定 `--repo`；Actions 内默认当前仓库。以仓库根目录为工作目录。

```sh
python -m scripts.control_bot --repo xurenqi/mail-bot status
python -m scripts.control_bot --repo xurenqi/mail-bot send --type random
python -m scripts.control_bot --repo xurenqi/mail-bot schedule --type random --at after:10m
python -m scripts.control_bot --repo xurenqi/mail-bot schedule --type custom --at '2026-10-25T20:00:00+08:00' --body '你好'
python -m scripts.control_bot --repo xurenqi/mail-bot schedule --type random --at now --repeat every:2h
python -m scripts.control_bot --repo xurenqi/mail-bot plan --mode repeat --min 5 --min-unit minutes --max 30 --max-unit days
python -m scripts.control_bot --repo xurenqi/mail-bot pause
python -m scripts.control_bot --repo xurenqi/mail-bot cancel-task --task-id 任务UUID
```

`send`、`plan` 是兼容的任务创建入口，不是独立调度器。旧 `--delay random_3min` 只转成 1–180 秒后的目标时间，
不再 sleep，实际仍受 cron 延迟影响。`cancel-run` 仍可取消指定邮件运行，但不能撤回已提交的邮件。
工作流停用且仍有未完成任务时，不会因创建新计划自动恢复旧任务。

ChatGPT Tasks 必须有自己的图片、文件、终端工具与 GitHub 授权；这里不能创建 Tasks 或授予未来任务工具，
不能承诺只说“两分钟后给我发漫画”即可全自动完成。GitHub 自身的任务检查不需要聊天一直在线。

## 迁移与验证

本仓库旧随机计划与旧图片请求已迁到统一账本；旧源文件没有删除或覆盖，不再驱动执行。
已关闭的计划仍关闭；旧 `sending/claimed` 转不确定，禁止重发；旧成功/失败和历史保留。
旧图片请求只有内容哈希时不会猜正文，保持 `blocked`；原授权触发内容匹配且从未占用的请求才可补齐内容。
迁移幂等，重复执行不会覆盖已迁入任务。可用 `scripts.task_runner preview-migration/migrate` 检查其他旧副本。
原 `.github/workflows` 归档仍在 `docs/legacy-workflows/`；旧运行记录依 GitHub 保留期限可查看。

`random_scheduler.py` 和运行器倒计时已移除，`legacy_plan.py` 只读解码旧格式。
工作流只保留只读代码检查及统一任务作业；提交代码不发送邮件。
本地运行 `python -m unittest discover -s tests -v`，测试模拟 GitHub/SMTP，不发送真实邮件。
覆盖时区、延迟、重复规则、迁移、并发占用、取消、过期、失败、清理、UTF-8 与真实格式图片 MIME 字节。
无需新增 QQ Secrets。
