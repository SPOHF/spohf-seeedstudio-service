# Official Playwright image: Python + Chromium + all system deps preinstalled.
# Keep the tag in sync with the playwright version in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV PYTHONUNBUFFERED=1
# Railway injects $PORT
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
