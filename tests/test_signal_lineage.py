#!/config/quant_env/bin/python3
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.engines import signal_lineage as sl  # noqa: E402


class TestSignalLineage(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        sl.LINEAGE_LOG = os.path.join(self._tmpdir, "signal_lineage.jsonl")

    def test_append_and_timeline(self):
        lid = sl.append("TRIGGER", "test", code="000063", payload={"summary": "t1"})
        sl.append("GATE", "test", code="000063", lineage_id=lid, payload={"verdict": "APPROVE"})
        rows = sl.read_lineage(lid)
        self.assertEqual(len(rows), 2)
        text = sl.format_timeline(lid)
        self.assertIn("TRIGGER", text)
        self.assertIn("GATE", text)


if __name__ == "__main__":
    unittest.main()
