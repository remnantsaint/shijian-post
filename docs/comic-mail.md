# ChatGPT 漫画 → 图片邮件

## 当前工具能做什么

本次检查的会话提供图片生成工具、终端和已绑定的 GitHub 身份；没有可调用的 GitHub 连接器工具、
连接器工具搜索或 ChatGPT Tasks 创建工具。这不代表所有 ChatGPT 产品都没有这些能力。
这里的自动调用入口是终端中的 Python 命令，不是假设普通 ChatGPT 能直接请求任意 HTTP API。

终端已验证 GitHub API 读取、草稿 Release 创建和 Actions 触发权限。
当前云环境对 `uploads.github.com` 返回明确的代理访问拒绝；因此本次没有完成真实二进制上传或真实漫画邮件投递。
不要绕过代理或关闭 TLS。需在执行环境允许 `uploads.github.com`，并确认既有 GitHub 身份允许此上传目的地。
下载需要 `api.github.com` 和 `release-assets.githubusercontent.com`；Actions 运行器使用自身内置令牌。
可行替代方案是在有正常 GitHub 网络访问及授权的本地终端运行同一个上传命令。

## 上传和发送机制

1. 验证本地 PNG/JPEG/静态 WebP 图片，最大 5 MiB；单边最多 8192，最多 4000 万像素。
   检查扩展名、容器结构和尺寸，PNG 检查块 CRC；不执行或解压图片作为程序，不支持 SVG、GIF、ZIP 或任意 URL。
2. 在 `mail-bot-state` 保存请求编号的占用记录，再用 GitHub 身份向草稿 Release 上传二进制。
3. 标题、正文与图片 SHA-256 绑定到请求；将请求置为待执行后，只调用一次统一工作流。
4. Actions 验证草稿状态、附件身份、大小、格式与 SHA-256，使用 CAS 原子占用请求后才允许 SMTP。
5. 图片仅写入运行器的 `images/.mail-bot-tmp/`，复用原来的 UTF-8 邮件与图片附件代码，收件人为 `MAIL_TO`。
6. 持久保存 SMTP 结果；独立 `always()` 清理步骤删除 Release 附件。本地临时图片也会删除。

图片不会被提交到 Git，删除附件不会留下不断增长的图片提交历史。
草稿 Release 名称为 `Mail bot temporary images (keep draft)`，标签 `mail-bot-temporary-images-v1`。
**不要发布这个草稿。** 即使主仓库公开，草稿资产仍需要相应 GitHub 权限访问；有权限的协作者和 GitHub 服务本身仍可访问。
如果有人将草稿发布或改变标签，发送流程会停止；人为发布之前已存在的图片可能因此公开。
请求编号、哈希和发送元数据保存在状态分支，公开仓库的这些元数据不保密。
邮件标题和正文属于普通触发参数，不是 Secrets；请勿在其中填写凭据或需要严格保密的信息。

## ChatGPT/助手调用

必须具备：图片生成工具、生成结果的文件访问能力、终端执行工具，以及已授权的 GitHub 身份。
如果图片工具只返回展示内容、不能取得可读取的文件，就先下载 PNG/JPEG/WebP；没有下载/文件工具时由用户手动下载。
终端需要安全绑定 `GH_TOKEN` 或 `GITHUB_TOKEN`，代码不接受令牌参数，也不从凭据文件提取令牌。
外部身份至少需要目标仓库的 **Contents: Read and write** 与 **Actions: Read and write**，
并以仓库所有者身份触发。匿名访问、普通公开读者和非所有者触发都不能发送漫画邮件。

在仓库根目录先生成并保留一次性请求编号：

```sh
python -c 'import uuid; print(uuid.uuid4())'
```

在本地创建 UTF-8 邮件描述文件，例如 `/tmp/comic-message.json`：

```json
{
  "subject": "小澪给你的漫画",
  "body": "今天的小故事在附件里。愿这几格漫画给你一点轻松。"
}
```

用刚生成的 UUID 调用：

```sh
python -m scripts.comic_mail --repo xurenqi/mail-bot upload \
  --image /tmp/comic.png \
  --message-file /tmp/comic-message.json \
  --request-id 本次任务的固定小写UUID
```

同一次任务或其任何重试必须使用相同 UUID；不要因为响应丢失就生成新编号重发。
上传失败或触发响应不确定时程序不重试，应查询：

```sh
python -m scripts.comic_mail --repo xurenqi/mail-bot status \
  --request-id 本次任务的固定小写UUID
```

然后检查 **Actions → 邮件机器人（统一入口）** 中 `发送漫画邮件` 的运行与 SMTP 步骤。
`upload` 返回成功只表示提交获确认，不表示工作流执行完成或最终收件。
上传入口会自动提供 `request_id`、`custom_subject`、`custom_body`，无需手动填写 `image_path`。
在 Actions 单独选择“发送漫画邮件”却没有对应上传记录会被拒绝，这是正常防重复检查。

## 结果和重复处理

| 请求状态 | 含义 |
| --- | --- |
| uploading / upload_failed / upload_uncertain | 上传未完成或待核验，没有自动提交邮件重试 |
| dispatching | 发送请求可能已提交、待执行或提交结果待核验，禁止重复提交 |
| claimed | 已被一个运行占用；可能正在处理或结果写入未获确认，禁止重跑 |
| accepted | SMTP 已接受，有真实接受时间，不代表最终投递或已读 |
| failed | 明确失败或尚未调用 SMTP，例如认证错误、图片损坏、无权限 |
| uncertain | SMTP 中断、任务取消或被占用后未完成，需要核验收件箱 |

这是“最多一次 SMTP 尝试”，不是保证邮件一定成功投递的“恰好一次”。
成功、失败和不确定请求都不能直接重跑再次发送。核验后确实需要新邮件时，由用户明确安排新请求。
清理失败时结果仍保留，例如 `accepted` 不变、`cleanup` 为 `pending`。
不要因清理失败重跑发送；可由管理员在草稿 Release 删除残留的 `mailbot-UUID` 附件。
上传中断、未成功触发或运行在占用前被取消时可能留下草稿附件，也需按请求编号单独清理；不要删除请求记录。
请求记录是阻止重复发送的占用凭据，不能为了省空间清空它。

## 一次性 ChatGPT 定时任务

当前会话无法创建 ChatGPT 定时任务，也不能给未来任务自动授予本会话的终端或 GitHub 身份。
未来只有任务环境本身暴露上述工具并授权后，才能执行以下任务说明：

> 在指定时间生成一张漫画，取得本地 PNG/JPEG/WebP 文件，写好标题和正文，
> 使用预先保存在任务说明中的固定 UUID 调用 `scripts.comic_mail upload`，查询对应请求及 Actions 结果。
> 禁止自动重试上传或发送；不能确认结果时只报告待核验，不创建新 UUID 再发。

“两分钟后生成”依赖实际具备这些工具的一次性任务调度器；GitHub cron 最短约 5 分钟且可能延迟，
现有随机计划也不负责调用 ChatGPT 生成图片。仅能发送提醒的 Tasks 环境不能完成自动生成与发送。
没有工具时，替代路径是任务提醒用户生成/下载图片，再在本地执行命令；无需购买额外服务器。

## 完整端到端测试

1. 保持临时图片 Release 为草稿；确保上传环境的网络和 GitHub 身份具备前述权限。
2. 现有 `QQ_EMAIL`、`QQ_SMTP_AUTH_CODE`、`MAIL_TO` 保持在 Actions Secrets，无需新增 SMTP Secret。
3. 请求 ChatGPT 生成一张测试漫画，取得真实图片文件，写邮件描述，生成一个固定 UUID。
4. 执行上传命令一次，等待对应的 `发送漫画邮件` 运行结束。
5. 请求查询应为 `accepted`，有 `accepted_at`；检查邮箱附件能显示，正文含北京时间。
6. `cleanup` 应为 `deleted`，草稿 Release 不再包含该请求的附件，代码仓库没有新图片提交。
7. 复用同一 UUID再次调用上传命令应被拒绝，重新运行同一个 Actions 邮件任务也应拒绝再次调用 SMTP。
8. 如状态为 `uncertain` 或结果保存未确认，先检查收件箱与运行摘要，不能自动重发。

本次自动化测试使用模拟 GitHub/SMTP，验证真实 PNG/JPEG/WebP 字节的 MIME 附件、UTF-8、
草稿校验、CAS 竞争、重复/重跑、被取消、丢失响应、清理失败与身份信息不随重定向转发。
没有新增项目依赖；真实 GitHub 上传与真实 SMTP 投递需要上述条件满足后另行授权执行。
