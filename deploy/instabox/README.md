# Instabox 离线演示部署

此配置用于 Debian 13 上的单实例 MedGuide 演示。Instabox 提供公网 HTTPS 端口代理，容器内 Nginx 在本机 `127.0.0.1:8080` 接收转发；API 8000 端口只在 Compose 网络内可达。不会启动仓库的 Caddy 网关，也不需要自有域名、模型密钥、Redis、Milvus 或外部 MySQL。

## 演示边界

- 回答来自本地 BM25 资料、固定模板和风险规则，不是真实模型生成。资料规模有限，未经医学审核，不能代替诊断或处方。
- 库存、检查价格和排班来自仓库的 SQLite 演示夹具，不是医院实时数据。
- 页面应显示离线演示提示；构建参数 `VITE_DEMO_MODE=true` 用于启用该提示。
- 会话、幂等结果与反馈只在单 API 进程内存中保留；API 重启后会丢失。账户与登录令牌保存在持久化 SQLite 中。不要增加 API worker 或副本。
- 当前使用的免费实例可能被平台暂停；恢复期间入口可能不可用，不承诺 7×24 小时在线。
- 仅用于合成示例，请勿输入真实患者资料或其他敏感信息。内置账户支持公开注册，不是私有邀请系统。

## 准备持久目录与环境

部署者应在 `/data/workspace` 持久盘下**显式创建一个新的运行目录**，并将所有者设为 UID/GID `10001:10001`，允许 API 容器写入。不要直接复用其他服务目录。Compose 的 bind mount 禁止自动创建路径，未准备好的目录会让部署失败。

在持久盘保存独立的演示配置文件，例如 `/data/workspace/medguide-demo.env`：

```dotenv
MEDGUIDE_RUNTIME_DIR=/data/workspace/medguide-demo-runtime
MEDGUIDE_BIND_ADDRESS=127.0.0.1
MEDGUIDE_NETWORK_SUBNET=172.30.90.0/24
```

`MEDGUIDE_RUNTIME_DIR` 必填且没有默认值。整个目录挂载到 `/app/data/runtime`，包含 `auth.sqlite3`、SQLite WAL/SHM、演示业务数据库及 BM25 快照。不要只持久化单个 SQLite 文件。

`MEDGUIDE_NETWORK_SUBNET` 必须与实例已有 Docker 网络不重叠。UI 镜像的原始启动校验要求网段与可信代理配置一致；此演示 Nginx 本身不启用 XFF real-IP 信任。

配置采用明确的容器环境变量白名单，没有 `env_file`，不会把 `OPENAI_*`、`EMBEDDING_*`、`MYSQL_DSN`、`REDIS_URL` 或服务令牌传入容器。启动命令应显式使用该演示配置文件，避免 Compose 自动读取其他 `.env`。不要复制本机模型配置到实例。

## 启动

在仓库根目录执行以下 Compose 命令（需要 Docker Compose v2）：

```powershell
$ErrorActionPreference = 'Stop'
docker compose --env-file /data/workspace/medguide-demo.env -f deploy/instabox/compose.demo.yml config --quiet
docker compose --env-file /data/workspace/medguide-demo.env -f deploy/instabox/compose.demo.yml up -d --build
docker compose --env-file /data/workspace/medguide-demo.env -f deploy/instabox/compose.demo.yml ps
```

API 启动前会从仓库的知识目录构建或复用 BM25 快照；首次构建镜像需要下载软件依赖，运行时不连接模型服务。API 限制 1 GiB 内存，UI 限制 128 MiB；这不是镜像构建的内存限制。两容器均设为 `restart: unless-stopped`，但该策略无法阻止平台暂停整台实例。

## HTTPS 入口与登录边界

将 Instabox HTTPS 入口指向实例本机的 8080 端口。优先保留 `127.0.0.1` 绑定；只有确认平台代理无法访问 loopback 时，才由部署者调整 `MEDGUIDE_BIND_ADDRESS`，并确保该端口只能由平台 HTTPS 入口访问。不要直接向公网开放 8080 或 API 8000。

Nginx 仅向内网 API 注入固定 `X-Forwarded-Proto: https`，前提是外部入口已经终止 TLS；它不根据用户提交的 XFF 或协议头建立身份。认证 Cookie 保留 `Secure`、`HttpOnly`、`SameSite=Strict`，UI 与 `/api` 必须同源，浏览器测试必须使用平台 HTTPS 地址。

离线 API 原本不会强制登录，所以这个配置在业务 `location /api/` 中使用 `auth_request /_auth`，通过无请求体的内部 GET `/api/auth/me` 子请求校验账户 Cookie；未登录返回 401。登录、注册、注销和账户检查端点不受该子请求保护，避免递归。`/_auth` 只允许内部请求。

登录和注册保留每连接源地址每分钟 6 次、突发 3 次限制，业务 API 为每分钟 30 次、突发 10 次。不信任任意公网 XFF，平台代理下多个访问者可能共享限流预算。这些限制不能替代平台级防滥用保护。

现有同源 CSP、安全响应头和 180 秒无缓冲 SSE 转发均保留。`/api/health`、`/api/ready` 对外始终返回 404；`/_health`、`/_api-ready` 仅允许容器 loopback 探针访问，对外也返回 404。

## 验收

1. Compose 显示 API 和 UI 都为 healthy，公网首页明确标注离线演示。
2. 未登录调用 `/api/sessions` 返回 401，直接访问 `/_auth`、健康和就绪地址返回 404。
3. 在 HTTPS 页面注册并登录，Cookie 包含 `Secure`、`HttpOnly` 和 `SameSite=Strict`；随后可建立会话。
4. 普通问题返回本地引用，高风险问题优先给出就医分流，结构化问题明确展示演示数据边界。
5. SSE 逐节点进度能持续更新，最终回答和引用完整。
6. 重启这套演示容器后，原账户仍可登录；旧内存会话丢失符合本配置边界。

这里只提供演示部署。切换真实模型、真实医疗资料、共享会话或多副本运行时，应重新使用生产部署门禁并单独验收，不能仅修改页面上的演示提示。
