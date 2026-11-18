import html
import json
import smtplib
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional


def build_license_email_content(
    customer_name: str,
    license_key: str,
    mt5_login: str,
    plan_type: str,
    bot_names: List[str],
    expires_at: int,
    download_url: str,
    contact_email: str,
    order_ref: str = "",
) -> Dict[str, str]:
    safe_name = html.escape(customer_name or "Trader")
    safe_key = html.escape(license_key)
    safe_mt5 = html.escape(str(mt5_login))
    safe_dl = html.escape(download_url or "https://aurumdesk.com")
    safe_contact = html.escape(contact_email or "eliodemaj07@gmail.com")
    safe_ref = html.escape(order_ref or "MANUAL")

    plan_label = "Lifetime License (Never Expires)"
    if plan_type == "monthly" and expires_at > 0:
        exp_str = datetime.fromtimestamp(int(expires_at), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        plan_label = f"30-Day Subscription (Valid until {exp_str})"

    bots_html = "".join(
        f'<li style="margin:4px 0;color:#eef2f7;">&#9989; <strong>{html.escape(b)}</strong></li>'
        for b in (bot_names or [])
    )
    bots_text = "\n".join(f"  - {b}" for b in (bot_names or []))

    subject = f"Your Aurum Desk Unique License Key ({license_key})"
    text_body = (
        f"Hello {customer_name or 'Trader'},\n\n"
        f"Thank you for choosing Aurum Desk!\n"
        f"Your payment has been approved and your license is now active.\n\n"
        f"=== YOUR LICENSE DETAILS ===\n"
        f"Order Reference : {order_ref or 'MANUAL'}\n"
        f"Unique Key      : {license_key}\n"
        f"Locked MT5 Acct : #{mt5_login} (1 MT5 account limit)\n"
        f"Plan            : {plan_label}\n"
        f"Unlocked EAs    :\n{bots_text}\n\n"
        f"=== HOW TO ACTIVATE ===\n"
        f"1. Download Aurum Desk: {download_url}\n"
        f"2. Extract the ZIP folder on your Windows PC.\n"
        f"3. Open MetaTrader 5 logged into account #{mt5_login} and enable 'Algo Trading'.\n"
        f"4. Launch Aurum_Desk.vbs and paste your Unique Key above.\n\n"
        f"You can also view your keys and download link anytime in My Account on the store.\n"
        f"Need help or need to switch your MT5 account? Reply to {contact_email}.\n"
        f"Best regards,\nAurum Desk Team\n"
    )

    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0b111d;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#eef2f7;">
  <div style="max-width:600px;margin:24px auto;background:#111827;border:1px solid rgba(245,213,122,0.35);border-radius:14px;overflow:hidden;">
    <div style="background:linear-gradient(135deg,#141d2e,#1e1910);padding:22px 26px;border-bottom:1px solid rgba(245,213,122,0.22);">
      <div style="font-size:21px;font-weight:800;color:#f5d57a;letter-spacing:0.6px;">AURUM DESK</div>
      <div style="font-size:12px;color:#9aa5b5;margin-top:4px;">Institutional Gold Trading Terminal • License Delivery</div>
    </div>
    <div style="padding:24px 26px;">
      <p style="margin:0 0 14px;font-size:15px;">Hello <strong>{safe_name}</strong>,</p>
      <p style="margin:0 0 18px;font-size:14px;line-height:1.55;color:#cfd6e0;">
        Thank you for your order (<strong>{safe_ref}</strong>). Your payment has been verified and your Unique License Key is ready below.
      </p>

      <div style="background:#0a0f18;border:1px solid rgba(245,213,122,0.55);border-radius:10px;padding:16px 18px;text-align:center;margin-bottom:18px;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.1px;color:#9aa5b5;margin-bottom:6px;">Your Unique License Key</div>
        <div style="font-family:'Courier New',monospace;font-size:22px;font-weight:800;color:#f5d57a;letter-spacing:1.4px;">{safe_key}</div>
        <div style="font-size:12px;color:#7ee787;margin-top:8px;">Locked to MT5 Account: <strong>#{safe_mt5}</strong></div>
      </div>

      <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:14px 16px;margin-bottom:18px;">
        <div style="font-size:13px;color:#f5d57a;font-weight:700;margin-bottom:6px;">Plan: {html.escape(plan_label)}</div>
        <div style="font-size:12.5px;color:#9aa5b5;margin-bottom:6px;">Unlocked Expert Advisors:</div>
        <ul style="margin:0;padding-left:18px;font-size:13.5px;">{bots_html}</ul>
      </div>

      <div style="text-align:center;margin:22px 0;">
        <a href="{safe_dl}" style="display:inline-block;background:linear-gradient(135deg,#e5b84b,#c79c3a);color:#0b111d;text-decoration:none;font-weight:800;font-size:14px;padding:12px 24px;border-radius:8px;">
          Download Aurum Desk (Windows)
        </a>
        <div style="font-size:11.5px;color:#8b949e;margin-top:8px;word-break:break-all;">Direct link: {safe_dl}</div>
      </div>

      <div style="border-top:1px solid rgba(255,255,255,0.08);padding-top:14px;font-size:12.5px;color:#9aa5b5;line-height:1.55;">
        <strong style="color:#eef2f7;">Quick Activation Steps:</strong><br/>
        1. Extract the downloaded ZIP archive.<br/>
        2. Open MetaTrader 5 logged into account <strong>#{safe_mt5}</strong> and turn ON <strong>Algo Trading</strong>.<br/>
        3. Double-click <strong>Aurum_Desk.vbs</strong> and enter your Unique License Key.<br/><br/>
        Support: <a href="mailto:{safe_contact}" style="color:#f5d57a;">{safe_contact}</a>
      </div>
    </div>
  </div>
</body>
</html>"""

    return {"subject": subject, "text": text_body, "html": html_body}


def build_verification_code_email(
    customer_name: str,
    code: str,
    purpose: str,
    contact_email: str,
) -> Dict[str, str]:
    safe_name = html.escape(customer_name or "Trader")
    safe_code = html.escape(code)
    safe_contact = html.escape(contact_email or "eliodemaj07@gmail.com")
    is_reset = purpose == "reset"

    subject = "Aurum Desk — Password Reset Code" if is_reset else "Aurum Desk — Verify Your Email Address"
    heading = "Password Reset Request" if is_reset else "Verify Your Aurum Desk Account"
    intro = (
        "Use the 6-digit code below to reset your Aurum Desk account password. This code expires in 15 minutes."
        if is_reset
        else "Welcome to Aurum Desk! Enter the 6-digit verification code below to activate your customer account. This code expires in 15 minutes."
    )

    text_body = (
        f"Hello {customer_name or 'Trader'},\n\n"
        f"{intro}\n\n"
        f"Verification Code: {code}\n\n"
        f"If you did not request this, you can safely ignore this email.\n"
        f"Support: {contact_email}\n"
        f"Aurum Desk Team\n"
    )

    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0b111d;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#eef2f7;">
  <div style="max-width:560px;margin:24px auto;background:#111827;border:1px solid rgba(245,213,122,0.35);border-radius:14px;overflow:hidden;">
    <div style="background:linear-gradient(135deg,#141d2e,#1e1910);padding:20px 24px;border-bottom:1px solid rgba(245,213,122,0.22);">
      <div style="font-size:20px;font-weight:800;color:#f5d57a;letter-spacing:0.6px;">AURUM DESK</div>
      <div style="font-size:12px;color:#9aa5b5;margin-top:4px;">{html.escape(heading)}</div>
    </div>
    <div style="padding:24px;">
      <p style="margin:0 0 12px;font-size:15px;">Hello <strong>{safe_name}</strong>,</p>
      <p style="margin:0 0 18px;font-size:14px;line-height:1.55;color:#cfd6e0;">{html.escape(intro)}</p>
      <div style="background:#0a0f18;border:1px solid rgba(245,213,122,0.55);border-radius:10px;padding:18px;text-align:center;margin-bottom:18px;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.1px;color:#9aa5b5;margin-bottom:6px;">Your 6-Digit Code</div>
        <div style="font-family:'Courier New',monospace;font-size:30px;font-weight:800;color:#f5d57a;letter-spacing:6px;">{safe_code}</div>
      </div>
      <p style="margin:0;font-size:12px;color:#8b949e;line-height:1.5;">
        If you did not request this, you can safely ignore this message.<br/>
        Support: <a href="mailto:{safe_contact}" style="color:#f5d57a;">{safe_contact}</a>
      </p>
    </div>
  </div>
</body>
</html>"""

    return {"subject": subject, "text": text_body, "html": html_body}


def build_mt5_switch_email(
    customer_name: str,
    license_key: str,
    old_mt5: str,
    new_mt5: str,
    approved: bool,
    note: str,
    contact_email: str,
) -> Dict[str, str]:
    safe_name = html.escape(customer_name or "Trader")
    safe_key = html.escape(license_key)
    safe_old = html.escape(str(old_mt5))
    safe_new = html.escape(str(new_mt5))
    safe_note = html.escape(note or "")
    safe_contact = html.escape(contact_email or "eliodemaj07@gmail.com")

    if approved:
        subject = f"Aurum Desk — MT5 Account Switch Approved ({license_key})"
        status_title = "MT5 Account Switch Approved"
        status_color = "#7ee787"
        body_msg = (
            f"Your request to rebind Unique License Key {license_key} from MT5 Account #{old_mt5} "
            f"to MT5 Account #{new_mt5} has been approved. Please restart Aurum Desk or re-verify your key on MT5 #{new_mt5}."
        )
    else:
        subject = f"Aurum Desk — MT5 Account Switch Declined ({license_key})"
        status_title = "MT5 Account Switch Declined"
        status_color = "#ffb3ad"
        body_msg = (
            f"Your request to move Unique License Key {license_key} from MT5 Account #{old_mt5} "
            f"to #{new_mt5} could not be approved at this time. Your key remains bound to MT5 #{old_mt5}."
        )

    text_body = (
        f"Hello {customer_name or 'Trader'},\n\n"
        f"{body_msg}\n\n"
        f"Admin Note: {note or 'None'}\n\n"
        f"Need assistance? Reply to {contact_email}.\n"
        f"Aurum Desk Team\n"
    )

    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0b111d;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#eef2f7;">
  <div style="max-width:580px;margin:24px auto;background:#111827;border:1px solid rgba(245,213,122,0.35);border-radius:14px;overflow:hidden;">
    <div style="background:linear-gradient(135deg,#141d2e,#1e1910);padding:20px 24px;border-bottom:1px solid rgba(245,213,122,0.22);">
      <div style="font-size:20px;font-weight:800;color:#f5d57a;letter-spacing:0.6px;">AURUM DESK</div>
      <div style="font-size:12px;color:{status_color};font-weight:700;margin-top:4px;">{html.escape(status_title)}</div>
    </div>
    <div style="padding:24px;">
      <p style="margin:0 0 12px;font-size:15px;">Hello <strong>{safe_name}</strong>,</p>
      <p style="margin:0 0 16px;font-size:14px;line-height:1.55;color:#cfd6e0;">{html.escape(body_msg)}</p>
      <div style="background:#0a0f18;border:1px solid rgba(255,255,255,0.1);border-radius:10px;padding:14px 16px;font-size:13px;">
        <div>License Key: <strong style="color:#f5d57a;font-family:'Courier New',monospace;">{safe_key}</strong></div>
        <div style="margin-top:5px;">Previous MT5: <strong>#{safe_old}</strong> → Requested MT5: <strong>#{safe_new}</strong></div>
        {f'<div style="margin-top:5px;color:#9aa5b5;">Note: {safe_note}</div>' if safe_note else ''}
      </div>
      <p style="margin:18px 0 0;font-size:12px;color:#8b949e;">
        Support: <a href="mailto:{safe_contact}" style="color:#f5d57a;">{safe_contact}</a>
      </p>
    </div>
  </div>
</body>
</html>"""

    return {"subject": subject, "text": text_body, "html": html_body}


class Mailer:
    def __init__(self, env_cfg: Dict[str, str], db=None):
        self.env = env_cfg
        self.db = db

    def is_configured(self) -> bool:
        return bool(self.env.get("BREVO_API_KEY") or self.env.get("RESEND_API_KEY") or (self.env.get("SMTP_USER") and self.env.get("SMTP_PASS")))

    def send_email(
        self,
        to_email: str,
        subject: str,
        text_body: str,
        html_body: str,
        order_ref: str = "",
        license_key: str = "",
    ) -> Dict[str, Any]:
        recipient = (to_email or "").strip()
        if not recipient or "@" not in recipient:
            return {"ok": False, "provider": "none", "error": "Invalid recipient email"}

        brevo_key = self.env.get("BREVO_API_KEY", "").strip()
        resend_key = self.env.get("RESEND_API_KEY", "").strip()
        smtp_user = self.env.get("SMTP_USER", "").strip()
        smtp_pass = self.env.get("SMTP_PASS", "").strip()

        res: Dict[str, Any]
        if brevo_key:
            res = self._send_brevo(brevo_key, recipient, subject, text_body, html_body)
        elif resend_key:
            res = self._send_resend(resend_key, recipient, subject, text_body, html_body)
        elif smtp_user and smtp_pass:
            res = self._send_smtp(recipient, subject, text_body, html_body)
        else:
            res = {
                "ok": False,
                "provider": "unconfigured",
                "error": "Email credentials not configured yet (set SMTP_PASS or BREVO_API_KEY / RESEND_API_KEY).",
            }

        if self.db is not None:
            self.db.log_email(
                recipient=recipient,
                subject=subject,
                status="SENT" if res.get("ok") else "FAILED",
                provider=res.get("provider", ""),
                error_text=res.get("error", ""),
                order_ref=order_ref,
                license_key=license_key,
            )
        return res

    def _send_smtp(self, to_email: str, subject: str, text_body: str, html_body: str) -> Dict[str, Any]:
        host = self.env.get("SMTP_HOST", "smtp.gmail.com")
        port = int(self.env.get("SMTP_PORT", "465") or "465")
        user = self.env.get("SMTP_USER", "")
        pwd = self.env.get("SMTP_PASS", "")
        sender = self.env.get("SMTP_FROM", "") or user

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to_email
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")

        try:
            ctx = ssl.create_default_context()
            if port == 465:
                with smtplib.SMTP_SSL(host, port, context=ctx, timeout=15) as server:
                    server.login(user, pwd)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=15) as server:
                    server.ehlo()
                    server.starttls(context=ctx)
                    server.ehlo()
                    server.login(user, pwd)
                    server.send_message(msg)
            return {"ok": True, "provider": f"smtp:{host}:{port}", "error": ""}
        except Exception as exc:
            return {"ok": False, "provider": f"smtp:{host}:{port}", "error": str(exc)}

    def _send_brevo(self, api_key: str, to_email: str, subject: str, text_body: str, html_body: str) -> Dict[str, Any]:
        sender_raw = self.env.get("SMTP_FROM", "") or self.env.get("PUBLIC_CONTACT_EMAIL", "eliodemaj07@gmail.com")
        sender_name = "Aurum Desk"
        sender_email = sender_raw
        if "<" in sender_raw and ">" in sender_raw:
            sender_name = sender_raw.split("<")[0].strip().strip('"') or "Aurum Desk"
            sender_email = sender_raw.split("<")[1].split(">")[0].strip()
        payload = json.dumps({
            "sender": {"name": sender_name, "email": sender_email},
            "to": [{"email": to_email}],
            "subject": subject,
            "htmlContent": html_body,
            "textContent": text_body,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "api-key": api_key,
                "Accept": "application/json",
                "User-Agent": "AurumDeskServer/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                if 200 <= r.status < 300:
                    return {"ok": True, "provider": "brevo_api", "error": ""}
                return {"ok": False, "provider": "brevo_api", "error": f"HTTP {r.status}"}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                pass
            return {"ok": False, "provider": "brevo_api", "error": f"HTTP {exc.code}: {detail or exc.reason}"}
        except Exception as exc:
            return {"ok": False, "provider": "brevo_api", "error": str(exc)}

    def _send_resend(self, api_key: str, to_email: str, subject: str, text_body: str, html_body: str) -> Dict[str, Any]:
        sender = self.env.get("SMTP_FROM", "") or self.env.get("PUBLIC_CONTACT_EMAIL", "eliodemaj07@gmail.com")
        payload = json.dumps({
            "from": sender,
            "to": [to_email],
            "subject": subject,
            "html": html_body,
            "text": text_body,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "AurumDeskServer/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                if 200 <= r.status < 300:
                    return {"ok": True, "provider": "resend_api", "error": ""}
                return {"ok": False, "provider": "resend_api", "error": f"HTTP {r.status}"}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                pass
            return {"ok": False, "provider": "resend_api", "error": f"HTTP {exc.code}: {detail or exc.reason}"}
        except Exception as exc:
            return {"ok": False, "provider": "resend_api", "error": str(exc)}

    def send_license_delivery(
        self,
        customer_name: str,
        customer_email: str,
        license_key: str,
        mt5_login: str,
        plan_type: str,
        bot_names: List[str],
        expires_at: int,
        order_ref: str = "",
        settings: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        cfg = settings or (self.db.get_settings() if self.db else {})
        dl_url = cfg.get("download_url") or self.env.get("DEFAULT_DOWNLOAD_URL", "")
        contact = cfg.get("admin_notify_email") or self.env.get("PUBLIC_CONTACT_EMAIL", "eliodemaj07@gmail.com")
        content = build_license_email_content(
            customer_name=customer_name,
            license_key=license_key,
            mt5_login=mt5_login,
            plan_type=plan_type,
            bot_names=bot_names,
            expires_at=expires_at,
            download_url=dl_url,
            contact_email=contact,
            order_ref=order_ref,
        )
        main_res = self.send_email(
            to_email=customer_email,
            subject=content["subject"],
            text_body=content["text"],
            html_body=content["html"],
            order_ref=order_ref,
            license_key=license_key,
        )
        if main_res.get("ok") and cfg.get("send_admin_copy", "1") == "1":
            copy_addr = (cfg.get("admin_notify_email") or "").strip()
            if copy_addr and copy_addr.lower() != customer_email.strip().lower():
                self.send_email(
                    to_email=copy_addr,
                    subject=f"[ADMIN COPY] {content['subject']} — {customer_name}",
                    text_body=content["text"],
                    html_body=content["html"],
                    order_ref=order_ref,
                    license_key=license_key,
                )
        return main_res

    def send_verification_email(
        self,
        customer_name: str,
        customer_email: str,
        code: str,
        purpose: str = "register",
        settings: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        cfg = settings or (self.db.get_settings() if self.db else {})
        contact = cfg.get("admin_notify_email") or self.env.get("PUBLIC_CONTACT_EMAIL", "eliodemaj07@gmail.com")
        content = build_verification_code_email(
            customer_name=customer_name,
            code=code,
            purpose=purpose,
            contact_email=contact,
        )
        return self.send_email(
            to_email=customer_email,
            subject=content["subject"],
            text_body=content["text"],
            html_body=content["html"],
        )

    def send_mt5_switch_notification(
        self,
        customer_name: str,
        customer_email: str,
        license_key: str,
        old_mt5: str,
        new_mt5: str,
        approved: bool,
        note: str = "",
        settings: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        cfg = settings or (self.db.get_settings() if self.db else {})
        contact = cfg.get("admin_notify_email") or self.env.get("PUBLIC_CONTACT_EMAIL", "eliodemaj07@gmail.com")
        content = build_mt5_switch_email(
            customer_name=customer_name,
            license_key=license_key,
            old_mt5=old_mt5,
            new_mt5=new_mt5,
            approved=approved,
            note=note,
            contact_email=contact,
        )
        return self.send_email(
            to_email=customer_email,
            subject=content["subject"],
            text_body=content["text"],
            html_body=content["html"],
            license_key=license_key,
        )

    def send_admin_alert_email(
        self,
        subject: str,
        message: str,
        order_ref: str = "",
        license_key: str = "",
        settings: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        cfg = settings or (self.db.get_settings() if self.db else {})
        if cfg.get("send_admin_copy", "1") != "1":
            return None
        admin_addr = (cfg.get("admin_notify_email") or "").strip()
        if not admin_addr or "@" not in admin_addr or not self.is_configured():
            return None
        safe_msg = html.escape(message).replace("\n", "<br/>")
        return self.send_email(
            to_email=admin_addr,
            subject=subject,
            text_body=message,
            html_body=f"<div style='font-family:Segoe UI,Arial,sans-serif;padding:16px;background:#111827;color:#eef2f7;border-radius:10px;border:1px solid #f5d57a;'><h3 style='color:#f5d57a;margin-top:0;'>{html.escape(subject)}</h3><p style='line-height:1.5;'>{safe_msg}</p></div>",
            order_ref=order_ref,
            license_key=license_key,
        )
