"""Shared test setup.

Every test runs with the network cut off: any attempt to open a socket
fails the test immediately. The pipeline talks to YouTube, ElevenLabs,
and video hosts in real use — none of that may happen from the test suite.
"""

import socket

import pytest


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("tests must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
