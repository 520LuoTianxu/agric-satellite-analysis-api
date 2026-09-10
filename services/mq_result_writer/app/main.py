"""Entrypoint: consume CLOUDAMQP_RESULT_QUEUE and write DB rows."""

from __future__ import annotations

import logging
import sys

from openfarm_common.mq import connection_label, consume_forever
from openfarm_common.settings import settings

from app.writer import handle_result_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mq_result_writer")


def main() -> None:
    if not settings.cloudamqp_url:
        logger.error("CLOUDAMQP_URL is required for mq_result_writer")
        sys.exit(1)
    logger.info(
        "starting mq_result_writer broker=%s result_queue=%s",
        connection_label(),
        settings.cloudamqp_result_queue,
    )
    consume_forever(
        settings.cloudamqp_result_queue,
        handle_result_message,
        prefetch=1,
    )


if __name__ == "__main__":
    main()
