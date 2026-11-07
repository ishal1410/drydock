# The orchestrator image. Built and run on the Vultr VM since 2026-11-07.
# Bases pinned by digest; web/dist comes from the laptop (deploy/deploy.sh), so there is no npm on the VM.
FROM docker:27-cli@sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c AS cli

FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
# sandbox/ drives sibling containers through the socket
COPY --from=cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY core ./core
COPY eval ./eval
COPY llm ./llm
COPY orch ./orch
COPY policy ./policy
COPY sandbox ./sandbox
COPY seed ./seed
COPY web/dist ./web/dist
RUN useradd --uid 1000 --create-home app && mkdir -p /data/receipts && chown -R app /data
USER app
