"""Pytest configuration.

Makes `custom_components` importable as a package root, so tests can do
`from ha_zyxel.zyxel_ssh_api import ZyxelSSHAPI` etc.

This requires Home Assistant to be importable (e.g. `pip install
homeassistant` or `pip install pytest-homeassistant-custom-component`),
since importing the `ha_zyxel` package runs its `__init__.py` like any other
Home Assistant custom component - this is the standard, expected setup for
testing a HA custom integration, not a limitation specific to this project.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))
