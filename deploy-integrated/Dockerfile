# Integrated hub image: hub core + integration layer + admin package in
# one process (PATCH-POINTS section 5). Build context = the ROOT of the
# assembled tree (ghep-loi-local/), NOT hub/:
#   docker build -f deploy-integrated/Dockerfile -t discord-hub-v2-integrated .
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY hub/requirements.txt .
# openssh-client: the admin page submits agent SSH keys to the VM
# keydrop account (hub_admin.keydrop) and generates its own keypair.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid bridge --no-create-home bridge \
    && mkdir -p /data /run/bridge-v2 \
    && chown bridge:bridge /data /run/bridge-v2
COPY hub/bridgehub ./bridgehub
COPY hub/hub_integration ./hub_integration
COPY admin/hub_admin ./hub_admin
COPY hub/bridge /usr/local/bin/bridge
RUN chmod 755 /usr/local/bin/bridge
RUN chown bridge:bridge /run
USER 10001:10001
CMD ["python", "-m", "bridgehub"]
