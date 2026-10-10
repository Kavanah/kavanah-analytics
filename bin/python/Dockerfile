FROM python:3.13-slim

WORKDIR /app

# System deps for compiled Python packages (cryptography, bcrypt, psycopg2-binary)
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# requirements.txt is the hashed lockfile compiled from requirements.in
# — edit the.in, recompile, commit both. --require-hashes makes
# pip refuse any requirement without an exact version and a sha256, so a
# hand-edited lock or a stray unpinned line fails the build instead of
# installing whatever PyPI serves that day.
COPY requirements.txt .

# Upgrade pip past 25.3 to pull in the CVE-2026-1703 path-traversal fix
# before any package installs run. Drop this `pip install -U pip` line
# once the base image (`python:3.13-slim`) ships with pip >= 26.0.
RUN pip install --no-cache-dir --upgrade 'pip>=26.0' && \
    pip install --no-cache-dir --require-hashes -r requirements.txt

# run as an unprivileged user. uid/gid 1000 match the host's
# `ubuntu` user on staging and prod, so files the apps and importers write to
# the bind-mounted /app/data and /app/reporting_configs stay owned by the same
# account that runs `git pull` and the host cron. start_web_apps.sh links
# /app/bin/python -> /app/python (the importers' path); /app is root-owned, so
# the directory is created here, owned by the app user.
RUN groupadd --gid 1000 app && \
    useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app --shell /usr/sbin/nologin app && \
    mkdir -p /app/bin && ln -sfn /app/python /app/bin/python && chown -R app:app /app/bin
USER app
ENV HOME=/home/app

# Start all web apps (FB OAuth :8083, Asset Manager :8084, Password Reset :8085)
# The script is volume-mounted from the repo at runtime (/app/python/start_web_apps.sh)
CMD ["/app/python/start_web_apps.sh"]
