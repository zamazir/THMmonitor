import pika
import json


class RabbitMQClient:
    def __init__(self, config_file):
        with open(config_file) as f:
            recv = json.load(f)

        hostname = recv['hostname']
        port = recv['port']
        exchange = recv['exchange']
        bindingKey = recv['bindingKey']
        user = recv['user']
        password = recv['pass']

        self.queue = bindingKey
        credentials = pika.PlainCredentials(user, password)
        self.connection = pika.BlockingConnection(pika.ConnectionParameters(
            host=hostname, port=port, credentials=credentials))
        self.channel = self.connection.channel()
        self.channel.exchange_declare(exchange=exchange, exchange_type='direct')
        self.channel.queue_declare(queue=self.queue, durable=True)

        # Receive the messages from all subsystems
        keys = ["CDH", "HORST", "ADCS", "THM", "EPS", "COM", "PL"]
        for routing_key in keys:
            self.channel.queue_bind(exchange=exchange,
                               queue=self.queue,
                               routing_key=routing_key)

    def start(self, callback):
        self.channel.basic_consume(callback, self.queue, no_ack=True)
        self.channel.start_consuming()
