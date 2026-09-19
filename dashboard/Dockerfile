FROM indicode-agent AS docker-cli

FROM python:3.11-slim
WORKDIR /app

# Reuse the Docker client that the agent image already installs. This is much
# smaller than installing a Docker daemon in the dashboard image.
COPY --from=docker-cli /usr/bin/docker /usr/local/bin/docker

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY .streamlit /app/.streamlit
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
