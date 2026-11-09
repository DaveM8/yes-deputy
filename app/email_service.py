"""
Async email notifications via stdlib smtplib + asyncio.to_thread.
No extra dependencies required.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import settings

log = logging.getLogger(__name__)

# Seconds before giving up on a stalled SMTP server, so send threads can't pile up
_SMTP_TIMEOUT = 15


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
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=_SMTP_TIMEOUT) as server:
            if settings.smtp_user and settings.smtp_pass:
                server.login(settings.smtp_user, settings.smtp_pass)
            server.sendmail(settings.smtp_from, to, msg.as_string())
    else:
        # Plain or STARTTLS (port 25 / 587)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=_SMTP_TIMEOUT) as server:
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


def _site_link(path: str) -> str:
    return (settings.site_url or "").rstrip("/") + path


def _render(heading: str, paragraphs: list[str], button_text: str | None = None,
            button_url: str | None = None) -> tuple[str, str]:
    """
    Build the (html, text) bodies of an account email. Paragraphs are plain
    text and are escaped here. The link is also written out in full, for mail
    clients that strip buttons.
    """
    esc = lambda v: html_lib.escape(v, quote=True)
    text_parts = [heading, ""] + [p + "\n" for p in paragraphs]
    paras_html = "".join(
        f'<p style="color: #374151; margin: 0 0 16px; font-size: 0.92rem; line-height: 1.5;">{esc(p)}</p>'
        for p in paragraphs
    )
    button_html = ""
    if button_text and button_url:
        text_parts.append(f"{button_text}:\n{button_url}\n")
        button_html = f"""
    <a href="{esc(button_url)}"
       style="display: inline-block; padding: 11px 24px; background: #4f46e5;
              color: #fff; text-decoration: none; border-radius: 6px;
              font-weight: 600; font-size: 0.92rem;">{esc(button_text)}</a>
    <p style="color: #6b7280; margin: 20px 0 0; font-size: 0.78rem; word-break: break-all;">
      Or open this link: {esc(button_url)}
    </p>"""
    text_parts.append("Yes Deputy, Oireachtas research tool")
    html = f"""<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
             background: #f4f6fb; margin: 0; padding: 20px;">
  <div style="max-width: 520px; margin: 0 auto; background: #fff;
              border-radius: 8px; padding: 32px; border: 1px solid #dde1ef;">
    <h2 style="margin: 0 0 16px; color: #1a1d2e; font-size: 1.15rem;">{esc(heading)}</h2>
    {paras_html}{button_html}
    <p style="color: #9ca3af; font-size: 0.78rem; margin: 28px 0 0;">
      Yes Deputy, Oireachtas research tool
    </p>
  </div>
</body>
</html>"""
    return html, "\n".join(text_parts)


async def notify_admin_new_user(user_email: str) -> None:
    """Tell the admin that a user has confirmed their address and is awaiting approval."""
    if not settings.admin_email:
        log.debug("ADMIN_EMAIL not configured — skipping admin notification")
        return
    html, text = _render(
        "New user awaiting approval",
        [f"{user_email} has registered and confirmed their email address."],
        "Open the admin portal", _site_link("/admin"),
    )
    await send_email(settings.admin_email, f"[Yes Deputy] New user awaiting approval: {user_email}",
                     html, text)


# ── Emails to users ────────────────────────────────────────────────────────
# Tokens go in the URL fragment (#...), which browsers never send to the
# server, so they stay out of access logs. The page then POSTs the token.

async def send_verification(to: str, token: str) -> None:
    html, text = _render(
        "Confirm your email address",
        ["Thanks for registering with Yes Deputy. Please confirm this is your email address.",
         "After that, an admin will review your account, and we'll email you when it's approved.",
         "The link works for 48 hours. If you didn't register, ignore this email."],
        "Confirm email address", _site_link(f"/login#verify={token}"),
    )
    await send_email(to, "Yes Deputy: confirm your email address", html, text)


async def send_already_registered(to: str, token: str) -> None:
    html, text = _render(
        "You already have an account",
        ["Someone, probably you, tried to register on Yes Deputy with this email address, "
         "but it already has an account.",
         "If you've forgotten your password, you can set a new one with the link below. "
         "It works for 1 hour. Otherwise, ignore this email."],
        "Set a new password", _site_link(f"/login#reset={token}"),
    )
    await send_email(to, "Yes Deputy: you already have an account", html, text)


async def send_password_reset(to: str, token: str) -> None:
    html, text = _render(
        "Reset your password",
        ["We received a request to reset the password for your Yes Deputy account.",
         "The link works for 1 hour and can be used once. If you didn't ask for this, "
         "ignore this email; your password hasn't changed."],
        "Set a new password", _site_link(f"/login#reset={token}"),
    )
    await send_email(to, "Yes Deputy: reset your password", html, text)


async def send_password_changed(to: str) -> None:
    html, text = _render(
        "Your password was changed",
        ["The password for your Yes Deputy account was just changed, and other devices "
         "have been signed out.",
         "If this wasn't you, reset your password straight away."],
        "Reset password", _site_link("/login#forgot"),
    )
    await send_email(to, "Yes Deputy: your password was changed", html, text)


async def send_account_approved(to: str) -> None:
    html, text = _render(
        "Your account is approved",
        ["Your Yes Deputy account has been approved. You can now sign in."],
        "Sign in", _site_link("/login"),
    )
    await send_email(to, "Yes Deputy: your account is approved", html, text)


async def send_delete_confirmation(to: str, token: str) -> None:
    html, text = _render(
        "Confirm account deletion",
        ["We received a request to delete your Yes Deputy account. Deleting it removes your "
         "account and usage history and can't be undone.",
         "The link works for 1 hour. If you didn't ask for this, ignore this email and "
         "consider changing your password."],
        "Delete my account", _site_link(f"/login#delete={token}"),
    )
    await send_email(to, "Yes Deputy: confirm account deletion", html, text)


async def send_account_deleted(to: str) -> None:
    html, text = _render(
        "Your account has been deleted",
        ["Your Yes Deputy account and its usage history have been deleted. "
         "You're welcome to register again at any time."],
    )
    await send_email(to, "Yes Deputy: your account has been deleted", html, text)
