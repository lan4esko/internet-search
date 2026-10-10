FROM python:3.12-slim AS httpx-download
ARG HTTPX_VERSION=1.12.0
ARG TARGETARCH
WORKDIR /download
RUN python -c "import hashlib, os, urllib.request, zipfile; arch=os.environ.get('TARGETARCH') or 'amd64'; version='1.12.0'; checks={'amd64':'9d8439e8b6c9aa7d1e2314817a392e00d5178da3af5652f7475f88868f418f76','arm64':'fd7b123c1dfbc3d69f19f524e4eebcd6ec06b9a6cbd56813c76f11645197331e'}; url='https://github.com/projectdiscovery/httpx/releases/download/v'+version+'/httpx_'+version+'_linux_'+arch+'.zip'; data=urllib.request.urlopen(url).read(); assert hashlib.sha256(data).hexdigest()==checks[arch]; open('httpx.zip','wb').write(data); zipfile.ZipFile('httpx.zip').extract('httpx')"

FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends masscan && rm -rf /var/lib/apt/lists/*
COPY --from=httpx-download /download/httpx /usr/local/bin/httpx
RUN chmod 0755 /usr/local/bin/httpx
COPY services/task-runner/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/task-runner/app ./app
COPY shared/scanlib ./scanlib
CMD ["python", "-m", "app.main"]
