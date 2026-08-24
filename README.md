# ecom-ai

企业级在线商城及 AI 智能客服项目。设计权威来源为根目录开发文档与 `docs/*.yaml` Registry。

## 本地开发

前置条件：Docker Desktop、Conda、Node.js 24、pnpm 11。

```bash
conda activate ecom-ai
cp .env.example .env
make bootstrap
make infra-up
make migrate
make seed
make api
```

另开终端启动前端：

```bash
conda activate ecom-ai
make frontend
```

也可以把基础设施、API 与静态前端全部运行在容器中：

```bash
make app-up
```

容器模式访问地址：前端 `http://127.0.0.1:8080`，API 文档 `http://127.0.0.1:8000/docs`，健康检查 `http://127.0.0.1:8000/health/ready`。本地 Vite 开发模式使用 `http://127.0.0.1:5173`。

基础设施只使用 Docker 容器，不启动宿主机 MySQL。默认调试端口为 MySQL `13306`、PostgreSQL `15432`、Redis `16379`。

知识检索默认以 PostgreSQL 全文检索安全降级运行。需要启用向量混合检索时，在 `.env` 配置 OpenAI-compatible Embeddings Endpoint、Key、Model 与固定 `1536` 维度；未配置或 Provider 故障时不会伪造语义结果。容器模式下 `knowledge-indexer` 会处理 MySQL 权威索引命令，并以 PostgreSQL 影子代次原子切换索引。

首次使用管理端前，在本机交互式创建首位平台超级管理员：

```bash
conda activate ecom-ai
make admin-bootstrap USERNAME=your_admin_name
```

命令会提示输入密码，并只显示一次 TOTP Secret、绑定 URI 和恢复码。请立即保存到密码管理器，不要写入 `.env`、截图或提交到 Git。管理端入口为 `http://127.0.0.1:8080/admin/login`。

对象存储通过 S3 兼容适配层接入，当前基线默认关闭；选定持续维护的本地/生产对象存储实现后再启用，避免把已停止维护的镜像固化进开发环境。

## 质量检查

```bash
make registry-check
make lint
make test
make openapi
make check
```

真实 Secret 只放 `.env` 或 Secret Manager，严禁提交版本库。
