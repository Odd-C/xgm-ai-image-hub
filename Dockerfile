FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# 以非特权用户运行。容器默认是 root：应用一旦被攻破，攻击者拿到的是容器内
# root，配合挂载进来的宿主机目录可以直接改写数据与素材。
# data/ 与 storage/ 是运行期写入点，先建好并把 /app 整体交给这个用户。
# 注意 uid 固定为 10001，宿主机上的 ./data 与 ./storage 需要让该 uid 可写
# （见 docker-compose.yml 的说明），否则容器启动时建库会失败。
RUN useradd --system --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/storage \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5190
CMD ["uvicorn", "image_hub.app:app", "--host", "0.0.0.0", "--port", "5190"]
