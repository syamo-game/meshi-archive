FROM python:3.11-slim

WORKDIR /app

# Install dependencies as a separate layer for cache efficiency
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# CMD is specified per-service in docker-compose.yml
