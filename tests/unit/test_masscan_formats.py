import tempfile
from pathlib import Path
from unittest import TestCase

from pydantic import ValidationError
from scanlib.commands import build_masscan_command
from scanlib.masscan_formats import SAMPLE_BYTES, output_example, parse_records, read_output_sample
from scanlib.profile import ScanProfile


class MasscanFormatTests(TestCase):
    def parse(self, content, output_format):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output"
            path.write_text(content)
            return parse_records(path, output_format)

    def test_all_output_formats_discover_the_same_endpoint(self):
        expected = [{"ip_address": "192.0.2.10", "port": 10443, "protocol": "tcp", "service_name": None, "banner": None}]
        for output_format in ("json", "list", "xml"):
            with self.subTest(output_format=output_format):
                self.assertEqual(self.parse(output_example(output_format), output_format), expected)
                profile = ScanProfile(masscan={"output_format": output_format}).model_dump(mode="json")
                command = build_masscan_command(profile, ["192.0.2.0/24"], "/tmp/out")
                self.assertEqual(command[command.index("--output-format") + 1], output_format)

    def test_unknown_formats_rejected_before_execution(self):
        with self.assertRaises(ValidationError):
            ScanProfile(masscan={"output_format": "binary; echo bad"})

    def test_list_merges_separate_banner_events_and_ignores_closed(self):
        records = self.parse("#masscan\nopen tcp 10443 192.0.2.10 1700000000\n"
            "banner tcp 10443 192.0.2.10 1700000000 ssl TLS\\x20hello\n"
            "closed tcp 10443 192.0.2.11 1700000000\n# end\n", "list")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["service_name"], "ssl")
        self.assertEqual(records[0]["banner"], "TLS hello")

    def test_xml_merges_banner_events_and_ignores_closed(self):
        records = self.parse('<nmaprun><host><address addr="192.0.2.10"/><ports>'
            '<port protocol="tcp" portid="10443"><state state="open"/></port></ports></host>'
            '<host><address addr="192.0.2.10"/><ports><port protocol="tcp" portid="10443">'
            '<state state="open"/><service name="ssl" banner="TLS\\x20hello"/></port></ports></host>'
            '<host><address addr="192.0.2.11"/><ports><port protocol="tcp" portid="10443">'
            '<state state="closed"/></port></ports></host></nmaprun>', 'xml')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["banner"], "TLS hello")

    def test_xml_entity_declarations_and_invalid_list_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError, "entity declarations"):
            self.parse('<!DOCTYPE nmaprun [<!ENTITY x "unexpected">]><nmaprun/>', "xml")
        with self.assertRaisesRegex(ValueError, "Invalid Masscan list"):
            self.parse("unexpected parse format", "list")

    def test_sample_is_bounded_and_preserves_raw_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output"
            path.write_bytes(b"x" * (SAMPLE_BYTES * 3))
            sample, size = read_output_sample(path)
            self.assertEqual(size, SAMPLE_BYTES * 3)
            self.assertEqual(len(sample), SAMPLE_BYTES)
