FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# 内存态演练台必须固定 1 个 worker；多线程即可满足单人/小班演练。
CMD ["gunicorn", "-w", "1", "--threads", "8", "-k", "gthread", \
     "-b", "0.0.0.0:8000", "--timeout", "30", "app:app"]
