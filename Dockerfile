FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EXPLORER_DATA_DIR=/app/data

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY frontend frontend

RUN useradd --create-home appuser && mkdir -p /app/data && chown -R appuser /app/data
USER appuser

VOLUME ["/app/data"]
EXPOSE 8000
WORKDIR /app/backend
# One worker: the parsed-DataFrame cache lives in process memory.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
