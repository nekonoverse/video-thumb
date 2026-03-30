FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8005
CMD ["sh", "-c", "if [ -n \"$UDS_PATH\" ]; then uvicorn main:app --uds \"$UDS_PATH\"; else uvicorn main:app --host 0.0.0.0 --port 8005; fi"]
