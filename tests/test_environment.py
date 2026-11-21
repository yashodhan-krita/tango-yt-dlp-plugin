"""Guard: the suite must exercise this checkout, not an installed copy.

``import yt_dlp`` installs yt-dlp's plugin finder, which serves
``yt_dlp_plugins`` from ``~/.config/yt-dlp/plugins/*`` ahead of
``PYTHONPATH``.  On a machine with the plugin installed, every other test
would then pass or fail on that copy.
"""

import os
import unittest

# Imported first, as the other test modules do: importing the plugin before
# yt_dlp would load the checkout by accident and hide the problem.
import yt_dlp  # noqa: F401
from yt_dlp_plugins.extractor import tango

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


class EnvironmentTest(unittest.TestCase):
    def test_the_module_under_test_is_this_checkout(self):
        module = os.path.realpath(tango.__file__)
        self.assertTrue(
            module.startswith(_REPO_ROOT + os.sep),
            f'the suite imported {module}, not the checkout at {_REPO_ROOT}. '
            'Run it as: XDG_CONFIG_HOME="$(mktemp -d)" PYTHONPATH=. '
            'python -m unittest discover -s tests')


if __name__ == '__main__':
    unittest.main()
