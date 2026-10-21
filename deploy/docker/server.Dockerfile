# 业务后端镜像：FastAPI 后端 + 打包好的前端页面（后端在 8080 直接提供页面）。
#
# 「多阶段构建」：第一阶段用 Node 把 web/ 打包成静态文件，第二阶段只装 Python，
# 把第一阶段的产物 dist/ 拷过来。最终镜像里没有 Node 和 node_modules，体积小很多。
#
# 构建上下文是仓库根目录（见 docker-compose.yml 的 build.context）。

# ---------------------------------------------------------------- 阶段一：打包前端
FROM node:20-alpine AS web
WORKDIR /src/web
# 先只拷依赖清单再装依赖：依赖没变时这一层直接用缓存，改页面代码不用重新 npm ci
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---------------------------------------------------------------- 阶段二：运行后端
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    DATABASE_URL=sqlite:////data/rental.db
WORKDIR /app/server

COPY server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./
# 后端按 <仓库根>/web/dist 找前端产物（app/main.py 的 WEB_DIST），这里放到同样的相对位置
COPY --from=web /src/web/dist /app/web/dist
COPY deploy/docker/ensure_seed.py /docker/ensure_seed.py

# 数据库放在 /data（compose 里挂成数据卷），容器删了数据还在
RUN mkdir -p /data /app/server/static/uploads
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"

# 空库才生成演示数据（seed.py 每次都会清空重建，不能无条件跑），然后起服务
CMD ["sh", "-c", "python /docker/ensure_seed.py && exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --proxy-headers"]
