FROM python:3.11-slim

WORKDIR /app

# flask: 웹 / pika: RabbitMQ / bcrypt: 패스워드 해싱(Phase G 사용자 격리)
RUN pip install flask pika bcrypt

COPY api_server.py .

CMD ["python", "api_server.py"]
