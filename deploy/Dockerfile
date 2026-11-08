# 本服务的镜像。构建上下文是仓库根目录（compose.yaml 里的 context: ..）。
# pip 默认走腾讯云的 PyPI 镜像，国内服务器快；服务器在国外就 docker compose build --build-arg PIP_INDEX_URL=https://pypi.org/simple
FROM python:3.12-slim

ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
# 版本和 uv.lock 一致，本地测试过的就是这个
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" mcp==2.3.0 starlette==1.10.0 uvicorn==0.54.0 websockets==17.4

WORKDIR /app
COPY glasshome ./glasshome

USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 19530 19531
CMD ["python", "-m", "glasshome.cli", "serve", "--host", "0.0.0.0", "--port", "19530", "--mcp-port", "19531"]
