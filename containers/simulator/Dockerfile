# Simulator image (linux/arm64), built by CodeBuild on an ARM build host (containers/buildspec.yml).
# Base image from Amazon ECR Public (official Docker "python" image mirror) to avoid Docker Hub limits.
FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY containers/simulator/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /app/requirements.txt \
 && useradd --uid 10001 --create-home sim
COPY src/edge_pipeline /app/src/edge_pipeline
USER 10001
# The simulator is a batch process; healthy as long as the interpreter and the package load.
HEALTHCHECK --interval=60s --timeout=10s --retries=3 CMD python -c "import edge_pipeline.simulator.device" || exit 1
ENTRYPOINT ["python", "-m", "edge_pipeline.simulator.cli"]
