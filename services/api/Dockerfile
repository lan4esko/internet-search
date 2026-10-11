FROM python:3.12-slim

WORKDIR /app

COPY services/api/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY services/api/app ./app
COPY shared/scanlib ./scanlib
COPY services/api/alembic.ini .
COPY services/api/alembic ./alembic
COPY services/api/docker-entrypoint.sh /usr/local/bin/api-entrypoint
RUN chmod +x /usr/local/bin/api-entrypoint

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/api-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
