import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def setup_logging(script_name: str, level: int = logging.INFO) -> logging.Logger:
    """Configure logging to write timestamped log files under LOG_DIR."""
    log_dir = Path(os.getenv("LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %Hh-%Mm")
    log_file = log_dir / f"{timestamp} {script_name}.log"

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=level,
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )

    return logging.getLogger(script_name)
