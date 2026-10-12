"""Regression checks for real HTTPX JSONL to confirmed ip(2) classification."""
import json
import tempfile
import unittest
from pathlib import Path

from app.main import best_endpoint_results, httpx_command, parse_httpx_output, results_for_chunk


MATCHES = [
    "GlobalProtect Portal",
    'id="login_form"',
    'name="prot"',
    "/global-protect/portal/css/login.css",
    "/global-protect/portal/images/logo-pan-",
    'id="heading"',
]


class HttpxTransitionTests(unittest.TestCase):
    def config(self):
        return {
            "paths": ["/global-protect/login.esp"],
            "threads": 1,
            "timeout": 5,
            "match_strings": MATCHES,
            "match_policy": "all",
            "status_codes": [200],
            "title_contains": ["GlobalProtect Portal"],
        }

    def parse(self, body):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "httpx.jsonl"
            path.write_text(json.dumps({
                "input": "https://192.0.2.7:443",
                "url": "https://192.0.2.7:443/global-protect/login.esp",
                "status_code": 200,
                "title": "GlobalProtect Portal",
                "body": body,
            }) + "\n", encoding="utf-8")
            return parse_httpx_output(path, 3, self.config())[0]

    def test_relative_logo_path_confirms_globalprotect(self):
        body = '''<title>GlobalProtect Portal</title>
          <form id="login_form"><input name="prot"></form><h1 id="heading"></h1>
          <link href="/global-protect/portal/css/login.css" rel="stylesheet">
          <img src="portal/images/logo-pan-example.svg">'''
        result = self.parse(body)
        self.assertTrue(result["matched"])
        self.assertEqual(result["url"], "https://192.0.2.7:443")
        self.assertEqual(result["match_details"], MATCHES)

    def test_missing_logo_stays_unconfirmed(self):
        body = '''<title>GlobalProtect Portal</title>
          <form id="login_form"><input name="prot"></form><h1 id="heading"></h1>
          <link href="/global-protect/portal/css/login.css" rel="stylesheet">'''
        result = self.parse(body)
        self.assertFalse(result["matched"])
        self.assertNotIn(MATCHES[4], result["match_details"])

    def test_httpx_retains_unmatched_responses_for_review(self):
        command = httpx_command(Path("input.txt"), Path("output.jsonl"), self.config())
        self.assertNotIn("-match-string", command)
        self.assertIn("-include-response", command)

    def test_redirect_cannot_create_result_for_an_unscanned_endpoint(self):
        responses = [
            {"ip": "192.0.2.7", "port": 443},
            {"ip": "192.0.2.8", "port": 443},
            {"ip": "192.0.2.7", "port": 8443},
        ]
        claimed = [{"ip": "192.0.2.7", "port": 443}]
        self.assertEqual(results_for_chunk(responses, claimed), responses[:1])

    def test_unmatched_path_cannot_overwrite_a_matching_path(self):
        base = self.parse('''<title>GlobalProtect Portal</title><form id="login_form">
            <input name="prot"></form><h1 id="heading"></h1>
            <link href="/global-protect/portal/css/login.css">
            <img src="portal/images/logo-pan-example.svg">''')
        unmatched = {**base, "matched": False, "match_details": []}
        self.assertEqual(best_endpoint_results([base, unmatched]), [base])
        self.assertEqual(best_endpoint_results([unmatched, base]), [base])


if __name__ == "__main__":
    unittest.main()
