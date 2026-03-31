FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# COPY libnvidia-ml.so.1 /usr/local/lib/libnvidia-ml.so.1  # uncomment when P40s are installed (or run setup_gpu.sh)
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]