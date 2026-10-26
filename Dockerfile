# Use lightweight Python 3.11
FROM python:3.11-slim

# Set environment variables for unbuffered logging and dynamic port binding
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=5000

# Set working directory
WORKDIR /app

# Install essential system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency definition and install
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy all project files into container
COPY . /app/

# Ensure logs directory exists
RUN mkdir -p /app/logs

# Expose dashboard port
EXPOSE 5000

# Start the dashboard server (reads $PORT dynamically for Railway)
CMD ["python", "main.py"]
