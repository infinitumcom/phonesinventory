"""typer_injector exceptions."""

from collections.abc import Callable
from typing import Any

from ._types import ParameterSourceEntry


class TyperInjectorError(Exception):
    """Base exception for Typer Injector errors."""


def _stringify_source(source: tuple[ParameterSourceEntry, ...], root_func: Any) -> str:
    """Create a string representation of a parameter source chain for error reporting."""
    source_str = ''
    indent = '    '
    depth = 1
    for entry in reversed(source):
        source_str += f'{entry.depends}\n{indent * depth}from parameter {entry.name!r} in '
        depth += 1
    return source_str + root_func.__qualname__


class ParameterNameConflictError(TyperInjectorError):
    """Raised when two parameters with the same name are found in a flattened signature."""

    def __init__(
        self,
        param_name: str,
        source_a: tuple[ParameterSourceEntry, ...],
        source_b: tuple[ParameterSourceEntry, ...],
        root_func: Callable[..., Any],
    ) -> None:
        """Create a new `ParameterNameConflictError`.

        :param param_name: The name of the conflicting parameter.
        :param source_a: The source chain of the first parameter.
        :param source_b: The source chain of the second parameter.
        :param root_func: The root function being flattened.
        """
        super().__init__(
            f'parameter {param_name!r} from {_stringify_source(source_a, root_func)}\n'
            f'conflicts with parameter {param_name!r} from {_stringify_source(source_b, root_func)}'
        )


class CircularDependencyError(TyperInjectorError):
    """Raised when a circular dependency is detected."""

    def __init__(self, chain: tuple[ParameterSourceEntry, ...], root_func: Callable[..., Any]) -> None:
        """Create a new `CircularDependencyError`.

        :param chain: The chain of dependencies that form the cycle.
        """
        super().__init__(f'Circular dependency detected: {_stringify_source(chain, root_func)}')
