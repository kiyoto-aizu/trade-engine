"""Report effective J-Quants storage destinations without API or data access."""

import json
import os

from src.jquants_storage import default_jquants_storage_settings, inspect_jquants_storage


def main() -> int:
    audit = inspect_jquants_storage(default_jquants_storage_settings(), os.environ)
    print(json.dumps(audit.as_dict(), sort_keys=True))
    return 0 if audit.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
