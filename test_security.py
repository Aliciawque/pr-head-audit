import json
import unittest
import urllib.error
from unittest.mock import patch
import pr_head_audit as audit


class SecurityTests(unittest.TestCase):
    def test_invalid_api_paths(self):
        for path in ['https://evil.example/', '//evil.example/', 'relative']:
            with self.subTest(path=path), self.assertRaises(audit.AuditError):
                audit.Client('synthetic-secret').get(path)

    def test_pagination_url_boundaries(self):
        for url in ['http://api.github.com/x', 'https://evil.example/x',
                    'https://api.github.com@evil.example/x', 'https://api.github.com/x#fragment']:
            client = audit.Client('synthetic-secret')
            with self.subTest(url=url), patch.object(client, 'get', return_value=([], f'<{url}>; rel="next"')):
                with self.assertRaises(audit.AuditError): client.pages('/x')

    def test_redirects_refused(self):
        with self.assertRaises(audit.AuditError):
            audit.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.example/')

    def test_network_exception_does_not_leak_secret(self):
        client = audit.Client('synthetic-secret')
        with patch.object(client.opener, 'open', side_effect=urllib.error.URLError('synthetic-secret')):
            with self.assertRaises(audit.AuditError) as error: client.get('/x')
        self.assertNotIn('synthetic-secret', str(error.exception))


if __name__ == '__main__':
    unittest.main()
