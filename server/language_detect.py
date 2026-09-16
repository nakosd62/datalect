"""
language_detect.py

Shared best-effort natural-language detection for every place this app
verifies a free-text LLM response was actually written in the user's own
language, rather than trusting a system-prompt instruction to be followed.

Split out into its own module (rather than living inline in
translate_routes.py, where this originated) because a SECOND call site
needed the exact same detection - connection_router.py's
triage_all_mode_question also produces free text ("answer"/"message") that
can drift into the wrong language under the identical "foreign-language
schema/data pulls the model along" failure mode translate_routes.py's own
post-execution summarization calls were fixed for first (see this module's
functions' own docstrings for that history). connection_router.py cannot
import FROM translate_routes.py (translate_routes.py already imports FROM
connection_router.py - see e.g. its own triage_all_mode_question usage -
so the reverse import would be circular), and duplicating the small pure
logic here would ALSO mean duplicating py3langid's real, ~1s model-load
cost at import time in two separate places - unlike this codebase's other
precedent for avoiding that same circular import
(connection_router.py/translate_routes.py each keep their own tiny
_drain_generation copy - see either one's docstring), that duplication
would be a real, measurable startup-cost regression, not just a few
duplicated lines. A standalone shared module both files import from avoids
both problems: one real model load per process (or per fresh_import()
cycle in tests - see tests/server/helpers.py's _APP_MODULE_NAMES, which
this module is included in for exactly that reason), with neither caller
importing from the other.
"""

from app_config import logger

# py3langid (a maintained fork of the older, now broken-on-modern-
# setuptools langid.py/langdetect) is used for this - pure Python, no
# network access needed at runtime (its language model ships as package
# data), and fast enough (~0.2ms/call once its model is loaded) to run on
# every summarization/translation/triage call with no perceptible latency
# added. Loading its model takes a real ~1s, so it's done exactly ONCE, at
# import time, via the module-level _LANGUAGE_IDENTIFIER below, not
# per-request - and, since every caller imports this same module rather
# than each loading its own copy, that ~1s is paid once per process
# regardless of how many call sites use detect_language().
try:
    from py3langid.langid import LanguageIdentifier as _LangIdentifier, MODEL_FILE as _LANGID_MODEL_FILE
    # norm_probs=True turns py3langid's raw per-class scores into an
    # actual normalized probability distribution across all languages it
    # knows, so `_MIN_LANGUAGE_CONFIDENCE` below is a real, comparable
    # threshold rather than an arbitrary raw-score cutoff - without this,
    # a short/degenerate input (e.g. "1", or a bare "SELECT 1") can still
    # report a "top" language with a raw score that looks confident but
    # isn't, since the raw scores aren't on a 0-1 scale at all.
    _LANGUAGE_IDENTIFIER = _LangIdentifier.from_model_file(_LANGID_MODEL_FILE, norm_probs=True)
except Exception:  # pragma: no cover - defensive only; see detect_language's docstring
    logger.warning("py3langid failed to load - language verification disabled", exc_info=True)
    _LANGUAGE_IDENTIFIER = None

# Deliberately non-exhaustive - just enough of py3langid's ~97 supported
# codes to give the model a readable name for the languages this app's
# users are actually likely to write questions in. describe_language()
# falls back to the bare code for anything not listed here, which is
# still meaningful to a model even when it's not friendly to a human
# skimming logs.
_LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "de": "German", "fr": "French", "it": "Italian",
    "pt": "Portuguese", "nl": "Dutch", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
    "fi": "Finnish", "pl": "Polish", "ru": "Russian", "uk": "Ukrainian", "cs": "Czech",
    "sk": "Slovak", "ro": "Romanian", "hu": "Hungarian", "el": "Greek", "tr": "Turkish",
    "ar": "Arabic", "he": "Hebrew", "hi": "Hindi", "bn": "Bengali", "ja": "Japanese",
    "ko": "Korean", "zh": "Chinese", "vi": "Vietnamese", "th": "Thai", "id": "Indonesian",
    "bg": "Bulgarian", "hr": "Croatian", "sr": "Serbian", "lt": "Lithuanian", "lv": "Latvian",
    "et": "Estonian", "fa": "Persian",
}

# Below this confidence (on py3langid's normalized 0-1 scale), a detection
# is treated as "unknown" rather than acted on - short or ambiguous text (a
# two-word question, a bare "SELECT 1", an empty string) genuinely can't be
# classified reliably, and guessing wrong here would either steer
# generation toward the wrong language or reject a perfectly correct
# response, so every call site skips the check entirely rather than trust
# a low-confidence guess.
_MIN_LANGUAGE_CONFIDENCE = 0.5


def detect_language(text):
    """Best-effort language code for `text` (py3langid's own code space -
    mostly ISO 639-1, a handful of 639-3 for languages with no 2-letter
    code), or None when detection isn't possible: py3langid itself failed
    to load (see the try/except above - degrades every caller's own
    language-verification feature to a no-op rather than crashing it),
    `text` is empty/whitespace-only, or the top result's confidence is
    below _MIN_LANGUAGE_CONFIDENCE."""
    if _LANGUAGE_IDENTIFIER is None:
        return None
    text = (text or "").strip()
    if not text:
        return None
    ranked = _LANGUAGE_IDENTIFIER.rank(text)
    if not ranked:
        return None
    code, confidence = ranked[0]
    return code if confidence >= _MIN_LANGUAGE_CONFIDENCE else None


def describe_language(code):
    """Human-readable English name for a detect_language() code, e.g.
    "de" -> "German" - used to give the model a concrete, named target
    ("Respond in German.") instead of only an indirect "same language as
    the question" framing. Falls back to the bare code for anything not in
    the (deliberately non-exhaustive) _LANGUAGE_NAMES map."""
    if not code:
        return None
    return _LANGUAGE_NAMES.get(code.lower(), code)
