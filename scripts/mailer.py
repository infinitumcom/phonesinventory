#!/usr/bin/env python3
"""
PhonesInventory Mailer
Gmail SMTP sender for PIN reset codes and PIN change notices.
Config via .env: SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / MAIL_FROM_NAME
SMTP_PASS empty = mail service not configured (endpoints should degrade gracefully).
"""
import os
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

import env_loader  # noqa: F401  (auto-loads .env on import)

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "noreply@phonesinventory.com")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "PhonesInventory")

PST = timezone(timedelta(hours=-7))

BRAND_GREEN = "#14532d"


def is_configured():
    """Mail service is usable only when an SMTP app password is present."""
    return bool(SMTP_PASS.strip())


def send_email(to, subject, html):
    """Send an HTML email. Raises on failure (caller decides how to degrade)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((MAIL_FROM_NAME, SMTP_USER))
    msg["To"] = to
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [to], msg.as_string())


def _wrap(title_zh, title_en, body_html):
    """Shared bilingual email shell, brand green header."""
    return f"""\
<div style="max-width:520px;margin:0 auto;font-family:-apple-system,'PingFang SC','Microsoft YaHei',Helvetica,Arial,sans-serif;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;">
  <div style="background:{BRAND_GREEN};padding:18px 24px;">
    <span style="display:inline-block;width:28px;height:28px;line-height:28px;text-align:center;background:#fff;color:{BRAND_GREEN};font-weight:800;border-radius:8px;font-size:16px;">P</span>
    <span style="color:#fff;font-weight:700;font-size:16px;margin-left:10px;vertical-align:middle;">PhonesInventory<span style="opacity:.7;">.com</span></span>
  </div>
  <div style="padding:24px;">
    <h2 style="margin:0 0 4px;font-size:17px;color:#111827;">{title_zh}</h2>
    <p style="margin:0 0 18px;font-size:13px;color:#6b7280;">{title_en}</p>
    {body_html}
    <p style="margin:22px 0 0;font-size:11.5px;color:#9ca3af;border-top:1px solid #f3f4f6;padding-top:14px;">
      此邮件由系统自动发送，请勿回复。This is an automated message from PhonesInventory (iFixForU internal), please do not reply.
    </p>
  </div>
</div>"""


def send_pin_reset_code(to, name, code):
    """PIN reset verification code — valid 10 minutes."""
    body = f"""\
    <p style="font-size:14px;color:#374151;margin:0 0 6px;">你好 {name}，你正在重置登录密码。验证码：</p>
    <p style="font-size:13px;color:#6b7280;margin:0 0 16px;">Hi {name}, you requested a PIN reset. Your verification code:</p>
    <div style="text-align:center;margin:18px 0;">
      <span style="display:inline-block;background:#f0fdf4;color:{BRAND_GREEN};font-size:32px;font-weight:800;letter-spacing:8px;padding:14px 26px;border-radius:10px;border:1px solid #bbf7d0;">{code}</span>
    </div>
    <p style="font-size:13px;color:#374151;margin:0 0 4px;">验证码 <b>10 分钟</b>内有效。如非本人操作，请忽略此邮件，你的密码不会被更改。</p>
    <p style="font-size:12.5px;color:#6b7280;margin:0;">The code expires in <b>10 minutes</b>. If you didn't request this, you can safely ignore this email.</p>"""
    send_email(to, "PhonesInventory 密码重置验证码 / PIN Reset Code", _wrap("密码重置验证码", "PIN Reset Code", body))


def send_pin_changed_notice(to, name):
    """Notice that the account PIN was just changed."""
    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M")
    body = f"""\
    <p style="font-size:14px;color:#374151;margin:0 0 6px;">你好 {name}，你的登录密码已于 <b>{now}</b>（美西时间）修改成功。</p>
    <p style="font-size:13px;color:#6b7280;margin:0 0 16px;">Hi {name}, your login PIN was changed at <b>{now}</b> (US Pacific).</p>
    <p style="font-size:13px;color:#b91c1c;margin:0 0 4px;">⚠️ 如果这不是你本人的操作，请立即联系管理员 Andy。</p>
    <p style="font-size:12.5px;color:#6b7280;margin:0;">If this wasn't you, contact admin Andy immediately.</p>"""
    send_email(to, "PhonesInventory 登录密码已修改 / Your PIN Was Changed", _wrap("登录密码已修改", "Your PIN Was Changed", body))
