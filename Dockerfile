FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
COPY tests ./tests
RUN python -m compileall -q app scripts tests && python -m unittest discover -s tests -v
RUN mkdir -p /app/data /app/var && chown -R 10001:10001 /app
USER 10001:10001
CMD ["python", "-m", "app.main"]
