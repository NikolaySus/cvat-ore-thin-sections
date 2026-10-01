FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ore_service ./ore_service
CMD ["uvicorn", "ore_service.api:app", "--host", "0.0.0.0", "--port", "8090"]
