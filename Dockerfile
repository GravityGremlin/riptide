FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    beets \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# streamrip from PyPI (latest available: 2.1.0)
RUN pip install --no-cache-dir "streamrip>=2.1.0"

# tidal-dl-ng removed from PyPI, install from vendored tarball
COPY tidal_dl_ng.tar.gz /tmp/
RUN pip install --no-cache-dir /tmp/tidal_dl_ng.tar.gz

RUN groupadd -g 1000 app && useradd -u 1000 -g app -m -d /app app

COPY --chown=app:app app /app/app
COPY --chown=app:app config/beets /app/.config/beets

WORKDIR /app
USER app

ENV DOWNLOAD_DIR=/app/downloads
ENV LIBRARY_DIR=/app/library
ENV TIDAL_CONFIG_DIR=/app/.config/tidal_dl_ng
ENV STREAMRIP_CONFIG_DIR=/app/.config/streamrip
ENV FLASK_PORT=19287

EXPOSE 19287

CMD ["python", "-m", "app.main"]
