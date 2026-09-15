"""Entrypoint: consume CLOUDAMQP_PROCESS_QUEUE and write DB rows."""

from __future__ import annotations

import logging
import sys

from openfarm_common.mq import connection_label, consume_forever
from openfarm_common.settings import settings
from openfarm_common.trace import install_stdlib_trace_log_record

from app.writer import handle_result_message

install_stdlib_trace_log_record()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s trace_id=%(trace_id)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mq_result_writer")


def main() -> None:
    if not settings.cloudamqp_url:
        logger.error("CLOUDAMQP_URL is required for mq_result_writer")
        sys.exit(1)
    logger.info(
        "starting mq_result_writer broker=%s process_queue=%s",
        connection_label(),
        settings.cloudamqp_process_queue,
    )
    consume_forever(
        settings.cloudamqp_process_queue,
        handle_result_message,
        prefetch=1,
    )


if __name__ == "__main__":
    main()
