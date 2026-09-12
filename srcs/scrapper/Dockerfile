FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY scrapper.py scheduler.py ./

RUN mkdir -p /app/data /app/logs

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os; os.kill(1, 0)" || exit 1

ENTRYPOINT ["python", "scheduler.py"]
