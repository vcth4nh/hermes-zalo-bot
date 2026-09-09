"""Official Zalo Bot API platform plugin for Hermes Agent."""


def register(ctx) -> None:
    """Plugin entry point called by Hermes.

    The adapter is imported lazily so ``zalo.api`` and ``zalo.inbound`` stay importable in
    environments without Hermes (their unit tests, the smoke script).
    """
    from .adapter import register as _register

    _register(ctx)


__all__ = ["register"]
