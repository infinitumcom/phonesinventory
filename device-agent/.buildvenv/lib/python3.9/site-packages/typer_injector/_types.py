from dataclasses import dataclass
from typing import Any, Callable, NamedTuple


@dataclass(frozen=True)
class Depends:
    """Special annotation metadata to declare an injectable dependency.

    :param dependency: The dependency callable to be injected. Parameters of this callable will be "expanded"
        into the command function's signature.
    """

    dependency: Callable[..., Any]

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}({self.dependency.__qualname__})'


class ParameterSourceEntry(NamedTuple):
    name: str
    depends: Depends
