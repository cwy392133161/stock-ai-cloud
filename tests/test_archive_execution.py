import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

import pandas as pd

from archive_execution import CSV, FIELDS, FILENAME, merge_execution, publish, recover


class ExecutionArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.archive = root / "2026-11-13"
        self.archive.mkdir()
        self.bundle = root / "原始记录.zip"
        self.csv = ("代码,报价时间,最新价\n000001,20261113160000,12.01\n"
                    "600001,20261113160100,9.23\n").encode("utf-8-sig")
        with ZipFile(self.archive / "a-quotes.zip", "w") as z:
            z.writestr(CSV, self.csv)
            z.writestr("quality.json", b"{}")
        self.frame = pd.DataFrame({"代码": ["600001", "000001"], "报价时间": ["20261113160100", "20261113160000"],
                                   "最新价": [9.23, 12.01]})

    def write_source(self, *, stamp="20261113160000", close="12.01", symbols="sz000001,sh600001", csv=None):
        def line(symbol, t, p, traded):
            fields = ["1"] * 60
            fields[1:6] = ["示例", symbol[2:], p, "11.01", "12.0" if traded else "0"]
            fields[30] = t
            fields[33:35] = ["13", "11.55"] if traded else ["0", "0"]
            fields[47:49] = ["-1", "-1"] if traded else ["12", "8"]
            return 'v_' + symbol + '="' + "~".join(fields) + '";'
        payload = (line("sz000001", stamp, close, True) + "\n" +
                   line("sh600001", "20261113160100", "9.23", False)).encode("gbk")
        log = {"file": "001.gbk", "http_status": 200, "url": "https://qt.gtimg.cn/q=" + symbols,
               "observed": "2026-11-15T10:00:00+08:00"}
        with ZipFile(self.bundle, "w") as z:
            z.writestr("A股/" + CSV, self.csv if csv is None else csv)
            z.writestr("A股/quality.json", b"{}")
            z.writestr("A股/requests.jsonl", json.dumps(log))
            z.writestr("A股/raw/001.gbk", payload)
        return payload

    def test_recover_provenance_original_codes_negative_and_zero_fields(self):
        payload = self.write_source()
        self.assertIs(merge_execution(self.archive, self.frame), self.frame)
        saved = recover(self.archive, self.bundle)
        self.assertEqual(saved["sources"]["001.gbk"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(saved["positive_counts"]["今开"], 1)
        output = self.archive / FILENAME
        publish(saved, output)
        original = output.read_bytes()
        publish(saved, output)
        with self.assertRaisesRegex(ValueError, "不同"):
            publish(dict(saved, source_bundle="另一期"), output)
        self.assertEqual(output.read_bytes(), original)
        combined = merge_execution(self.archive, self.frame)
        self.assertEqual(combined.columns.tolist(), [*self.frame.columns, *FIELDS])
        self.assertEqual(combined["今开"].tolist(), [0, 12])
        self.assertEqual(combined["涨停价"].tolist(), [12, -1])
        with self.assertRaisesRegex(ValueError, "时间"):
            merge_execution(self.archive, self.frame.assign(报价时间="20261113170000"))
        saved["positive_counts"]["今开"] = 2
        output.write_bytes(gzip.compress(json.dumps(saved).encode()))
        with self.assertRaisesRegex(ValueError, "覆盖"):
            merge_execution(self.archive, self.frame)

    def test_mismatched_archive_code_close_or_stamp_aborts_before_writing(self):
        for changes in ({"csv": b"wrong"}, {"symbols": "sz000001"}, {"close": "12.02"},
                        {"stamp": "20261112160000"}, {"stamp": "20261113151000"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.write_source(**changes)
                recover(self.archive, self.bundle)
        self.assertFalse((self.archive / FILENAME).exists())
