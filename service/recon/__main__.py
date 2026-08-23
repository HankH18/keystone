"""`python -m recon` -- the service command line.

A thin Typer shell so downstream tickets add commands rather than invent a CLI.
"""

from __future__ import annotations

import typer

from recon import __version__
from recon.app import create_app
from recon.logging import configure_logging_once

cli = typer.Typer(add_completion=False, no_args_is_help=True, help="Keystone service CLI.")


@cli.callback()
def _bootstrap() -> None:
    """Install the privacy-safe logging chain before any command runs.

    Typer invokes this for every subcommand, so no command can be added that
    forgets it (`recon.logging.ENTRY_POINTS`).
    """
    configure_logging_once()


@cli.command()
def version() -> None:
    """Print the service version."""
    typer.echo(__version__)


@cli.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Run the HTTP service."""
    import uvicorn

    if reload:
        uvicorn.run("recon.app:create_app", factory=True, host=host, port=port, reload=True)
    else:
        uvicorn.run(create_app(), host=host, port=port)


def main() -> None:
    """Console-script entry point."""
    cli()


if __name__ == "__main__":
    main()
