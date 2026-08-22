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

对象存储通过 S3 兼容适配层接入，当前基线默认关闭；选定持续维护的本地/生产对象存储实现后再启用，避免把已停止维护的镜像固化进开发环境。

## 质量检查

```bash
make registry-check
make lint
make test
```

真实 Secret 只放 `.env` 或 Secret Manager，严禁提交版本库。
