"""
prompt_loader.py

Tiny shared helper: loads a system instruction's actual wording from a
plain text file under server/prompts/, rather than a hardcoded Python
string literal buried inside translate_routes.py/connection_router.py.
The point is editability: a person can open one of those .txt files
directly and change the wording - no Python string escaping to fight
with (several of these prompts describe a JSON response contract inline,
which meant a lot of backslash-escaped quotes as hardcoded string
literals), and no code review of translate_routes.py/connection_router.py
needed just to tweak tone or wording.

Deliberately a standalone module with no other imports of its own:
translate_routes.py and connection_router.py both need this, and
translate_routes.py already imports FROM connection_router.py (see that
module's own docstring) - so this has to depend on neither of them, to
avoid introducing a circular import.

Every prompt file's content is returned EXACTLY as stored on disk - no
strip(), no reformatting. Each one intentionally keeps its own original
constant's exact trailing-newline convention (most end in "\n", matching
how these were originally written as a sequence of concatenated
"...line.\n" string literals; a couple don't, and this loader doesn't
second-guess that either way) - the file-based version of a prompt is
meant to be byte-for-byte what the old hardcoded constant was, not a
"cleaned up" rewrite of it.
"""
import os

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


def load_prompt(*relative_path_parts):
    """Reads and returns the text of server/prompts/<relative_path_parts
    joined together> - e.g. load_prompt("summary_single_connection.txt")
    or load_prompt("dialects", "postgresql.txt"). Raises FileNotFoundError
    at import time if the file is missing/misnamed - loudly and
    immediately, rather than a typo'd filename silently becoming an empty
    or wrong system instruction that only shows up once a real LLM call
    behaves strangely."""
    path = os.path.join(_PROMPTS_DIR, *relative_path_parts)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
