"""Engine-agnostic AI translation and three-pass proofreading for game text."""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Config, ConfigError, config_from_dict, load_config
from .glossary import Glossary, Term
from .proofread import Proofreader
from .providers import (
    AuthError,
    Completion,
    ContextOverflow,
    Provider,
    ProviderError,
    QuotaExhausted,
    RateLimited,
    TransientError,
    create_provider,
)
from .records import Project, Record
from .store import Store
from .text import TagMasker, Validator
from .translate import Report, Translator

__all__ = [
    "AuthError", "Completion", "Config", "ConfigError", "ContextOverflow", "Glossary", "Project",
    "Proofreader", "Provider", "ProviderError", "QuotaExhausted", "RateLimited", "Record", "Report",
    "Store", "TagMasker", "Term", "TransientError", "Translator", "Validator", "__version__",
    "config_from_dict", "create_provider", "load_config",
]
