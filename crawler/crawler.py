import pika
import json
import time

def get_news_urls():
    """분석할 뉴스 URL 목록 (나중에 API에서 받아올 수도 있음)"""
    return [
        "https://news.naver.com/main/read.naver?mode=LSD&mid=sec&sid1=100",
        "https://news.naver.com/main/read.naver?mode=LSD&mid=sec&sid1=101",
        "https://news.naver.com/main/read.naver?mode=LSD&mid=sec&sid1=102",
    ]

def send_to_queue(urls):
    """뉴스 URL들을 RabbitMQ 큐에 넣기"""
    
    # RabbitMQ에 연결 (Docker 안에서는 'rabbitmq'라는 이름으로 접근)
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host='rabbitmq')
    )
    channel = connection.channel()
    
    # 'news_queue'라는 이름의 큐 만들기 (없으면 새로 만들고, 있으면 그대로 사용)
    channel.queue_declare(queue='news_queue', durable=True)
    
    # URL을 하나씩 큐에 넣기
    for i, url in enumerate(urls):
        message = json.dumps({
            "id": i + 1,
            "url": url,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        })
        
        channel.basic_publish(
            exchange='',
            routing_key='news_queue',
            body=message,
            properties=pika.BasicProperties(delivery_mode=2)  # 메시지 영구 저장
        )
        print(f"[크롤러] 큐에 전송 완료: 기사 {i + 1}")
    
    print(f"[크롤러] 총 {len(urls)}개 기사를 큐에 넣었습니다.")
    connection.close()

if __name__ == "__main__":
    print("[크롤러] 시작...")
    urls = get_news_urls()
    send_to_queue(urls)
    print("[크롤러] 완료!")
    