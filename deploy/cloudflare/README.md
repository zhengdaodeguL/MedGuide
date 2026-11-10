# Cloudflare Workers 域名代理

`worker.js` 将用户域名的 HTTP 路径与查询转发到代码中固定的 InstaBox HTTPS 上游；请求参数、Host 和环境变量均不能选择其他目标。没有 SDK、运行依赖、密钥或模型配置。

## 当前部署

- 主入口：[https://medguide.kdns.fr](https://medguide.kdns.fr)
- 备用入口：[https://medguide-domain-gateway.longautian.workers.dev](https://medguide-domain-gateway.longautian.workers.dev)
- Worker：`medguide-domain-gateway`，ES Modules 入口为 `worker.js`，兼容日期为 `2026-11-10`。
- Cloudflare 账户 ID：`3ad140c52d098e41ee3957fb6ec68ffc`；独立域名区域使用 `bob.ns.cloudflare.com`、`joan.ns.cloudflare.com` 委派。
- `workers_dev=true`、`preview_urls=false`、`send_metrics=false`，Worker observability 已关闭。
- 固定源站仍为 `https://8080-agq4bggjaxrxosu62udnbbxfsi.instabox.tech`；不是由请求或环境变量指定的开放代理。

`wrangler.jsonc` 记录以上实际配置，包含 `medguide.kdns.fr` 的 Custom Domain route，不包含访问令牌。**当前本机旧 Wrangler OAuth 不属于这个部署账户，不能直接沿用旧登录执行部署。** 后续变更前先运行 `wrangler whoami`，确认账户 ID 与配置一致；不一致时应停止并切换到正确账户授权，仅设置 `account_id` 不会赋予旧登录访问权限。

当前部署使用空兼容标志 `compatibility_flags: []`，未启用 `enable_request_signal`。真实客户端断连后，源站请求及后端任务是否取消尚未验收；本地 mock 的 AbortSignal 测试仅验证模拟信号传递。

## 代理行为

代理直接传递请求体和响应 ReadableStream，不解析 JSON、不缓冲 SSE、不跟随重定向。上游绝对 `Location` 指向固定上游时会改写回用户访问的域名，外部重定向保持为重定向交给浏览器处理；重定向 URL 中的 userinfo 会移除。HTTP 入口仅返回到同域 HTTPS 的 308。

账户 Cookie、多个 `Set-Cookie`、状态码与响应正文保留；当前 MedGuide 使用不带 Domain 的 host-only Cookie，因此浏览器会在用户域名下保存它们。上游与客户端不得通过此代理添加任意目的地或供应商凭据。跳转不会被 Worker 自动跟随，避免将 Cookie 或 Authorization 转发到跳转目标；代理也不会记录请求头、正文、跳转 URL 或异常细节，并会删除响应中的 `Authorization`、`Proxy-Authorization` 和 `X-API-Key`。

所有响应（包括静态资源、登录和 API）都设置浏览器及 CDN `no-store`。上游 fetch 同时使用 `cache: no-store` 与负值缓存 TTL，不使用 Cache API。不要给此 Worker 增加入口缓存，也不要为 `/api/*` 或登录响应配置强制缓存规则。此代理不改变 Nginx 的 Cookie-only 认证、CSP 与同源 API 规则。

在仓库根目录可使用 Node.js 内置测试运行无网络 mock 验证：

```powershell
$ErrorActionPreference = 'Stop'
node --test deploy/cloudflare/worker.test.mjs
```

测试覆盖固定目的地、请求体、Cookie 与多个 Set-Cookie、SSE 延迟完成流、本地模拟 AbortSignal、内外部重定向、禁止缓存、凭据头移除、空响应及不泄露异常的 502。Mock 不验证 Cloudflare 平台配置或真实客户端断连。

## 新域名实机验收记录

2026-11-10 在 `https://medguide.kdns.fr` 完成以下检查：

- HTTPS 首页、JavaScript、CSS 均返回 200；HTTP 入口返回同域 HTTPS 的 308。
- 匿名业务请求返回 401；对外探针地址返回 404。
- 登录 Cookie 为 Secure、HttpOnly、SameSite=Strict、host-only；浏览器刷新后恢复登录。
- 跨用户读取及写入会话均返回 404。
- SSE 收到 9 个节点事件、1 个 `final` 事件和 2 条引用；首个节点约 2.2 秒返回，约 5.9 秒收齐。浏览器可查看参考资料。

当前入口已接入 InstaBox 内的 Qwen3-1.7B 本地模型和 BM25 资料检索，未上传模型凭据；库存、排班和价格尚未连接医院业务系统。域名绑定不改变 InstaBox 的休眠与连续运行限制，不代表 24×7 可用。真实客户端断连取消未验收。

官方依据（实现前核对）：[Fetch](https://developers.cloudflare.com/workers/runtime-apis/fetch/)、[Request / redirect 与缓存参数](https://developers.cloudflare.com/workers/runtime-apis/request/)、[Response / Set-Cookie](https://developers.cloudflare.com/workers/runtime-apis/response/)、[Streams](https://developers.cloudflare.com/workers/runtime-apis/streams/)。
