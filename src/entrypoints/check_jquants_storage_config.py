"""Report effective J-Quants storage destinations without API or data access."""

import json
import os

from src.config import config
from src.config.jquants_storage import inspect_jquants_storage


def main() -> int:
    audit = inspect_jquants_storage(config, os.environ)
    print(json.dumps(audit.as_dict(), sort_keys=True))
    return 0 if audit.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
