FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

ENV VOICEBOX_MODELS=/models
VOLUME /models
EXPOSE 8100

# model fetch is idempotent — restarts skip straight to serving
CMD ["sh", "-c", "python download_models.py && exec uvicorn app:app --host 0.0.0.0 --port 8100"]
