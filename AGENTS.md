# Shijian Post

This checkout is now a TypeScript React/Hono app for the user's own Cloudflare Workers account.
Use npm run typecheck, npm test and npm run build. Never send real mail during checks.
Local .dev.vars uses mock delivery. Never print or commit secrets. Production sessions require GitHub OAuth and invitation/admin authorization. Quota management is deliberately out of scope at the user's request.
D1 task IDs and atomic queued-to-sending claims prevent duplicates. Uncertain external submissions must never be automatically retried. Never treat provider acceptance as inbox delivery. Cancellation cannot recall accepted mail.
Do not delete task tombstones or reset live state to retry mail. Do not deploy or change DNS without an authorized target and existing credentials. Preserve the repository Git history.

Repository/package name is shijian-post. The existing production Worker is now shijian-post after the user renamed it; D1 still retains manyou-mail. Keep their configured names and database ID aligned with those existing resources. Setup and deployment instructions are in docs/SETUP.md.
