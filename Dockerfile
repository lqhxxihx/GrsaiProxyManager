FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 确保持久化文件存在（volume 挂载时需要文件而非目录）
RUN touch /app/keys_cache.json /app/.password

EXPOSE 1515

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "1515"]
