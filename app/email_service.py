"""
Async email notifications via stdlib smtplib + asyncio.to_thread.
No extra dependencies required.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import settings

log = logging.getLogger(__name__)


def _send_sync(to: str, subject: str, html: str, text: str) -> None:
    """Blocking SMTP send — called via asyncio.to_thread."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    if settings.smtp_port == 465:
        # Implicit TLS
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port) as server:
            if settings.smtp_user and settings.smtp_pass:
                server.login(settings.smtp_user, settings.smtp_pass)
            server.sendmail(settings.smtp_from, to, msg.as_string())
    else:
        # Plain or STARTTLS (port 25 / 587)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.ehlo()
            if settings.smtp_port == 587:
                server.starttls()
                server.ehlo()
            if settings.smtp_user and settings.smtp_pass:
                server.login(settings.smtp_user, settings.smtp_pass)
            server.sendmail(settings.smtp_from, to, msg.as_string())


async def send_email(to: str, subject: str, html: str, text: str) -> None:
    """Send an email asynchronously. Logs errors without raising."""
    if not settings.smtp_host:
        log.debug("SMTP_HOST not configured — skipping email send")
        return
    try:
        await asyncio.to_thread(_send_sync, to, subject, html, text)
        log.info("Email sent to %s: %s", to, subject)
    except Exception as exc:
        log.error("Failed to send email to %s: %s", to, exc)


async def notify_admin_new_user(user_email: str) -> None:
    """Notify the admin that a new user is awaiting approval."""
    if not settings.admin_email:
        log.debug("ADMIN_EMAIL not configured — skipping admin notification")
        return

    portal_url = (settings.site_url.rstrip("/") + "/admin") if settings.site_url else "/admin"

    subject = f"[Yes Deputy] New user awaiting approval: {user_email}"

    text = (
        f"A new user has registered on Yes Deputy and is awaiting your approval.\n\n"
        f"Email: {user_email}\n\n"
        f"Open the admin portal to approve or reject this user:\n{portal_url}\n"
    )

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
             background: #f4f6fb; margin: 0; padding: 20px;">
  <div style="max-width: 520px; margin: 0 auto; background: #fff;
              border-radius: 10px; padding: 32px; box-shadow: 0 4px 12px rgba(0,0,0,0.08);">
    <h2 style="margin: 0 0 8px; color: #1a1d2e; font-size: 1.2rem;">
      New User Registration
    </h2>
    <p style="color: #6b7280; margin: 0 0 24px; font-size: 0.9rem;">
      Someone has registered on <strong>Yes Deputy</strong> and is waiting for access.
    </p>
    <table style="width: 100%; border-collapse: collapse; margin-bottom: 24px;">
      <tr>
        <td style="padding: 10px 12px; background: #f0f2f8; font-weight: 600;
                   font-size: 0.85rem; color: #1a1d2e; border-radius: 6px 0 0 6px;
                   white-space: nowrap; width: 1%;">Email</td>
        <td style="padding: 10px 12px; border: 1px solid #dde1ef; border-left: none;
                   font-size: 0.9rem; color: #1a1d2e; border-radius: 0 6px 6px 0;">
          {user_email}
        </td>
      </tr>
    </table>
    <a href="{portal_url}"
       style="display: inline-block; padding: 12px 28px; background: #4f46e5;
              color: #fff; text-decoration: none; border-radius: 7px;
              font-weight: 700; font-size: 0.95rem; letter-spacing: -0.01em;">
      Open Admin Portal →
    </a>
    <p style="color: #9ca3af; font-size: 0.78rem; margin: 28px 0 0;">
      Yes Deputy — Oireachtas Research Tool
    </p>
  </div>
</body>
</html>"""

    await send_email(settings.admin_email, subject, html, text)
