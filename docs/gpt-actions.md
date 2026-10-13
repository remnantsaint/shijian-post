# 自定义 GPT 接入

这份配置让自定义 GPT 直接调用 GitHub API，复用现有统一工作流与任务账本，不调用 Codex 会话，不新增服务器。当前没有在自定义 GPT 产品中完成真实调用验证。

## 配置

1. 在 ChatGPT 的自定义 GPT 编辑器里打开「配置 → Actions → 创建新 Action」。将 `integrations/gpt/openapi.json` 粘贴为 schema。
2. Authentication 选择 API Key、Bearer。使用仅授权本仓库的细粒度 GitHub 令牌，Contents 与 Actions 为读写，Metadata 自动只读。值只填产品认证栏，不写进 schema、指令、聊天或代码。Codex 网络密钥不会自动继承到 GPT。
3. 把 `integrations/gpt/instructions.md` 加入 GPT 指令。保持 GPT 仅自己使用。公开仓库任务仍由工作流验证仓库所有者身份。
4. 在编辑器检查 schema 是否被接受，以及各只读操作能否返回账本和运行。若平台限制自定义 Accept 请求头、GitHub 域名或认证，停止发送并报告错误，不关闭安全检查。不要把 GPT 发布给其他用户：所有使用者可能借助配置的令牌发送邮件。

## 一次测试

先要求 GPT「查询邮件任务，不发送」。确认 `readMailTasks` 返回可理解的账本。再明确要求「发送一封你写的小消息，结尾写实际北京时间」，确认 consequential 操作。保留 UUID，通过账本查结果，核对对应 Actions 运行；SMTP accepted 与收件箱收到分开确认。失败或响应丢失时只查询，不能重提。

漫画先按 `docs/comic-mail.md` 上传图片到临时草稿 Release，再给 GPT 精确的 asset:编号 或 draft:文件名。GPT 可触发既有漫画登记流程，附件类型、大小、SHA-256、去重、SMTP 与清理由现有实现负责。已发送的附件不能再次登记。

## 能力边界

- 支持提交自写文字、随机来信、小龙娘来信、已有草稿图片，以及统一任务的目标时间。没有单独随机调度器。
- schema 不提供图片二进制上传；新生成的图片仍需 Codex 上传命令或浏览器草稿上传。不能从普通聊天图片预览提取文件，也不能假设 GPT 会自动传递生成图片。
- 普通 ChatGPT 与 GPT 定时任务不自动获得自定义 GPT Action。不能宣称定时任务能调用此 GPT 或 Codex。延迟发送仅表示现在创建未来发送任务，不表示未来才生成漫画。
- 无 SMTP 或 GitHub 密钥进入仓库。工作流输入与公开状态元数据不适合隐私内容。GitHub token 过期后需在 GPT 认证栏更换。
- schema 中的请求编号要求是客户端契约。原有账本阻止同编号再次处理，图片按附件去重；模型若违反指令换编号，普通文字任务可能重复。平台操作确认仍需保留。
