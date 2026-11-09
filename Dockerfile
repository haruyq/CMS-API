FROM python:3.13-slim

# ffmpeg decodes; Deno is the JavaScript runtime yt-dlp needs for YouTube.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app

# Unprivileged; it owns /data (the cookie file volume) and the venv, so it can update yt-dlp in place.
RUN useradd --system --create-home cms && mkdir /data && chown -R cms /data /app/.venv
USER cms
VOLUME /data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--ws-max-size", "65536"]
