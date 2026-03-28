FROM python:3.12-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Data dir (will be overridden by Railway Volume mount at /data)
RUN mkdir -p /data

ENV DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
