"""Component tests run against the real HA 2026.8.3 (pytest-homeassistant-custom-component pins it)."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request):
    try:
        yield request.getfixturevalue("enable_custom_integrations")
    except pytest.FixtureLookupError:  # plain pytest without the HA plugin: only the pure helper tests can run
        yield None


if sys.platform == "win32":
    try:
        import pytest_socket

        # Windows: asyncio's selector loop is built on a socket pair, which the HA plugin's disable_socket() would refuse.
        # The block stays in force in CI (Linux); locally nothing else opens sockets in these tests.
        pytest_socket.disable_socket = lambda *args, **kwargs: None
    except ImportError:
        pass

    @pytest.fixture(scope="session")
    def mock_zeroconf_resolver():
        """Windows: pycares opens a socket while pytest-socket is armed; the resolver is never exercised by these tests."""
        resolver = SimpleNamespace(close=lambda: None, real_close=AsyncMock())  # HA awaits real_close() at shutdown
        with patch("homeassistant.helpers.aiohttp_client._async_make_resolver", return_value=resolver) as patcher:
            yield patcher
