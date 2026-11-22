FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid bridge --no-create-home bridge \
    && mkdir -p /data /run/bridge-v2 \
    && chown bridge:bridge /data /run/bridge-v2
COPY bridgehub ./bridgehub
COPY bridge /usr/local/bin/bridge
RUN chmod 755 /usr/local/bin/bridge
# /run/bridge.sock is the protocol default. Writable /run is scoped to this
# container; use a named socket volume there so host /run is never mounted.
RUN chown bridge:bridge /run
USER 10001:10001
CMD ["python", "-m", "bridgehub"]
