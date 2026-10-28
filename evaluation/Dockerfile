# The backend-neutral evaluator: Python and PyYAML, nothing else.
#
# Built from this directory, but the package itself is bind-mounted read-only
# at run time (see docker-compose.yml), so editing a scorer needs no rebuild.
FROM python:3.12.16-slim-bookworm@sha256:6dd8b6dab6b3adc7812bab69eca5dbdc1a5cc690ad28306d234ffb4ef8ac9753

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# An unprivileged user. The results directory is a bind mount owned by the
# host, so the Compose service runs as the invoking host user instead; this is
# the default for any other use of the image.
RUN useradd --create-home --uid 10001 evaluator
USER evaluator
WORKDIR /app
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "-m", "evaluation", "run", "--suite", "all"]
