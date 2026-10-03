# telegram-musicdown：单进程 FastAPI + Kurigram（SDD §8 M4）
# 体积取向：python:3.11-slim + --no-compile-bytecode，依赖只装运行时直需包；
# 前端产物 web/dist 不进 git，构建时在容器内现场生成（run.py build 同逻辑）。
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TGM_WEB_HOST=0.0.0.0 \
    MALLOC_ARENA_MAX=2

WORKDIR /opt/app

# 依赖层（利用缓存）：pyproject 为唯一事实源，逐包列出与 pyproject 对齐
# （不装 dev 组：pytest/mypy/ruff 不进镜像）。
COPY pyproject.toml ./
RUN pip install --no-cache-dir --no-compile \
    fastapi uvicorn pydantic kurigram tgcrypto mutagen pyyaml httpx

# 前端构建层：pnpm build 产出 web/dist（无 pnpm 环境时跳过构建，产物经 build stage 注入）
FROM node:20-alpine AS frontend
WORKDIR /build
COPY web/package.json web/pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

# 代码 + 前端产物；目录布局（FR-CFG-02）与 app.config.app_dirs 默认一致：
# save=downloads、session=data/sessions、temp=data/temp 相对 TGM_BASE_DIR=/data 解析：
# 曲库 /data/downloads，密钥/会话/中转/日志/SQLite 全在 /data/data。
# 密钥文件（/data/data/config.yaml）跟 SQLite 同目录，所以下面那个 VOLUME 一并覆盖它，
# 容器重建不丢，也不必为它单独挂一个文件。
FROM base
COPY app ./app
COPY --from=frontend /build/dist ./web/dist

# 非 root 运行；数据目录按需创建（app_dirs 启动时 mkdir -p），镜像内只建根。
RUN useradd -m tgm && mkdir -p /data /data/data \
    && chown -R tgm:tgm /opt/app /data
USER tgm

VOLUME ["/data/downloads", "/data/data"]

EXPOSE 8787

CMD ["python", "-m", "app"]
