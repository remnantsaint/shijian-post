# 柿间邮局：完整搭建与部署指南

本文记录项目从本地开发到正式上线的完整流程。记录日期：2026-10-14。服务界面可能更新，DNS 的具体值始终以服务后台为准。

## 架构和开发顺序

旧项目通过 Python / GitHub Actions 发信。新版使用 TypeScript，React 负责界面、Hono 负责 API，放在同一个 Worker 中部署，省去独立服务器和跨域配置。D1 保存状态，Resend 负责投递。

开发顺序是：整理写信/模板/记录流程 → 实现登录与邀请码 → 实现任务保存、定时与防重复提交 → 完成本地模拟测试 → 配置线上账号和 DNS → 发布 → 实际检查邮件认证。

品牌为“柿间邮局”，仓库名为 shijian-post。现有 Worker 已改名为 shijian-post；D1 数据库仍叫 manyou-mail。Worker 改名后要同步 wrangler.jsonc 顶层 name，数据库配置继续指向原 ID。

## 1. 准备工具和账号

- Git，Node.js 22.12 或以上。
- GitHub 账号，用于保存代码与 OAuth 登录应用。
- Cloudflare 账号，域名已使用 Cloudflare DNS，选择免费套餐。
- Resend 账号，选择免费套餐。

本项目的地址规划：

| 用途         | 当前配置                           |
| ------------ | ---------------------------------- |
| 网站访问     | https://email.remsait.com          |
| 邮件发件域名 | send.remsait.com                   |
| 发件地址     | 柿间邮局 <letter@send.remsait.com> |
| GitHub 仓库  | remnantsaint/shijian-post          |
| Worker 名称  | shijian-post                       |
| D1 绑定名    | DB                                 |

其他人复用项目时，使用自己的域名、管理员账号、数据库和密钥。不要把示例或维护者的数据库 ID 当成可访问凭据。

## 2. 本地启动

```sh
git clone https://github.com/remnantsaint/shijian-post.git
cd shijian-post
npm ci
npm run setup
npm run dev
```

setup 仅在缺少 .dev.vars 时从示例创建文件，然后执行本地数据库迁移。文件中使用 development + mock；即使 wrangler.jsonc 的线上 MAIL_MODE 为 resend，本地仍不发送真实邮件。

点击“登录邮局”→“预览邮局 · 不发送邮件”。本地体验者有管理员权限，可以检查模板、定时任务、取消和邀请功能。生产环境不会开放这个体验入口。

## 3. 登录 Cloudflare，创建并绑定 D1

仅首次创建生产资源时执行；现有维护者换电脑不需要再次创建数据库。

```sh
npx wrangler login
npx wrangler whoami
npx wrangler d1 create manyou-mail
```

login 会打开 Cloudflare 网页，登录并授权命令行。创建后把返回的数据库 ID 填到 wrangler.jsonc 的 database_id，binding 必须保持 DB，migrations_dir 为 migrations。

若配置里已手动填入该数据库，创建命令询问是否自动加入配置时选 n，避免多添加一个不同绑定名。这个选择不会删除已创建数据库。

数据库创建后还是空的，需要执行建表迁移。先构建最新配置，再迁移到线上：

```sh
npm run build
npm run db:migrate:remote
```

确认应用迁移时选择 y。本地和线上分别由 --local / --remote 控制；只有线上迁移才能修复首次部署时“业务表不存在”的错误。

[官方：D1 入门](https://developers.cloudflare.com/d1/get-started/)

## 4. 注册 GitHub OAuth App

打开 https://github.com/settings/applications/new，确认是 OAuth App 注册页。

| 字段                                      | 当前站点填写                                       |
| ----------------------------------------- | -------------------------------------------------- |
| Application name                          | 柿间邮局                                           |
| Homepage URL                              | https://email.remsait.com                          |
| Description                               | 把今天的话，寄给某一天。                           |
| Redirect URI / Authorization callback URL | https://email.remsait.com/api/auth/github/callback |

不启用 wildcard matching，不启用 Device Flow。Expire user access tokens 可以保持勾选：本项目只在登录时读取资料，然后使用自己的七天会话，不长期保存或刷新 GitHub Token。

点击 Register application。保存 Client ID，再点击 Generate a new client secret 生成 Client Secret。两个值稍后写到 Worker Secrets。OAuth App 用来登录网站，与 GitHub 仓库的自动部署授权独立。

在 wrangler.jsonc 的 ADMIN_GITHUB_LOGIN 填管理员 GitHub 用户名，不是昵称或邮箱。首次创建账号前必须配置好；第一个匹配用户名的账号成为管理员，普通用户需要邀请码激活。

[官方：创建 OAuth App](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app)

## 5. 注册 Resend，配置发信 DNS

在 Resend 的 Domains 添加 send.remsait.com。选择 Manual setup 查看记录；也可以通过 Auto configure 授权 Cloudflare 自动设置。

手动设置时，在 Cloudflare 的 remsait.com → DNS → Add record，逐条复制 Resend 的完整名称和内容。界面里的省略号不能直接填入。

本次站点使用的记录如下；新注册账号的类型和目标可能不同，应以当前后台为准：

| 类型  | 名称                   | 内容或目标                   | 状态                   |
| ----- | ---------------------- | ---------------------------- | ---------------------- |
| TXT   | resend._domainkey.send | 从 Resend 复制完整 DKIM 公钥 | TTL Auto               |
| CNAME | rsend.send             | 从 Resend 复制对应目标       | 仅 DNS，灰云；TTL Auto |
| CNAME | send.send              | 从 Resend 复制对应目标       | 仅 DNS，灰云；TTL Auto |

Cloudflare 会补上 remsait.com，因此 send.send 的完整地址为 send.send.remsait.com。这是邮件服务记录，不是网站访问入口。TXT/MX 没有网站代理功能；任何邮件 CNAME 都应使用灰云。

Enable Sending 开启，Enable Receiving 关闭。当前只提供发信，不提供收件箱；发件地址不需要单独注册成邮箱。

检查是否已有 _dmarc 或 _dmarc.send。没有记录时，可为发信子域名添加 TXT，名称 _dmarc.send，内容 v=DMARC1; p=none;。本次站点也可继承根域已有的 _dmarc 策略，不要重复添加或盲目覆盖。

p=none 是初期不要求拦截的策略，不能保证进入收件箱。确认正常邮件的 DKIM/SPF 对齐和 DMARC 通过后，再评估 quarantine 或 reject。没有真实收件地址时，不填 rua 报告地址。

添加完成后回 Resend 点击 I've already added the records，等待域名显示 Verified。DNS 同步时间不固定，不需要反复删除和重建。

随后在 API Keys 创建 Sending access 密钥，尽量限制到这个发信域名。保存 API Key，不提交到仓库。

[官方：Cloudflare DNS 配置](https://resend.com/docs/guides/dns/cloudflare) · [DMARC](https://resend.com/docs/dashboard/domains/dmarc)

## 6. 初次发布：先上线页面，再启用发信

检查 wrangler.jsonc：APP_URL、routes 的域名、D1 的 database_id、ADMIN_GITHUB_LOGIN 和 MAIL_FROM 必须正确。首次上线暂用 MAIL_MODE=disabled，ENVIRONMENT=production。

完成上面的线上迁移后执行：

```sh
npm run deploy
```

命令会检查类型、构建并上传 Worker 和静态页面。Custom Domain 会关联 email.remsait.com 并由 Cloudflare 处理证书。已有同名 DNS 记录发生冲突时，先核实用途再处理。

在页面可以打开后，逐项写入线上凭据：

```sh
npx wrangler secret put GITHUB_CLIENT_ID
npx wrangler secret put GITHUB_CLIENT_SECRET
npx wrangler secret put RESEND_API_KEY
```

每条命令按提示输入对应值，不把密钥放进命令参数、代码或聊天。设置在同一个 shijian-post Worker 下。

Resend 域名验证通过后，将 MAIL_MODE 改为 resend，再执行 npm run deploy。

[官方：Worker Secrets](https://developers.cloudflare.com/workers/configuration/secrets/) · [自定义域名](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/)

## 7. 正式验收与邮件认证

管理员用 GitHub 登录，检查“邮局管理员”身份。给自己的邮箱发送一封有实际内容的信，检查收件箱和垃圾箱；再检查定时发送、取消待寄任务和邀请朋友。

自动化测试只使用模拟邮件。实际发信由站点使用者明确操作，不通过测试脚本批量发送。

在收到的邮件中找“更多操作 → 查看原文/邮件头”，搜索 Authentication-Results。没有该字段时再找 Received-SPF、spf=、dkim= 或 dmarc=。

- dkim=fail(No key)：收件服务器当时无法取得签名所用公钥。根据签名的 s 和 d 查询 TXT，例如 s=resend、d=send.remsait.com 对应 resend._domainkey.send.remsait.com。
- DNS 修改后，旧邮件的认证结果不会更新，需要少量新邮件验证。
- 没有显示 spf= 不等于 SPF 失败；本次 rsend.send 通过 CNAME 指向服务商管理的 SPF，不能在同名 CNAME 上额外加 TXT。
- 域名 DKIM 通过且与发件地址域名对齐时，可满足 DMARC 的 DKIM 路径；具体仍看收件方报告。
- SPF/DKIM/DMARC 通过也不保证进入收件箱。新域名信誉、收件人反馈、正文与发送行为仍影响分类。
- QQ 的“代发”常见于服务商使用独立的退信地址，不等于认证失败。

本次排查记录：首次 QQ 测试报告 DKIM No key / DMARC fail；之后权威及多个公共解析器能查到一致公钥，163 新邮件报告 SPF pass 和两个 DKIM pass，随后其他收件人验证成功。没有把真实邮件、收件地址或签名内容写入仓库。

## 8. 上传 GitHub 与自动部署

当前仓库地址：https://github.com/remnantsaint/shijian-post。推送使用 Git，生产分支为 main。

日常更新：

```sh
git status
git add .
git diff --cached
git commit -m "说明本次修改"
git push
```

.gitignore 排除了 .dev.vars、.env、本地数据库、依赖、构建输出、日志和测试截图。Client Secret、API Key、会话令牌、邀请码和用户数据都不上传。数据库 ID、域名、Worker 名称、Client ID 和管理员用户名不是密钥。

首次连接自动部署：Cloudflare → Workers & Pages → 已存在的 shijian-post → Settings → Builds → Connect，授权 GitHub 并选择 remnantsaint/shijian-post。

| 构建项     | 填写                                           |
| ---------- | ---------------------------------------------- |
| 项目根目录 | 仓库根目录                                     |
| 生产分支   | main                                           |
| Node 版本  | 22.12 或以上，可配置 NODE_VERSION=22           |
| 构建命令   | npm run typecheck && npm test && npm run build |
| 部署命令   | npx wrangler deploy                            |

Cloudflare Worker 名称应与 wrangler.jsonc 的 name=shijian-post 一致；D1 数据库的 manyou-mail 和 database_id 保持原样。直接连接现有 Worker 能保留域名、D1 和线上 Secrets。

推送 main 后在 Cloudflare Builds 查看状态，部署成功才代表网站更新。实际 Worker 的凭据继续放在 Variables and Secrets，不需要将 API Key 上传到仓库，也不需要放进前端构建代码。

首次仅启用 main 的生产部署。需要分支预览时，先为预览环境配置独立数据库和模拟发送，避免预览写入正式数据。

数据库结构变更仍需迁移：先检查新 SQL，执行 npm run build 和 npm run db:migrate:remote，再发布依赖新表结构的代码。当前自动部署命令不自动修改数据库结构。

[官方：连接现有 Worker](https://developers.cloudflare.com/workers/ci-cd/builds/) · [构建配置](https://developers.cloudflare.com/workers/ci-cd/builds/configuration/)

## 换电脑与日常开发

安装 Git 和 Node.js，克隆仓库后执行 npm ci、npm run setup、npm run dev。新电脑生成独立本地数据，不会重置线上 D1。

只通过 GitHub 推送触发发布时，只需要 GitHub 推送权限，不需要本机 Wrangler 登录。若要手动 npm run deploy 或执行线上迁移，再 npx wrangler login 到原 Cloudflare 账号。

不需要重建 GitHub OAuth App、Resend 域名、DNS、D1 或 Worker Secrets。线上 Secrets 不会随 git clone 下载，但重新部署同一个 Worker 时仍会保留。

若在本地测试真实 GitHub 登录，需要额外登记开发回调并在 .dev.vars 配置凭据；日常开发使用 mock 即可。若换的是 Cloudflare 账号、生产 Worker 或域名，则需要重新绑定资源和密钥，并单独迁移数据。

## 常见问题

| 现象                                   | 先检查                                                                   |
| -------------------------------------- | ------------------------------------------------------------------------ |
| 首页面板无法加载，/api/config 返回 500 | 线上 D1 是否执行初始迁移、绑定是否仍叫 DB                                |
| GitHub 登录不可用                      | 两项 GitHub Secrets 与 ADMIN_GITHUB_LOGIN 是否齐全                       |
| 回调验证失败                           | Redirect URI 与 APP_URL 是否一致；重新发起登录                           |
| Resend 验证失败                        | 完整 DNS 名称/内容、CNAME 灰云、等待同步                                 |
| 邮件仍显示旧品牌                       | 邮件模板位于 src/worker/mail-template.ts，修改后需要发布；旧邮件不会变化 |
| 结果待核实                             | 检查收件箱和 Resend 记录，不自动重新提交                                 |
| Git push 后网站没更新                  | 仓库是否连接到现有 Worker、分支是否 main、Builds 是否成功                |
| 换电脑运行时真实发信                   | 先执行 setup，确认 .dev.vars 使用 development + mock                     |

## 官方资料

- [Wrangler](https://developers.cloudflare.com/workers/wrangler/)
- [D1](https://developers.cloudflare.com/d1/get-started/)
- [GitHub OAuth](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app)
- [Resend 域名](https://resend.com/docs/dashboard/domains/introduction)
- [Resend API Keys](https://resend.com/docs/dashboard/api-keys/introduction)
- [免费套餐与限制](https://resend.com/pricing)
