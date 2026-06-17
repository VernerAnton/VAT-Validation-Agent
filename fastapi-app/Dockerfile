FROM python:3.12-slim
WORKDIR /app
COPY shared/ /app/shared/
COPY fastapi-app/ /app/fastapi-app/
RUN pip install --no-cache-dir -r fastapi-app/requirements.txt
ENV PYTHONPATH=/app
CMD ["sh", "-c", "uvicorn fastapi-app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
