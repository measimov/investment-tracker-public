FROM python:3.12-slim

WORKDIR /app

# Improve pip resilience on slower networks
ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_REQUIRE_HASHES=0

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app ./app
COPY manage.py .
COPY alembic.ini .
COPY alembic ./alembic
COPY scripts ./scripts

# 非 root 运行（#143）：固定 uid 便于宿主对挂载的日志目录授权
# （compose 挂 ${BACKEND_LOG_DIR}:/app/logs，宿主目录须可被 10001 写入，
# 见 DEPLOYMENT.md 的部署步骤）。
#
# 只授权运行时真正要写的目录，代码保持 root-owned 只读——chown 整个 /app
# 会让被攻破的进程可以改写源码/迁移并在容器重启后持续执行，把非 root
# 加固削掉一半。
#
# --home 必须显式给：Debian 的 adduser --system 默认把 home 设成不存在的
# /nonexistent，而 tushare 的 set_token 会往 ~/tk.csv 落盘——没有可写 home
# 的话，部署容器第一次拉行情/基本面/分红就在这里炸。ENV HOME 同步显式设置，
# 不依赖运行时从 passwd 解析。
RUN addgroup --system --gid 10001 app \
    && adduser --system --uid 10001 --ingroup app --home /home/appuser appuser \
    && mkdir -p /app/logs /home/appuser \
    && chown appuser:app /app/logs /home/appuser
USER appuser
ENV HOME=/home/appuser

# Expose port
EXPOSE 8000

# slim 镜像无 curl/wget，用标准库探活；/health 会真实探一次 DB
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

# Run application
# --no-proxy-headers：uvicorn 的代理头处理默认开启且信任 127.0.0.1，会抢先把
# scope.scheme 改成 https，让应用层的 TRUST_PROXY_HEADERS 形同虚设。关掉它，由
# app/api/auth.py 独占解析 X-Forwarded-Proto / X-Forwarded-For（一个信任决定，
# 一个地方做）。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
