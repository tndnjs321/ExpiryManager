"""Offline FCM error regression tests. No database or network calls."""
import io
import json
import pathlib
import runpy
import unittest
import urllib.error
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent
dispatch = runpy.run_path(str(ROOT / "notify-prod.py"))
system = runpy.run_path(str(ROOT / "notify-system-prod.py"))
ROW = {
    "store_id": "11111111-1111-4111-8111-111111111111",
    "device_id": "22222222-2222-4222-8222-222222222222",
    "fcm_token": "synthetic-test-token",
    "expired_count": 1, "today_count": 0, "upcoming_count": 0,
}


def error(code, typed=None):
    detail = [] if typed is None else [{
        "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
        "errorCode": typed,
    }]
    body = json.dumps({"error": {"code": code, "details": detail}}).encode()
    return urllib.error.HTTPError("https://example.invalid", code, "", {}, io.BytesIO(body))


class FcmTests(unittest.TestCase):
    def test_dispatch_errors(self):
        cases = [(404, "UNREGISTERED", "unregistered"),
                 (404, None, "failed"),
                 (400, "INVALID_ARGUMENT", "failed"),
                 (403, "SENDER_ID_MISMATCH", "failed"),
                 (401, None, "failed"),
                 (429, "QUOTA_EXCEEDED", "unknown"),
                 (500, "INTERNAL", "unknown"),
                 (503, "UNAVAILABLE", "unknown"),
                 (400, "UNREGISTERED", "failed")]
        fn = dispatch["send"]
        for code, typed, expected in cases:
            with self.subTest(code=code, typed=typed):
                with patch("urllib.request.urlopen", side_effect=error(code, typed)), \
                     patch.dict(fn.__globals__, {"disable_unregistered": unittest.mock.Mock(return_value=True)}):
                    disable = fn.__globals__["disable_unregistered"]
                    self.assertEqual(fn(ROW, "synthetic-access"), expected)
                    self.assertEqual(disable.call_count, int(expected == "unregistered"))

    def test_system_errors(self):
        fn = system["send"]
        for code, typed, expected in [(404, "UNREGISTERED", "unregistered"),
                                      (404, None, "failed"),
                                      (400, "INVALID_ARGUMENT", "failed"),
                                      (503, "UNAVAILABLE", "failed")]:
            with self.subTest(code=code, typed=typed):
                disable = unittest.mock.Mock(return_value=True)
                with patch("urllib.request.urlopen", side_effect=error(code, typed)), \
                     patch.dict(fn.__globals__["notify"], {"disable_unregistered": disable}):
                    self.assertEqual(fn(ROW, "test", "test", "synthetic-access"), expected)
                    self.assertEqual(disable.call_count, int(expected == "unregistered"))

    def test_malformed_response(self):
        response = urllib.error.HTTPError("https://example.invalid", 404, "", {}, io.BytesIO(b"not json"))
        self.assertFalse(dispatch["is_unregistered"](response))

    def test_network_error_keeps_device(self):
        fn = dispatch["send"]
        disable = unittest.mock.Mock(return_value=True)
        with patch("urllib.request.urlopen", side_effect=OSError("offline")), \
             patch.dict(fn.__globals__, {"disable_unregistered": disable}):
            self.assertEqual(fn(ROW, "synthetic-access"), "unknown")
            disable.assert_not_called()

    def test_disable_uses_store_device_and_exact_token(self):
        fn = dispatch["disable_unregistered"]
        for response in ("t", "f"):
            fake_db = unittest.mock.Mock(return_value=response)
            with patch.dict(fn.__globals__, {"db": fake_db}):
                self.assertEqual(fn(ROW), response == "t")
                sql = fake_db.call_args.args[0]
                self.assertIn("bakery.disable_invalid_push_device", sql)
                self.assertIn(ROW["store_id"], sql)
                self.assertIn(ROW["device_id"], sql)
                self.assertIn("c3ludGhldGljLXRlc3QtdG9rZW4=", sql)
                self.assertNotIn(ROW["fcm_token"], sql)


if __name__ == "__main__":
    unittest.main()
