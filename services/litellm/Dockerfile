FROM ghcr.io/berriai/litellm:main-stable

USER root

RUN /app/.venv/bin/python -m ensurepip --upgrade && \
    /app/.venv/bin/python -m pip install --no-cache-dir Pillow
