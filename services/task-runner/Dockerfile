FROM golang:1.24-bookworm AS zgrab-build
ENV GOMAXPROCS=2
RUN go install -p 2 github.com/zmap/zgrab2/cmd/zgrab2@v0.2.0 && go clean -cache -modcache
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends zmap ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=zgrab-build /go/bin/zgrab2 /usr/local/bin/zgrab2
COPY services/task-runner/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chmod=0755 services/task-runner/app ./app
COPY --chmod=0755 shared/scanlib ./scanlib
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.v2"]
