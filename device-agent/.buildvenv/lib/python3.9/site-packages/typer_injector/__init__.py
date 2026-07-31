'''Dependency injection for Typer.

This package provides FastAPI-style dependency injection for Typer CLI applications.

Example:
```
import socket
from typing import Annotated, TypeAlias

import typer
from typer_injector import Depends, InjectingTyper


app = InjectingTyper()


def address_dependency(
    host: Annotated[str, typer.Option()],
    port: Annotated[int, typer.Option()],
) -> tuple[str, int]:
    return host, port


Address: TypeAlias = Annotated[tuple[str, int], Depends(address_dependency)]


@app.command()
def send_message(message: str, address: Address) -> None:
    """Send a message to the specified address."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect(address)
        sock.send(message.encode())


@app.command()
def receive_message(address: Address) -> None:
    """Listen for a message at the specified address."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(address)
        sock.listen(1)
        conn, _ = sock.accept()
        with conn:
            data = conn.recv(1024)
            print(data.decode())


if __name__ == '__main__':
    app()
```
'''

from ._typer_injector import InjectingTyper
from ._types import Depends


__all__ = (
    'Depends',
    'InjectingTyper',
)
