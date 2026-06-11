FROM python:3.12-slim
WORKDIR /app
COPY shared/ /app/shared/
COPY fastapi-app/ /app/fastapi-app/
RUN pip install --no-cache-dir -r fastapi-app/requirements.txt
ENV PYTHONPATH=/app
CMD ["uvicorn", "fastapi-app.main:app", "--host", "0.0.0.0", "--port", "8000"]
