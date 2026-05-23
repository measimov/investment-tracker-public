import logging
import os
from logging.config import dictConfig


LOGGER_NAMESPACE = "investment_tracker"


def configure_logging(log_dir: str = "logs", level: str = "INFO") -> None:
    os.makedirs(log_dir, exist_ok=True)

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {"format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"},
                "auth": {"format": "%(asctime)s - %(levelname)s - %(message)s"},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                },
                "app_file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": os.path.join(log_dir, "app.log"),
                    "maxBytes": 10_485_760,
                    "backupCount": 10,
                    "formatter": "standard",
                },
                "auth_file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": os.path.join(log_dir, "auth.log"),
                    "maxBytes": 10_485_760,
                    "backupCount": 10,
                    "formatter": "auth",
                },
            },
            "root": {
                "level": level,
                "handlers": ["console", "app_file"],
            },
            "loggers": {
                f"{LOGGER_NAMESPACE}.auth": {
                    "level": level,
                    "handlers": ["auth_file"],
                    "propagate": True,
                }
            },
        }
    )


def get_app_logger(module_name: str | None = None) -> logging.Logger:
    if not module_name or module_name == "__main__":
        return logging.getLogger(LOGGER_NAMESPACE)

    if module_name.startswith("app."):
        module_name = module_name.removeprefix("app.")

    return logging.getLogger(f"{LOGGER_NAMESPACE}.{module_name}")
