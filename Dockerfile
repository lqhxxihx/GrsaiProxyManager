FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# .dockerignore 已排除 .env .password keys_cache.json
COPY . .

# 确保持久化文件存在（volume 挂载目录）
RUN mkdir -p /app/data && \
    touch /app/data/keys_cache.json /app/data/.password && \
    echo 'GRSAI_API_KEYS=\nMIN_CREDITS=400\nCREDITS_REFRESH_INTERVAL=300\nPORT=1515' > /app/.env && \
    mkdir -p /app/results

EXPOSE 1515

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "1515"]
