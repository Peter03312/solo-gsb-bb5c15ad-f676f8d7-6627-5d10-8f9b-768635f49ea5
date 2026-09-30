FROM python:3.12-slim

# 不生成 .pyc、日志不缓冲
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

# 先装依赖（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY app/ ./app/

# 非 root 运行
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /srv/app
USER appuser

EXPOSE 8000

# db.init_pool 内含数据库就绪重试；多 worker 下由货物行锁串行化同货写入
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
