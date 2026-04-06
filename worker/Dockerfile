FROM python:3.11-slim

WORKDIR /app

RUN pip install pika newspaper3k lxml_html_clean scikit-learn numpy

COPY worker.py .

CMD ["python", "worker.py"]
