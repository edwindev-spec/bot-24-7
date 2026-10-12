FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEOBF_RECOVER_DIR=/app/prometheus-recover \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv git lua5.1 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv

# StyLua makes successful recoveries readable instead of merely removing obfuscation.
RUN npm install --global @johnnymorganz/stylua-bin \
    && stylua --version

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

ARG PROMETHEUS_COMMIT=98e1d89faeddbe8d478a9b72df58d13d43a2b765
RUN git init /app/prometheus-recover \
    && git -C /app/prometheus-recover remote add origin https://github.com/edwin-dev67/coderabit.git \
    && git -C /app/prometheus-recover fetch --depth=1 origin ${PROMETHEUS_COMMIT} \
    && git -C /app/prometheus-recover checkout --detach FETCH_HEAD

COPY prepare_engine.py ./prepare_engine.py
RUN python3 /app/prepare_engine.py
COPY bot.py ./bot.py

EXPOSE 8080
CMD ["python", "/app/bot.py"]
