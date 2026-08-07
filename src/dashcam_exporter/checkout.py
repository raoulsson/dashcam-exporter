"""checkout.py — where this installation's files are, asked rather than reached for.

Every module used to work its own location out from `__file__` and then walk
up or down from it, which meant the layout of the checkout was written down
once per module and drifted the moment the sources moved under src/: the same
`Path(__file__).parent` meant the checkout in one file and the source
directory in another, and nothing said which was which.

So the layout is one object, and a module is handed one instead of computing
it. That also makes a test able to say where the checkout is -- point a Ctx at
a FakeCheckout and it reads that config.txt, not the operator's real one, with
no monkeypatching of module globals and nothing left behind for the next test.

Only root() is abstract. The derived paths hang off it and are deliberately
NOT overridable, because a second implementation that disagreed about where
config.txt lives would be a second answer to a question that must have one --
the point of the class is that the layout is stated once.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class Checkout(ABC):
    """Where this installation's files are, asked rather than reached for."""

    @abstractmethod
    def root(self) -> Path:
        """The checkout directory itself.

        Where config.txt, .env, .venv, VERSION and the git history live, and
        the directory a child process should run in. The tool's memory is
        named after it, so this is never the source directory.
        """

    def src(self) -> Path:
        """The one directory a plugin needs on sys.path.

        A plugin imports the public interface from `dashcam_exporter.uploader`.
        """
        return self.root() / "src"

    def config_file(self) -> Path:
        """The settings the operator edits. Configuration is not source, so it
        sits at the root rather than beside the modules that read it."""
        return self.root() / "config.txt"

    def env_file(self) -> Path:
        """The private keys, gitignored, overlaying config.txt."""
        return self.root() / ".env"


class RealCheckout(Checkout):
    """The checkout the running module was loaded from.

    Takes the module's own __file__ as an argument rather than reading a
    global, so that the one place a location is discovered is a construction
    site you can see, and so a test can construct one for any tree.
    """

    def __init__(self, module_file):
        self._module_file = Path(module_file)

    def root(self) -> Path:
        module = self._module_file.resolve()
        for parent in module.parents:
            if (parent / "src").is_dir():
                return parent
        raise RuntimeError(f"cannot locate checkout root for {module}")


class FakeCheckout(Checkout):
    """A checkout that is wherever a test says it is.

    It inherits every derived path unchanged, which is the guarantee worth
    having: a test tree has the same shape as a real one, so a test that
    passes against it is a test about the real layout.
    """

    def __init__(self, root):
        self._root = Path(root)

    def root(self) -> Path:
        return self._root
