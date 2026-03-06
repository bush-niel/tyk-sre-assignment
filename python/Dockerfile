FROM python:3.11-slim

WORKDIR /app

RUN useradd --create-home --shell /bin/bash appuser

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY app ./app

USER appuser

EXPOSE 8080

ENTRYPOINT ["python", "main.py"]

CMD ["--address", "0.0.0.0:8080"]
