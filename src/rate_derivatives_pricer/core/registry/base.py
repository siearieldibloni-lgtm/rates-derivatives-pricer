"""Generic name -> component registry with decorator-based registration (Open/Closed principle).

Adding a component means adding a module that decorates its class with ``@REGISTRY.register(key)``;
no existing line has to change. Built-in components are imported lazily on the first lookup, so
the registry is always complete without creating import cycles between ``registry`` and the
packages that register into it.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterator
from threading import Lock
from typing import TypeVar

from rate_derivatives_pricer.core.errors import RatesPricerError

T = TypeVar("T")


class Registry[K: Hashable, V]:
    """Mapping from a stable key to a component (class, factory or instance).

    Args:
        kind: What the registry holds, used in error messages (e.g. ``"interpolator"``).
        error_type: Exception raised on an unknown key (a :class:`RatesPricerError` subclass).
        loader: Callable importing the built-in components, run once before the first lookup.
    """

    def __init__(
        self,
        kind: str,
        error_type: type[RatesPricerError],
        loader: Callable[[], None] | None = None,
    ) -> None:
        self._kind = kind
        self._error_type = error_type
        self._loader = loader
        self._loaded = loader is None
        self._lock = Lock()
        self._entries: dict[K, V] = {}

    @property
    def kind(self) -> str:
        """What the registry holds (e.g. ``"calendar"``)."""
        return self._kind

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if not self._loaded:
                # Flag first: the loader imports modules whose decorators call `add` re-entrantly.
                self._loaded = True
                assert self._loader is not None
                try:
                    self._loader()
                except BaseException:
                    # Allow a retry instead of leaving a silently half-filled registry.
                    self._loaded = False
                    raise

    def add(self, key: K, value: V) -> None:
        """Register ``value`` under ``key``.

        Raises:
            ValueError: If the key is already registered (a silent override would hide a bug).
        """
        if key in self._entries and self._entries[key] is not value:
            raise ValueError(f"duplicate {self._kind} registration for key {key!r}")
        self._entries[key] = value

    def register(self, key: K) -> Callable[[T], T]:
        """Class/function decorator registering the decorated object under ``key``."""

        def decorator(component: T) -> T:
            self.add(key, component)  # type: ignore[arg-type]
            return component

        return decorator

    def get(self, key: K) -> V:
        """Return the component registered under ``key``.

        Raises:
            RatesPricerError: The registry's ``error_type`` if the key is unknown.
        """
        self._ensure_loaded()
        try:
            return self._entries[key]
        except KeyError:
            raise self._error_type(
                f"unknown {self._kind}: {key!r}",
                {"kind": self._kind, "key": str(key), "available": [str(k) for k in self.keys()]},
            ) from None

    def keys(self) -> tuple[K, ...]:
        """Registered keys, in registration order."""
        self._ensure_loaded()
        return tuple(self._entries)

    def __contains__(self, key: object) -> bool:
        self._ensure_loaded()
        return key in self._entries

    def __iter__(self) -> Iterator[K]:
        return iter(self.keys())

    def __len__(self) -> int:
        return len(self.keys())
