FROM python:3.13-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends skopeo && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -g 1000 appgroup && useradd -r -u 1000 -g appgroup appuser

WORKDIR /app

COPY pyproject.toml ./
COPY kubeic_checker/ ./kubeic_checker/
COPY kubeic_operator/metrics.py ./kubeic_operator/metrics.py
COPY kubeic_operator/__init__.py ./kubeic_operator/__init__.py
COPY kubeic_operator/checks/ ./kubeic_operator/checks/

RUN pip install --no-cache-dir . && chown -R appuser:appgroup /app

USER 1000

EXPOSE 9090

CMD ["python", "/app/kubeic_checker/main.py"]
