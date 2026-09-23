"""Source files must be plain ASCII.

Invisible or bidi characters in code can make it read differently from how it runs
("Trojan Source"). Use \\uXXXX escapes in strings instead.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY_FILES = sorted(
    p for d in ("app", "tests", "scripts", "migrations") for p in (ROOT / d).rglob("*.py")
)


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_python_source_is_ascii(path):
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        bad = [f"U+{ord(c):04X}" for c in line if ord(c) > 127]
        assert not bad, f"{path.name}:{n} contains non-ASCII {bad}; use \\u escapes"
