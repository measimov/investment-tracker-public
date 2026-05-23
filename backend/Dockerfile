FROM python:3.11-slim

WORKDIR /app

# Improve pip resilience on slower networks
ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_REQUIRE_HASHES=0

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app ./app
COPY manage.py .
COPY alembic.ini .
COPY alembic ./alembic
COPY scripts ./scripts

# Expose port
EXPOSE 8000

# Run application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
