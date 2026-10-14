FROM mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim AS ironbrew-build
ARG IRONBREW_COMMIT=43dfbd8fcff52c1f0b9d1ad32ae45142c03683d4
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git init /src/IronbrewDeobfuscator \
    && git -C /src/IronbrewDeobfuscator remote add origin https://github.com/Lethal-Luka/IronbrewDeobfuscator.git \
    && git -C /src/IronbrewDeobfuscator fetch --depth=1 origin ${IRONBREW_COMMIT} \
    && git -C /src/IronbrewDeobfuscator checkout --detach FETCH_HEAD
WORKDIR /src
COPY bot/ironbrew-adapter/ ./ironbrew-adapter/
RUN dotnet publish ./ironbrew-adapter/IronbrewAdapter.csproj -c Release -r linux-x64 \
    --self-contained true -o /out

FROM debian:bookworm-slim AS luau-build
ARG LUAU_COMMIT=32d52d1b2ceef46fc25d87094a2d7f201c3ea5b8
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN git init /src/luau \
    && git -C /src/luau remote add origin https://github.com/luau-lang/luau.git \
    && git -C /src/luau fetch --depth=1 origin ${LUAU_COMMIT} \
    && git -C /src/luau checkout --detach FETCH_HEAD \
    && test "$(git -C /src/luau rev-parse HEAD)" = "${LUAU_COMMIT}"
RUN cmake -S /src/luau -B /src/luau-build -DCMAKE_BUILD_TYPE=Release \
    -DLUAU_BUILD_CLI=ON -DLUAU_BUILD_TESTS=OFF -DLUAU_BUILD_WEB=OFF \
    && cmake --build /src/luau-build --target Luau.Repl.CLI Luau.Compile.CLI --parallel 2 \
    && install -D /src/luau-build/luau /out/luau \
    && install -D /src/luau-build/luau-compile /out/luau-compile

FROM node:22-bookworm-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEOBF_RECOVER_DIR=/app/prometheus-recover \
    LURAPH_RECOVER_DIR=/app/luraph-fusion \
    LURAPH_CACHE_DIR=/app/cache \
    MV_ENGINE_DIR=/app/mv-engine \
    LUAU_PATH=/usr/local/bin/luau \
    IRONBREW_DEOBF_PATH=/app/ironbrew/IronbrewAdapter \
    PATH=/opt/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv git lua5.1 ca-certificates cmake build-essential libicu72 libssl3 \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv

# StyLua conserva el formato legible de las recuperaciones Prometheus.
RUN npm install --global @johnnymorganz/stylua-bin \
    && stylua --version

WORKDIR /app
COPY bot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

ARG PROMETHEUS_COMMIT=98e1d89faeddbe8d478a9b72df58d13d43a2b765
RUN git init /app/prometheus-recover \
    && git -C /app/prometheus-recover remote add origin https://github.com/edwin-dev67/coderabit.git \
    && git -C /app/prometheus-recover fetch --depth=1 origin ${PROMETHEUS_COMMIT} \
    && git -C /app/prometheus-recover checkout --detach FETCH_HEAD

COPY --from=ironbrew-build /out/ /app/ironbrew/
COPY --from=ironbrew-build /src/IronbrewDeobfuscator/LICENSE /app/licenses/IRONBREW-GPL-3.0.txt
COPY --from=luau-build /out/ /usr/local/bin/
COPY bot/prepare_engine.py ./prepare_engine.py
COPY bot/luraph-fusion /app/luraph-fusion
RUN python3 /app/prepare_engine.py --skip-dependencies --jobs 2
COPY bot/result_cache.py ./result_cache.py
COPY bot/lb-engine /app/lb-engine
COPY bot/iv-engine /app/iv-engine
COPY bot/mv-engine /app/mv-engine
COPY bot/bot.py ./bot.py
EXPOSE 8080
CMD ["python", "/app/bot.py"]
