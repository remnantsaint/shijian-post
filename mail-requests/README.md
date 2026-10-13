# 普通 ChatGPT 邮件请求入口

这里不放可运行示例 JSON，避免部署时误发。只在用户明确要求发送或安排时新增请求。

文件名：`mail-requests/<小写 UUID>.json`，仅允许仓库所有者通过 GitHub 工具创建在 `main`。每次推送只新增一个请求。公开仓库的文件、提交历史和内容公开，禁止填写隐私、邮箱地址、密码、SMTP 授权码或 GitHub 令牌。

格式示例（说明文本，不是待发送任务）：

```json
{
  "version": 1,
  "request_id": "与文件名一致的规范小写UUID",
  "action": "发送自定义邮件",
  "subject": "GPT 自己写的中文标题",
  "body": "GPT 自己写的非空中文正文",
  "timing": "now"
}
```

普通 GPT 可使用 GitHub `create_file` 工具创建此文件。每次新请求只生成一个 UUID，返回不确定时禁止换 UUID、改文件或重跑发送。先读 `.mail-bot/tasks.json`（`mail-bot-state` 分支）检查同编号任务，再创建文件。提交后只查询对应任务历史和 Actions 运行。`accepted` 仅表示 SMTP 接受；最终到达由收件人确认。

`timing` 支持 `now`、`after:2m`、带时区的 ISO 时间、`random:5m:2d`。可选 `expires_at` 必须带时区。GitHub Actions 有延迟；创建未来任务不会未来才生成正文。后台追加实际写信时间，不编造当前时间。

日常邮件可将 action 改为 `发送随机来信` 或 `发送小龙娘来信`，省略 subject/body；沿用原有随机文案和 Issue 留言转达。不支持在文件入口里修改收件人、周期循环、取消任务、上传图片或执行命令。原 GPT Action 的漫画功能保持可用。

修改、删除、重命名请求文件不触发发送，也不取消已创建任务。删除后重建同编号仍被账本去重。保留账本 tombstone；失败或不确定时新发送必须重新获得用户明确授权。

检查全部通过后才会入队；owner-only 限制同时在 workflow 和脚本校验，不接受 PR 或强制推送。普通 ChatGPT 的具体 GitHub 连接器必须支持当前仓库的写入及选择 main。工具缺失或授权不足时说明限制，不改用 Gmail，也不声称已发送。

本入口没有完成普通 ChatGPT `create_file` 的真实端到端测试。部署提交不包含请求 JSON，不发送测试邮件。
