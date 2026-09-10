"""Entrypoint: consume CLOUDAMQP_TASK_QUEUE forever."""

from __future__ import annotations

import logging
import sys

from openfarm_common.mq import connection_label, consume_forever
from openfarm_common.settings import settings

from app.handler import handle_task_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mq_consumer")


def main() -> None:
    if not settings.cloudamqp_url:
        logger.error("CLOUDAMQP_URL is required for mq_consumer")
        sys.exit(1)
    logger.info(
        "starting mq_consumer broker=%s task_queue=%s",
        connection_label(),
        settings.cloudamqp_task_queue,
    )
    consume_forever(
        settings.cloudamqp_task_queue,
        handle_task_message,
        prefetch=1,
    )


if __name__ == "__main__":
    main()
