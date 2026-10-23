"""Rate derivatives pricer: quant library, pricing service, SDK and dashboard.

Layers depend on each other in one direction only:
``ui -> sdk -> (HTTP) -> api -> services -> core`` (enforced by import-linter).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("rate-derivatives-pricer")
except PackageNotFoundError:  # pragma: no cover - only when running from an uninstalled source tree
    __version__ = "0.0.0"

__all__ = ["__version__"]
