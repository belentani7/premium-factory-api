import unittest

from pf import notify


class TestNotify(unittest.TestCase):
    def test_summary_text_is_plain_and_complete(self):
        summary = {
            "mode": "api", "apply": True, "processed": 3,
            "counts": {"APPLIED": 2, "ELEVATED": 1},
            "pending_rate": ["o/late-repo"],
            "attention": [{"full_name": "o/x", "decision": "ELEVATED",
                           "reason": "protected branch"}],
        }
        text = notify.build_summary_text(summary)
        self.assertNotIn("#", text)          # no markdown, ever
        self.assertIn("APPLIED=2", text)
        self.assertIn("o/late-repo", text)
        self.assertIn("protected branch", text)

    def test_send_uses_plain_text_email(self):
        captured = {}

        class FakeSMTP:
            def __init__(self, host, port, context=None):
                captured["host"] = host

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def login(self, user, password):
                captured["user"] = user

            def send_message(self, message):
                captured["message"] = message

        original = notify.smtplib.SMTP_SSL
        notify.smtplib.SMTP_SSL = FakeSMTP
        try:
            ok = notify.send_summary_text(
                "pf run", "line1\nline2", host="smtp.test", port=465,
                user="bot@test", password="pw", to="human@test")
            self.assertTrue(ok)
        finally:
            notify.smtplib.SMTP_SSL = original
        message = captured["message"]
        self.assertEqual(message["Subject"], "pf run")
        self.assertEqual(message["To"], "human@test")
        body = message.get_content()
        self.assertEqual(message.get_content_type(), "text/plain")
        self.assertIn("line1", body)

    def test_missing_env_returns_clean_reason(self):
        sent, reason = notify.notify_from_env({}, environ={})
        self.assertFalse(sent)
        self.assertIn("PF_SMTP_HOST", reason)

    def test_smtp_failure_never_raises(self):
        class Boom:
            def __init__(self, *a, **k):
                raise OSError("network down")

        original = notify.smtplib.SMTP_SSL
        notify.smtplib.SMTP_SSL = Boom
        try:
            sent, reason = notify.notify_from_env(
                {}, environ={"PF_SMTP_HOST": "h", "PF_NOTIFY_TO": "t",
                             "PF_SMTP_USER": "u", "PF_SMTP_PASS": "p"})
        finally:
            notify.smtplib.SMTP_SSL = original
        self.assertFalse(sent)
        self.assertIn("smtp error", reason)


if __name__ == "__main__":
    unittest.main()
