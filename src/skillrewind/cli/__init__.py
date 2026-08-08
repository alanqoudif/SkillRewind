"""SkillRewind CLI package.

``main``/``run`` currently resolve to the full CLI in
:mod:`skillrewind.cli.app`, which preserves the legacy ``closure``/``attest``
commands from v0.1 alongside the v0.2 command families.
"""

from .app import main, run

__all__ = ["main", "run"]
