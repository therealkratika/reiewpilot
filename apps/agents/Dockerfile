FROM python:3.12-slim

WORKDIR /app
COPY requirements-lock.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements-lock.txt
COPY app ./app
COPY alembic.ini .
COPY migrations ./migrations

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
