"""Unit tests for registry.py. No network: DNS and status endpoints are not asked.

Run: python -m unittest discover -s tools -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402


def host_errors(host: str) -> list[str]:
    report = registry.Report()
    registry.check_host(registry.Server("servers/europe/moscow-9.json", {"host": host}), report)
    return [message for _, message in report.errors]


class CheckHostTests(unittest.TestCase):
    def test_dns_name_and_public_ipv4_are_accepted(self):
        for host in ("moscow-1.play.haulnet.cc", "msk2.example-team.org", "8.8.8.8"):
            self.assertEqual(host_errors(host), [], host)

    def test_ipv6_address_is_refused(self):
        # The monitor checks over IPv4 only, so even a public IPv6 address cannot be listed as the host.
        for host in ("2a00:b700:2::1de", "2001:4860:4860::8888", "::1"):
            errors = host_errors(host)
            self.assertEqual(len(errors), 1, host)
            self.assertIn("is an IPv6 address", errors[0])
            self.assertIn("reachable over IPv4", errors[0])

    def test_non_public_ipv4_is_refused(self):
        for host in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "100.64.0.1", "169.254.1.1", "192.0.2.1"):
            errors = host_errors(host)
            self.assertEqual(len(errors), 1, host)
            self.assertIn("is not a public address", errors[0])

    def test_reserved_and_malformed_names_are_refused(self):
        for host in ("localhost", "server.local", "msk1.example.org", "Moscow.example-team.org", "1.2.3"):
            self.assertTrue(host_errors(host), host)


class RepositoryTests(unittest.TestCase):
    def test_entries_of_this_repository_are_valid(self):
        report, _, _, servers = registry.validate(base=None, dns=False)
        self.assertEqual(report.errors, [])
        self.assertTrue(servers)


if __name__ == "__main__":
    unittest.main()
