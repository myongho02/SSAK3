FROM python:3.11-slim

WORKDIR /app

RUN pip install pika transformers torch scikit-learn numpy

COPY worker.py .

CMD ["python", "worker.py"]
