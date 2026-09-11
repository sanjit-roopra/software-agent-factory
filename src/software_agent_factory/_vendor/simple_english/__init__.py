"""Reviewed SimpleEnglish linter subset.

Source: https://github.com/AminBlg/SimpleEnglish
Revision: 61ee200efbd423050aab982eed94226229891ae0
License: MIT
"""

from .lint import lint, prose_word_count

__all__ = ["lint", "prose_word_count"]
