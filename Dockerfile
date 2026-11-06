FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEBHOOK_HOST=0.0.0.0 \
    WEBHOOK_PORT=9000 \
    DB_PATH=/data/paper_trading.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

VOLUME ["/data"]
EXPOSE 9000

CMD ["python", "bot/bot.py"]
