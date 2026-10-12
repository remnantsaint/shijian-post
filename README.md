# QQ 邮件机器人

使用 Python 3.12 标准库，通过 `smtp.qq.com:465` 的 SMTP SSL 发送中文 UTF-8 邮件。
保留原工作流的环境测试步骤；不需要安装第三方依赖。

## GitHub Actions Secrets

在仓库 **Settings → Secrets and variables → Actions → New repository secret** 创建：

| Secret 名称 | 内容 |
| --- | --- |
| `QQ_EMAIL` | 启用 SMTP 的完整 QQ 邮箱地址，例如你的 `数字@qq.com` 地址；仅填写邮箱地址 |
| `QQ_SMTP_AUTH_CODE` | 该 QQ 邮箱生成的 SMTP 授权码，**不是 QQ 登录密码** |
| `MAIL_TO` | 一个完整的收件邮箱地址；仅填写邮箱地址，不包含显示名称 |

在 QQ 邮箱设置的账号/安全相关页面启用 POP3/SMTP 或 IMAP/SMTP 服务，按提示生成授权码。
授权码仅填入 Secrets；不要提交 `.env`、凭据文件，也不要在手动触发参数中输入敏感信息。

## 手动发送

将本次代码修改提交并推送到 GitHub 默认分支后：

1. 打开仓库 **Actions → QQ Mail Bot → Run workflow**。
2. 选择默认分支，`message_type` 选择 `test`，`send_delay` 选择 `立即发送`，点击 **Run workflow**。
3. 查看 `Send mail through QQ SMTP` 步骤；成功表示 SMTP 已接受邮件，再检查收件箱和垃圾邮件文件夹。
4. 选择 `random` 可发送一条内置的随机中文小消息。

失败时工作流退出为失败状态，并输出脱敏的配置、认证、TLS、收件人或网络错误说明。
程序不打印底层异常、邮箱地址或授权码，也不开启 SMTP 调试日志。
网络中断可能发生在服务器已接受邮件之后；重跑前先查收件箱，避免重复发送。
程序不自动重试，SMTP 接受邮件也不保证最终投递成功。

## 几分钟后的测试邮件

在 **QQ Mail Bot → Run workflow** 中，将 `message_type` 选为 `random`，
`send_delay` 选为 `3 分钟内随机发送`。工作流完成离线检查后，随机等待 1–180 秒再发送一次。
`Wait before sending` 日志会显示等待秒数和预计开始发送的北京时间，不包含邮箱或授权码。

这是运行器内部的倒计时，触发事件仍为 `workflow_dispatch`，不算 GitHub cron 定时验证。
倒计时从等待步骤开始，GitHub 排队和环境准备时间额外计算，不能保证点击后 5 分钟内收到。
任务总超时为 10 分钟；等待步骤失败会阻止后续发送。默认仍然立即发送。
提交代码只运行不发邮件的检查，不会自动发送测试消息。

## 可选定时发送

GitHub 定时调度可能延迟或跳过，不适合验证严格的几分钟内发送。
当前未启用定时发送；未来可以按每日发送需求添加以下配置。

先验证手动发送成功，再在 `.github/workflows/main.yml` 的 `on` 下、与 `workflow_dispatch` 同级加入：

```yaml
  schedule:
    - cron: '17 1 * * *'
```

这会每天北京时间 09:17（UTC 01:17）发送随机小消息；非整点安排有助于避开调度高峰，但仍不保证准点。
GitHub 定时工作流仅运行默认分支，可能延迟或被平台跳过；公开仓库长时间没有活动时，定时任务可能被禁用。
运行器在每次任务结束后会回收，下次触发时重新启动，不需要自己的服务器常驻。

## 本地离线验证

在仓库根目录运行 `python -m unittest discover -s tests -v`。
测试模拟 SMTP 和倒计时，验证 UTF-8 编码、SSL 配置、发送流程、失败状态、清理异常和日志脱敏，不会发送邮件或实际等待。
**Mail Bot Checks** 工作流会在推送到 `main` 或创建 PR 时运行这些离线测试，不读取邮箱 Secrets。
真实 QQ SMTP 认证与投递需要配置 Secrets 后通过 Actions 测试。
