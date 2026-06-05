FROM python:3.11-slim

# Prevents Python from writing .pyc files and ensures output is sent straight to logs
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

# Create a non-root user for security and to avoid the pip warning
RUN addgroup --system appgroup && adduser --system --group appuser

# Install dependencies as root (standard for build stage)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Secure the application files
RUN chown -R appuser:appgroup /app
USER appuser

# Cloud Run sets the PORT environment variable (default is 8080)
EXPOSE 8080
CMD ["python", "main.py"]