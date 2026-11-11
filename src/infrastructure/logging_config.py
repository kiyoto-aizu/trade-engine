import logging
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from src.config import config
from src.infrastructure.monthly_log_storage import prepare_monthly_log_storage


def configure_logging() -> None:
    logging.root.handlers.clear()
    logging.root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logging.root.addHandler(stream_handler)

    if not config._is_test_runtime():
        try:
            prepare_monthly_log_storage(
                config.LOG_ROOT_DIRECTORY,
                config.LOG_ARCHIVE_DIRECTORY,
            )
        except (OSError, ValueError) as error:
            logging.warning(
                "月別ログの移行・アーカイブに失敗しました。既存ログを保持して処理を続けます: %s",
                error,
            )

    log_path = Path(config.LOG_DIRECTORY) / datetime.now().strftime("%Y%m") / Path(config.LOG_FILE_PATH).name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=max(config.LOG_BACKUP_COUNT, 31),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logging.root.addHandler(file_handler)