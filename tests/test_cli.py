from typer.testing import CliRunner

from pcfs import __version__
from pcfs.cli import app


def test_version_command() -> None:
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_package_version_is_semantic() -> None:
    assert len(__version__.split(".")) == 3
