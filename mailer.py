"""Outgoing email (BHW/responder approval notices) over SMTP.
Password reset emails are sent by Firebase Authentication instead (auth_accounts.py).

Configure in .env (Gmail example -- use an App Password, not your normal
Gmail password: Google Account > Security > 2-Step Verification > App passwords):

    MAIL_USERNAME=youraccount@gmail.com
    MAIL_PASSWORD=your-16-char-app-password
    MAIL_FROM=ALISTO <youraccount@gmail.com>     (optional)
    MAIL_SERVER=smtp.gmail.com                    (optional, default)
    MAIL_PORT=587                                 (optional, default)

If MAIL_USERNAME / MAIL_PASSWORD are missing, emails are skipped and a
warning is printed -- the rest of the app keeps working.
"""
import os
import smtplib
import threading
from email.message import EmailMessage
from html import escape


def is_configured():
    return bool(os.environ.get('MAIL_USERNAME') and os.environ.get('MAIL_PASSWORD'))


def _send_now(to, subject, text, html):
    username = os.environ.get('MAIL_USERNAME')
    # Google shows App Passwords as "abcd efgh ijkl mnop"; the spaces aren't part of it.
    password = (os.environ.get('MAIL_PASSWORD') or '').replace(' ', '')
    if not (username and password):
        print(f'[mailer] MAIL_USERNAME/MAIL_PASSWORD not set; skipped email to {to}: {subject}')
        return False

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = os.environ.get('MAIL_FROM') or f'ALISTO <{username}>'
    msg['To'] = to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype='html')

    server = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    port = int(os.environ.get('MAIL_PORT', '587'))
    try:
        if port == 465:
            with smtplib.SMTP_SSL(server, port, timeout=20) as smtp:
                smtp.login(username, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(server, port, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(username, password)
                smtp.send_message(msg)
        return True
    except Exception as e:
        print(f'[mailer] Failed to send email to {to}: {e}')
        return False


def send(to, subject, text, html=None, background=True):
    """Sends an email. In the background by default so a slow SMTP server
    never holds up the page. Returns False right away if mail isn't set up."""
    if not to:
        return False
    if not is_configured():
        print(f'[mailer] MAIL_USERNAME/MAIL_PASSWORD not set; skipped email to {to}: {subject}')
        return False
    if background:
        threading.Thread(target=_send_now, args=(to, subject, text, html),
                         daemon=True).start()
        return True
    return _send_now(to, subject, text, html)


def simple_html(heading, paragraphs, button_text=None, button_url=None):
    """A small branded HTML email body."""
    body = ''.join(f'<p style="margin:0 0 14px;line-height:1.5">{escape(p)}</p>'
                   for p in paragraphs)
    button = ''
    if button_text and button_url:
        button = (f'<p style="margin:22px 0"><a href="{escape(button_url)}" '
                  'style="background:#2f6fed;color:#fff;text-decoration:none;'
                  'padding:12px 22px;border-radius:8px;font-weight:600;'
                  f'display:inline-block">{escape(button_text)}</a></p>')
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;background:#f5f7fb;padding:24px">'
        '<div style="max-width:520px;margin:0 auto;background:#fff;border-radius:12px;'
        'padding:28px;color:#1f2937">'
        '<div style="font-size:20px;font-weight:700;color:#2f6fed;margin-bottom:18px">ALISTO</div>'
        f'<h2 style="margin:0 0 16px;font-size:18px">{escape(heading)}</h2>'
        f'{body}{button}'
        '<p style="margin:24px 0 0;font-size:12px;color:#6b7280">'
        'This is an automated message from ALISTO. Please do not reply.</p>'
        '</div></div>'
    )
