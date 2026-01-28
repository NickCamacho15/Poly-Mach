FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY src/ ./src/

# Create non-root user and runtime directories
RUN useradd -m botuser \
    && mkdir -p /app/logs /app/data \
    && chown -R botuser:botuser /app

USER botuser

# Set environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Run
CMD ["python", "-m", "src.main"]
