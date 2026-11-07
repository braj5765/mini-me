"""Loads versioned prompt files (app/drafting/prompts/<version>/<name>.md) and fills {{placeholders}}."""
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).parent / "prompts"


@lru_cache
def load_prompt(version: str, name: str) -> str:
    return (_ROOT / version / f"{name}.md").read_text(encoding="utf-8")


def render(template: str, **values: str) -> str:
    out = template
    for k, v in values.items():
        out = out.replace("{{" + k + "}}", v)
    return out
