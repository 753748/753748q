"""Console integration hook for the paragraph justification feature.

Importing this module installs ``Console.print_justified`` (see
:func:`rich._justify.install`).  It exists as a separate module for two reasons:

* :mod:`rich.console` can import it once, at the bottom of its own module, so
  that ``Console`` never grows a hard dependency on the layout engine at the top
  of the file;
* the layout engine and the hyphenation table are imported lazily, so a program
  that never justifies anything does not pay for them.

The hook is idempotent: importing it twice, or importing it after manually
calling :func:`rich._justify.install`, has no additional effect.
"""

from __future__ import annotations

from typing import Any

__all__ = ["install_console_methods", "installed"]

_installed = False


def install_console_methods(console_class: Any = None) -> bool:
    """Install the justification methods and report whether this call did it.

    Returns ``True`` only the first time it successfully installs, which makes
    the call safe to repeat from several import sites.
    """
    global _installed
    if _installed:
        return False
    from ._justify import install

    install(console_class)
    _installed = True
    return True


def installed() -> bool:
    """True when the console methods have already been installed."""
    return _installed


install_console_methods()
