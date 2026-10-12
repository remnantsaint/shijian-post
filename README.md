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
2. 选择默认分支，`message_type` 选择 `test`，点击 **Run workflow**。
3. 查看 `Send mail through QQ SMTP` 步骤；成功表示 SMTP 已接受邮件，再检查收件箱和垃圾邮件文件夹。
4. 选择 `random` 可发送一条内置的随机中文小消息。

失败时工作流退出为失败状态，并输出脱敏的配置、认证、TLS、收件人或网络错误说明。
程序不打印底层异常、邮箱地址或授权码，也不开启 SMTP 调试日志。
网络中断可能发生在服务器已接受邮件之后；重跑前先查收件箱，避免重复发送。
程序不自动重试，SMTP 接受邮件也不保证最终投递成功。

## 可选定时发送

先验证手动发送成功，再在 `.github/workflows/main.yml` 的 `on` 下、与 `workflow_dispatch` 同级加入：

```yaml
  schedule:
    - cron: '0 1 * * *'
```

这会每天北京时间 09:00（UTC 01:00）发送随机小消息。当前未启用定时发送。
GitHub 定时工作流仅运行默认分支，可能延迟或被平台跳过；公开仓库长时间没有活动时，定时任务可能被禁用。

## 本地离线验证

在仓库根目录运行 `python -m unittest discover -s tests -v`。
测试模拟 SMTP，验证 UTF-8 编码、SSL 配置、发送流程、失败状态和日志脱敏，不会发送邮件。
真实 QQ SMTP 认证与投递需要配置 Secrets 后通过 Actions 测试。
