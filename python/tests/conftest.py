import sys
from pathlib import Path

# Allow running pytest from the python/ project root without installing the package.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

import pytest  # noqa: E402

from espbridge.bridge import Bridge  # noqa: E402
from fake_firmware import FakeFirmware  # noqa: E402


@pytest.fixture()
def fw():
    fake = FakeFirmware()
    fake.boot()
    return fake


@pytest.fixture()
def bridge(fw):
    b = Bridge(transport=fw.transport, upgrade_baud=False, reset_on_open=False)
    yield b
    b.close()
