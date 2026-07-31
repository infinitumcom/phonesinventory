from functools import wraps
from typing import Callable, TypeVar, cast

import typer

from typer_injector._inject import inject


_CommandDecoratorFactoryT = TypeVar(
    '_CommandDecoratorFactoryT',
    bound=Callable[
        ...,
        Callable[
            [Callable[..., object]],
            Callable[..., object],
        ],
    ],
)


def _make_injecting_command(super_method: _CommandDecoratorFactoryT) -> _CommandDecoratorFactoryT:
    """Wrap `Typer.command` while preserving its type signature."""

    @wraps(super_method)
    def injecting_command(*args, **kwargs):
        super_decorator = super_method(*args, **kwargs)

        def injecting_decorator(f):
            super_decorator(inject(f))
            # Return original, pre-injection function
            return f

        return injecting_decorator

    return cast(_CommandDecoratorFactoryT, injecting_command)


class InjectingTyper(typer.Typer):
    """Typer subclass with dependency injection support."""

    command = _make_injecting_command(typer.Typer.command)
    callback = _make_injecting_command(typer.Typer.callback)
