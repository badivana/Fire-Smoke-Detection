"""Gmail command line.

python -m app.gmail auth     # one-time OAuth consent -> secrets/token.json
python -m app.gmail sync     # fetch new mail matching GMAIL_QUERY into the database
"""

from __future__ import annotations

import sys

from app.core.config import get_settings
from app.core.errors import PipelineError
from app.core.logging import setup_logging
from app.db.session import get_sessionmaker
from app.gmail.auth import build_service, load_credentials, run_oauth_flow
from app.gmail.sync import sync_gmail


def main(argv: list[str]) -> int:
    settings = get_settings()
    setup_logging(settings.log_level)
    cmd = argv[0] if argv else ""
    try:
        if cmd == "auth":
            path = run_oauth_flow(settings)
            print(f"Token saved to {path} (permissions 0600). Do not commit it.")
            return 0
        if cmd == "sync":
            if settings.demo_mode:
                print("DEMO_MODE=true: Gmail sync is disabled. Set DEMO_MODE=false.")
                return 2
            service = build_service(load_credentials(settings))
            with get_sessionmaker()() as db:
                r = sync_gmail(db, service, settings)
            print(
                f"listed {r.listed}, created {r.created}, duplicates {r.duplicates}, "
                f"failed {len(r.failed)}"
            )
            return 0 if not r.failed else 1
    except PipelineError as e:
        print(f"{e.code.value}: {e.policy.user_message} {e.detail}")
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
