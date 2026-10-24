FROM python:3.12-slim

WORKDIR /app

COPY --chmod=0755 services/api/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY --chmod=0755 services/api/app ./app
COPY --chmod=0755 shared/scanlib ./scanlib
COPY --chmod=0755 services/api/alembic.ini .
COPY --chmod=0755 services/api/alembic ./alembic
COPY --chmod=0755 services/api/docker-entrypoint.sh /usr/local/bin/api-entrypoint
RUN chmod +x /usr/local/bin/api-entrypoint

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/api-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

COPY --chmod=0755 templates/v2 ./templates/v2
