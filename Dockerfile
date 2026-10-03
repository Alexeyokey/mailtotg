FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml .
COPY mail_summary_bot ./mail_summary_bot
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home bot && mkdir /app/data && chown bot:bot /app/data
USER bot
CMD ["mail-summary-bot", "run", "--config", "/app/config.toml"]
