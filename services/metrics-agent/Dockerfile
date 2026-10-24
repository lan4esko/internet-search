FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --chmod=0755 services/metrics-agent/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chmod=0755 services/metrics-agent/app ./app

USER 65534:65534
CMD ["python", "-m", "app.collector"]
