# InstaBox 本地模型部署

本配置面向 4 vCPU、8 GiB 内存、无 GPU 的单实例：llama.cpp 在容器内运行 Qwen3-1.7B，MedGuide 用本地 BM25 资料检索后生成回答。模型权重和账户数据库保存在持久盘，不需要外部模型 API 凭据、Redis、Milvus 或 MySQL。

## 固定版本与资源

- 官方模型：`Qwen/Qwen3-1.7B-GGUF`，`Qwen3-1.7B-Q8_0.gguf`，Apache-2.0。
- 模型 revision：`90862c4b9d2787eaed51d12237eafdfe7c5f6077`；大小 `1,834,426,016` bytes。
- SHA-256：`061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a`。
- 官方 llama.cpp CPU 镜像：b12242，固定 manifest `sha256:4d2de15a0ffbce704777c64026b40512a71e0bbc0b303a5fe5ea9bf71760dff9`。
- 模型容器最多 3 CPU、4 GiB 内存；4096 token 上下文、单并发、512 token 输出上限。显式关闭 RAM prompt cache，避免默认缓存额度挤占主机内存。
- 使用 `--reasoning off` 和 `enable_thinking=false`，不向用户输出内部思考过程。

模型接口只暴露在 Compose 内网，不发布主机端口。API 仅接受显式的本机/内网模型地址，忽略 `OPENAI_*` 及环境代理；对外仍通过 Nginx 的账户 Cookie 认证、HTTPS 入口及 Cloudflare 网关访问。

## 安装与启动

在实例中用 PowerShell 7 下载模型，脚本会验证固定文件大小与 SHA-256，校验通过后才发布为正式模型文件；已有文件不匹配时拒绝覆盖：

```powershell
$ErrorActionPreference = 'Stop'
$env:MEDGUIDE_MODEL_DIR = '/data/workspace/medguide-models'
./deploy/instabox/Install-LocalModel.ps1
```

模型容器以 UID/GID `10001:10001` 运行。首次安装时，应确保模型目录允许该 UID/GID 进入和读取，模型文件允许其读取；安装脚本不会自动修改所有者或权限。当前实例已经成功加载模型，无需更改现有权限。

单独准备不含模型凭据的环境文件，例如 `/data/workspace/medguide-local.env`：

```text
MEDGUIDE_RUNTIME_DIR=/data/workspace/medguide-demo-runtime
MEDGUIDE_MODEL_DIR=/data/workspace/medguide-models
MEDGUIDE_NETWORK_SUBNET=172.30.90.0/24
MEDGUIDE_BIND_ADDRESS=127.0.0.1
```

`MEDGUIDE_RUNTIME_DIR` 必须继续指向原账户数据目录，不要另建空目录替代它。当前实例沿用已有 Compose project `medguide-demo`，仅为兼容现存容器、网络和账户数据；新安装可使用文件默认 project `medguide`，但须选择不冲突的网段。不要在已有实例省略 `--project-name` 另起一套占用相同端口的服务。

迁移后认证 Cookie 名由 `medguide_demo_session` 改为 `medguide_session`，已有浏览器需要重新登录。只要继续挂载原账户数据目录，原账户不会丢失，可使用原用户名和密码登录。

```powershell
$ErrorActionPreference = 'Stop'
$compose = @('--project-name', 'medguide-demo', '--env-file', '/data/workspace/medguide-local.env', '-f', 'deploy/instabox/compose.local.yml')
docker compose @compose config --quiet
docker compose @compose up -d --no-deps --wait medguide-llm
docker compose @compose build medguide-api medguide-ui
docker compose @compose up -d --wait medguide-api medguide-ui
docker compose @compose ps
```

本地模式使用 `MEDGUIDE_LOCAL_LLM_BASE_URL`、`MEDGUIDE_LOCAL_LLM_MODEL`、`MEDGUIDE_LOCAL_LLM_TIMEOUT_SECONDS`、`MEDGUIDE_LOCAL_LLM_MAX_TOKENS`。不要把本机原有供应商配置或 `.env` 复制到实例。持久目录中的模型权重不要提交到 Git。

## 能力与边界

高风险信号仍先走确定性就医规则，普通回答基于检索到的资料生成并经安全审查。模型失联不会伪装成模型成功回答。库存、排班和检查价格没有接入医院业务系统时明确不可用，本地模式不加载示例业务记录。

账户保存在原 SQLite 数据库；咨询状态仍为单进程内存，API 更新后旧咨询状态可能失效。知识库范围有限，来源日期及医学审核要求见项目 README；小模型不替代医生或医学审核。域名绑定不会改变 InstaBox 的自动休眠与连续运行限制。

## 验收

2026-11-10：模型文件与镜像校验通过；CPU 服务健康，短中文回答未包含 thinking，单次 10 token 输出约 27 token/s，模型容器约占 1.88 GiB。短答数据只代表该次测量，不能推算长上下文或并发请求的速度。

应用接入后应通过公网域名检查：账户登录与刷新恢复、跨用户隔离、匿名 401、完整 SSE 节点与引用、实际 `local-grounded` 来源、高风险规则优先、未接入业务数据时的明确提示、模型端口未公开，以及桌面和手机页面。

官方来源：[Qwen 固定版本模型](https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/tree/90862c4b9d2787eaed51d12237eafdfe7c5f6077)、[llama.cpp Docker](https://github.com/ggml-org/llama.cpp/blob/3b4d6ab3f79651935fba1dd93732f29022a7f5f5/docs/docker.md)、[server 参数](https://github.com/ggml-org/llama.cpp/blob/3b4d6ab3f79651935fba1dd93732f29022a7f5f5/tools/server/README.md)。
