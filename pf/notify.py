"""Plain-text email notification. SMTP via environment variables only.

Required: PF_SMTP_HOST, PF_NOTIFY_TO
Optional: PF_SMTP_PORT (default 465/587), PF_SMTP_USER, PF_SMTP_PASS,
          PF_NOTIFY_FROM (default PF_SMTP_USER), PF_SMTP_SSL (default 1)
Never raises: a notification problem must not kill a batch.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage


def build_summary_text(summary):
    """Plain text run summary. No markdown, ever."""
    lines = []
    lines.append("premium-factory run summary")
    lines.append("mode=%s apply=%s" % (summary.get("mode"),
                                       "yes" if summary.get("apply") else "no"))
    counts = summary.get("counts", {})
    if counts:
        lines.append("decisions: " + ", ".join(
            "%s=%d" % (key, value) for key, value in sorted(counts.items())))
    lines.append("processed=%d" % summary.get("processed", 0))
    pending = summary.get("pending_rate") or []
    if pending:
        lines.append("pending (rate floor): %s" % ", ".join(pending))
    for record in summary.get("attention", []):
        lines.append("ATTENTION %s: %s (%s)"
                     % (record.get("full_name"), record.get("decision"),
                        record.get("reason", "")))
    return "\n".join(lines) + "\n"


def send_summary_text(subject, body, *, host, port, user, password, to,
                      from_addr=None, use_ssl=True):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_addr or user
    message["To"] = to
    message.set_content(body)  # plain text only
    if use_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context) as smtp:
            if user:
                smtp.login(user, password or "")
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if user:
                smtp.login(user, password or "")
            smtp.send_message(message)
    return True


def notify_from_env(summary, environ=None):
    """Best-effort notification. Returns (sent, reason)."""
    env = environ if environ is not None else __import__("os").environ
    host = env.get("PF_SMTP_HOST")
    to = env.get("PF_NOTIFY_TO")
    if not host:
        return False, "PF_SMTP_HOST not set"
    if not to:
        return False, "PF_NOTIFY_TO not set"
    user = env.get("PF_SMTP_USER", "")
    password = env.get("PF_SMTP_PASS", "")
    use_ssl = env.get("PF_SMTP_SSL", "1") not in ("0", "false", "no")
    port = int(env.get("PF_SMTP_PORT", "465" if use_ssl else "587"))
    body = build_summary_text(summary)
    subject = "premium-factory: %s" % (summary.get("title", "run"))
    try:
        send_summary_text(subject, body, host=host, port=port, user=user,
                          password=password, to=to,
                          from_addr=env.get("PF_NOTIFY_FROM"), use_ssl=use_ssl)
    except Exception as exc:  # notification must never kill a batch
        return False, "smtp error: %s" % exc
    return True, "sent to %s" % to
