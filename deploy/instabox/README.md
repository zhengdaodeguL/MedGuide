# InstaBox 本地部署

此配置用于 Debian 13 上的单实例 MedGuide。InstaBox 提供公网 HTTPS 端口代理，容器内 Nginx 在本机 `127.0.0.1:8080` 接收转发；API 和模型服务只在 Compose 网络内可达。模型使用官方 Qwen3-1.7B GGUF 和 llama.cpp CPU 服务，不上传模型凭据，也不依赖 Redis、Milvus 或外部 MySQL。

模型、资源限制、固定校验值和完整启动命令见 [`LOCAL_MODEL.md`](LOCAL_MODEL.md)。当前实例沿用 Compose 项目名 `medguide-demo` 以保留既有网络和账户目录；这是内部兼容名称，不代表页面或服务状态。

## 前置条件

- Debian 13、Docker Compose v2、4 vCPU、8 GiB 内存、无 GPU。
- 实例已通过平台 HTTPS 入口提供公网访问；容器 UI 只绑定 `127.0.0.1:8080`。
- 已准备持久账户目录和模型目录；模型文件需允许容器 UID/GID `10001:10001` 读取。

## 配置与启动

在持久盘保存不含模型凭据的环境文件，例如 `/data/workspace/medguide-local.env`：

```text
MEDGUIDE_RUNTIME_DIR=/data/workspace/medguide-demo-runtime
MEDGUIDE_MODEL_DIR=/data/workspace/medguide-models
MEDGUIDE_NETWORK_SUBNET=172.30.90.0/24
MEDGUIDE_BIND_ADDRESS=127.0.0.1
```

`MEDGUIDE_RUNTIME_DIR` 必须继续指向原账户目录，整个目录挂载到 `/app/data/runtime`。配置使用明确的容器环境变量白名单，不会把 `OPENAI_*`、`EMBEDDING_*`、`MYSQL_DSN`、`REDIS_URL` 或服务令牌传入容器。

所有命令都在仓库根目录执行，并固定使用已有项目名：

```powershell
$ErrorActionPreference = 'Stop'
$compose = @('--project-name', 'medguide-demo', '--env-file', '/data/workspace/medguide-local.env', '-f', 'deploy/instabox/compose.local.yml')
docker compose @compose config --quiet
docker compose @compose up -d --wait --wait-timeout 180 medguide-llm medguide-api medguide-ui
docker compose @compose ps
```

模型和 API 不发布主机端口，只有 UI 暴露本机端口。Cookie 名已切换为 `medguide_session`，已有浏览器需要重新登录；只要复用原账户目录，账户不会丢失。

## 安全边界

Nginx 仅向内网 API 注入固定 `X-Forwarded-Proto: https`，不根据用户提交的 XFF 或协议头建立身份。认证 Cookie 保留 `Secure`、`HttpOnly`、`SameSite=Strict`，UI 与 `/api` 必须同源，公网入口必须使用 HTTPS。

高风险信号优先走确定性就医分流；普通回答基于本地资料检索后由本机模型整理，并经过安全审查。库存、检查价格和排班尚未连接医院业务系统，服务会明确提示不可查询，不加载示例业务记录。回答不能替代医生、处方或急救服务。

## 已验收

- Compose 中 API、UI 和模型服务均为 healthy，模型接口无主机端口发布。
- 公网入口 [https://medguide.kdns.fr](https://medguide.kdns.fr) 首页及静态资源返回 200；匿名业务请求 401，内部探针 404。
- 账户登录、刷新恢复、Cookie 属性、跨用户会话隔离和请求恢复均通过。
- 本地模型 SSE 返回 9 个节点、1 个最终事件和 2 条引用；首个节点约 1.1 秒，完整回答约 11.4 秒。
- 普通回答标记 `answer_source=local-grounded`；高风险问题标记 `safety-rules`；业务数据边界按未连接实际系统处理。
