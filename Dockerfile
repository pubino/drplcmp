# Containerised test image for drplcmp.
# macOS-friendly: build with `docker build -t drplcmp-tests .`
# and run via `docker-compose run --rm tests` or `docker run --rm drplcmp-tests`.
FROM python:3.12-slim

WORKDIR /app

COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY drplcmp.py ./
COPY tests/ ./tests/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "pytest", "-v", "tests/"]
