FROM python:3.11-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    wget \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY dashboard/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire repo (autonomous/ modules + dashboard/)
COPY . /app/

# Set working directory to dashboard for Django management
WORKDIR /app/dashboard

# Collect static files
RUN python manage.py collectstatic --noinput || true

# Entrypoint
COPY dashboard/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000
