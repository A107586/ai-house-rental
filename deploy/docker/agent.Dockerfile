# 智能体服务镜像。
#
# 大模型不在容器里：本机的 NInfer 引擎是 Windows 程序、要直接用显卡，
# 放不进 Linux 容器。容器里的智能体通过 host.docker.internal 访问宿主机上的模型
# （见 docker-compose.yml 的 LLM_BASE_URL），用云端模型时改成厂商地址即可。

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8
WORKDIR /app/agent

COPY agent/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# agent/.env 不会被拷进来（.dockerignore 排除了），配置全部走环境变量
COPY agent/ ./
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
