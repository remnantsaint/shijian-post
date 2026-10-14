# 柿间邮局 · Shijian Post

把今天的话，寄给某一天。

一个可以立即寄信、定时寄信的个人小邮局。前端与 API 一起部署在 Cloudflare Workers，使用 D1 保存任务，使用 Resend 投递邮件。

在线地址：[email.remsait.com](https://email.remsait.com)

## 功能

- GitHub 登录，管理员邀请码激活；用户只访问自己的信件和模板。
- 自由填写单个收件人、标题与正文，信纸实时预览。
- 在正文光标处粘贴或选择图片，自动压缩，通过 CID 随邮件发送；每封最多 3 张、合计 1 MB。
- 立即发送、按北京时间定时发送、查看记录、取消尚未开始发送的任务。
- 内置开场白，保存和删除个人文字模板（图片不存入模板）。
- 管理员生成七天一次性邀请码、暂停与恢复寄信。
- HTML 与纯文本邮件，收件人可确认拒收后续来信。
- 本地预览模式，完整体验流程，不发送真实邮件。

当前为邀请使用的小项目，没有应用内额度管理、附件、循环任务或投递状态 webhook。

## 技术与结构

TypeScript · React · Vite · Hono · Cloudflare Workers / D1 · Resend · Zod

```text
src/web/       页面、样式、API 客户端
src/worker/    登录、会话、API、发送和定时调度
src/shared/    前后端共享的数据类型和请求校验
migrations/    数据库结构迁移
scripts/       本地初始化
public/        静态图标与响应头
tests/        API、D1、发送行为测试
e2e/          桌面和手机浏览器验收
docs/         完整搭建与部署指南
```

仓库和 npm 包名为 shijian-post。线上 Worker 已命名为 shijian-post，D1 数据库仍沿用 manyou-mail。仓库、Worker 和数据库是独立资源。

## 本地开发

安装 Git 和 Node.js 22.12 或以上。克隆后进入项目目录：

```sh
git clone https://github.com/remnantsaint/shijian-post.git
cd shijian-post
npm ci
npm run setup
npm run dev
```

打开终端显示的地址，点击“登录邮局”→“预览邮局 · 不发送邮件”。setup 会生成被 Git 忽略的 .dev.vars，并初始化本地 D1。开发环境不需要真实 GitHub / Resend 密钥，本地数据与线上数据独立。

## 验证与发布

```sh
npm run typecheck
npm test
npm run test:e2e
npm run build
```

浏览器验收在 Windows 使用系统 Edge；其他系统可先运行 npx playwright install chromium。自动化测试只使用模拟发送。

第一次上线请按 [完整搭建与部署指南](docs/SETUP.md) 配置 D1、GitHub OAuth App、Resend DNS 和 Worker Secrets。已授权电脑可通过 npm run deploy 更新原 Worker，也可以连接 GitHub 自动部署。

换电脑后只需重新安装依赖、运行 setup；线上账号、DNS、数据库与 Secrets 不需要重建。具体步骤见 [换电脑与日常开发](docs/SETUP.md#换电脑与日常开发)。

## 发送行为

- 定时任务每分钟检查一次，使用北京时间输入，可能有平台调度延迟。
- 相同任务编号不会重复执行；D1 原子领取和 Resend 幂等键降低重复发送风险。
- “服务已接受”代表 Resend 接受了请求，不能证明已进入收件箱。
- 网络中断或结果不确定时不会自动重发；先核对记录和实际收件情况。
- 只有等待寄出的任务可以取消，已经提交的邮件无法撤回。
- 记录页显示最近一百封，历史任务继续保存作为去重依据。
- 退订页面打开时只显示确认，确认后屏蔽该收件地址并取消待寄信件。

## 正文图片

支持复制图片本身后粘贴，也支持选择 JPG、PNG、WebP 文件。在正文中定位光标后插入；没有选位置时放到末尾。编辑区、预览和收件人邮件保持相同顺序。浏览器压缩到最长边不超过 1600 像素；WebP 转为 JPEG，较大的透明 PNG 压缩为 JPEG 时使用白色背景。不读取网页里图片的远程链接。

图片原始字节以 BLOB 保存到 D1 的 task_images，信件记录只保存名称、大小、尺寸、位置等信息。图片接口要求寄件人登录；图片不会上传到公开图床。CID 把图片字节带进邮件，收件人查看邮件不依赖网站保留原图。个别邮箱仍可能显示附件图标或要求点击显示图片。

| 状态                                | 网站保存的图片               |
| ----------------------------------- | ---------------------------- |
| 等待寄出 / 正在提交                 | 保留原图                     |
| 服务已接受且结果保存成功 / 预览完成 | 清理原图，保留记录与图片信息 |
| 已取消 / 已过期                     | 清理原图，保留记录与图片信息 |
| 结果待核实 / 发送失败               | 保留原图供核实，不自动重发   |

清理失败会由后续定时检查再次处理。删除后的数据库页可复用，Cloudflare 控制台显示的文件大小未必立即缩小。已有邮件中的图片不受网站清理影响；网站的已清理记录显示图片名称占位，不再显示原图。

升级此版本需要新增 migrations/0002_inline_images.sql，**先执行 npm run db:migrate:remote，再推送 main 触发自动部署**。新增结构兼容此前版本，不会删除已有信件。具体见 [图片功能升级](docs/SETUP.md#图片功能升级)。

## 配置与费用

实际 API Key、Client Secret、会话凭证和用户数据不提交到 GitHub。线上密钥保存在 Cloudflare Secrets；数据库 ID、域名和 GitHub 用户名属于公开配置。仓库保留站点维护者的线上资源配置，其他使用者需换成自己的资源。

项目可在免费套餐额度内运行，但各服务仍有请求、数据库和发信限制；额度以 [Cloudflare](https://developers.cloudflare.com/workers/platform/pricing/) 和 [Resend](https://resend.com/pricing) 当前规则为准，域名续费另计。

本项目从旧 Python 邮件工具重建，保留原 Git 历史。旧代码不参与当前运行；如果旧仓库仍启用了定时发信工作流，需要在旧仓库单独停用。
