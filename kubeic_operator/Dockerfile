FROM python:3.13-slim

RUN groupadd -g 1000 appgroup && useradd -r -u 1000 -g appgroup appuser

WORKDIR /app

COPY pyproject.toml ./
COPY kubeic_operator/ ./kubeic_operator/

RUN pip install --no-cache-dir . && chown -R appuser:appgroup /app

USER 1000

EXPOSE 9090

ENTRYPOINT ["kopf", "run", "--standalone", "-A", "/app/kubeic_operator/main.py"]
