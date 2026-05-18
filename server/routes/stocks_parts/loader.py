"""
Loader utility for split stock route parts.

We intentionally keep exec-based loading to preserve the single shared
namespace semantics used by the existing part files.
"""

from pathlib import Path
from typing import Iterable


PART_FILES = (
    "part01_realtime_base.py",
    # part09: /api/themes/us, /api/themes/pairs 등 구체 경로 — part02의
    # /api/themes/{theme_name} 파라미터 라우트보다 먼저 등록해 shadow 방지
    "part09_us_themes.py",
    "part02_list_theme_market.py",
    "part03_search_news_live.py",
    "part04_ohlcv_rankings_basic.py",
    "part05_strength_timeline.py",
    "part06_surge_investor_program.py",
    "part07_limitup_pollers.py",
    "part08_home_ws_prewarm.py",
)


def _validate_parts(base: Path, part_files: Iterable[str]) -> None:
    names = list(part_files)
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        dup = ", ".join(sorted(duplicates))
        raise RuntimeError(f"Duplicate stocks part entries: {dup}")
    missing = [name for name in names if not (base / name).exists()]
    if missing:
        miss = ", ".join(missing)
        raise RuntimeError(f"Missing stocks part files: {miss}")


def load_split_parts(target_globals: dict) -> None:
    base = Path(__file__).parent
    _validate_parts(base, PART_FILES)
    for name in PART_FILES:
        part_path = base / name
        try:
            code = part_path.read_text(encoding="utf-8")
            exec(compile(code, str(part_path), "exec"), target_globals, target_globals)
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"Failed loading stocks part: {name}") from exc
