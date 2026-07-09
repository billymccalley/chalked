from __future__ import annotations

import os
import smtplib
import sqlite3
from email.message import EmailMessage

from .security import new_id, now_iso


def public_url() -> str:
    return os.environ.get("CHALKED_PUBLIC_URL", "http://127.0.0.1:8080").rstrip("/")


def smtp_enabled() -> bool:
    return bool(os.environ.get("CHALKED_SMTP_HOST"))


def send_email(conn: sqlite3.Connection, recipient: str, subject: str, body: str) -> dict:
    outbox_id = new_id("mail")
    status = "queued"
    error = None
    sent_at = None
    if smtp_enabled():
        try:
            msg = EmailMessage()
            msg["From"] = os.environ.get("CHALKED_MAIL_FROM", "Chalked <noreply@chalked.local>")
            msg["To"] = recipient
            msg["Subject"] = subject
            msg.set_content(body)
            host = os.environ["CHALKED_SMTP_HOST"]
            port = int(os.environ.get("CHALKED_SMTP_PORT", "587"))
            username = os.environ.get("CHALKED_SMTP_USERNAME")
            password = os.environ.get("CHALKED_SMTP_PASSWORD")
            with smtplib.SMTP(host, port, timeout=10) as smtp:
                if os.environ.get("CHALKED_SMTP_TLS", "1") != "0":
                    smtp.starttls()
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(msg)
            status = "sent"
            sent_at = now_iso()
        except Exception as exc:
            status = "failed"
            error = str(exc)[:500]
    conn.execute(
        """
        INSERT INTO email_outbox (id, recipient, subject, body, status, error, sent_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (outbox_id, recipient, subject, body, status, error, sent_at, now_iso()),
    )
    return {"id": outbox_id, "status": status, "error": error}
