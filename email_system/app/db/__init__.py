from app.db import models  # noqa: F401  (register all tables on Base.metadata)
from app.db.base import Base

__all__ = ["Base"]
