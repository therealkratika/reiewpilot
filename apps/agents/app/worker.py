import logging

from rq import Queue, Worker

from .job_queue import QUEUE_NAME, redis_connection


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    connection = redis_connection()
    worker = Worker([Queue(QUEUE_NAME, connection=connection)], connection=connection)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
