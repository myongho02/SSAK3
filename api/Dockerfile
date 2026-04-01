FROM python:3.11-slim

WORKDIR /app

RUN pip install flask pika

COPY api_server.py .

CMD ["python", "api_server.py"]
