"""Selfie prompt composer constants.

The SelfiePromptComposer class and its style/background/lighting dicts were
removed — nothing consumed them (selfie generation composes prompts via the
vision-analyzer JSON handoff / wildcard paths). Only DEFAULT_GENDER is still
imported (kling_gui/tabs/selfie_tab.py for the retained composer_gender config
compat field), so that's all this module keeps.
"""

DEFAULT_GENDER = "female"
