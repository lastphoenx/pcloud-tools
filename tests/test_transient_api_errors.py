"""Unit tests for retry-worthy API error classification."""

import errno
import socket
import unittest

import pcloud_bin_lib as pc


class TestTransientApiErrors(unittest.TestCase):
    def test_oserror_etimedout(self):
        exc = OSError(errno.ETIMEDOUT, "Connection timed out")
        self.assertTrue(pc.is_transient_api_error(exc))

    def test_oserror_errno_110(self):
        exc = OSError(110, "Connection timed out")
        self.assertTrue(pc.is_transient_api_error(exc))

    def test_socket_timeout(self):
        self.assertTrue(pc.is_transient_api_error(socket.timeout("timed out")))

    def test_auth_error_not_transient(self):
        exc = RuntimeError("uploadfile failed: {'result': 2008, 'error': 'Access denied'}")
        self.assertFalse(pc.is_transient_api_error(exc))


if __name__ == "__main__":
    unittest.main()
