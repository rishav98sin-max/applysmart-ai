# agents/email_agent.py

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from dotenv import load_dotenv

load_dotenv()

EMAIL_ADDRESS      = os.getenv("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD")


def send_email(
    to_email:    str,
    subject:     str,
    body:        str,
    attachments: list = None,
) -> dict:
    print(f"   📧 Sending to {to_email}...")
    print(f"   📎 Attachments: {len(attachments) if attachments else 0}")

    msg = MIMEMultipart("alternative")
    msg["From"]     = f"ApplySmart AI <{EMAIL_ADDRESS}>"  # ✅ Named sender
    msg["To"]       = to_email
    msg["Subject"]  = subject
    msg["X-Priority"] = "3"                               # ✅ Anti-spam
    msg["X-Mailer"]   = "ApplySmart AI Job Agent"         # ✅ Anti-spam

    # Plain text fallback
    msg.attach(MIMEText(body, "plain"))

    # HTML version ✅ looks professional, avoids spam filters
    html_body = f"""
    <html>
    <body style="font-family:Arial,sans-serif;color:#1C1917;max-width:600px;margin:0 auto;padding:20px;">
        <div style="border-bottom:2px solid #0F766E;padding-bottom:10px;margin-bottom:20px;">
            <h2 style="color:#0F766E;margin:0;">ApplySmart AI</h2>
            <p style="color:#57534E;margin:4px 0 0 0;font-size:13px;">Your Job Application Report</p>
        </div>
        <div style="white-space:pre-line;font-size:14px;line-height:1.7;color:#1C1917;">
{body}
        </div>
        <div style="border-top:1px solid #E7E5E4;margin-top:30px;padding-top:15px;
                    font-size:12px;color:#A8A29E;text-align:center;">
            Sent by ApplySmart AI &middot; Powered by Groq
        </div>
    </body>
    </html>
    """
    msg.attach(MIMEText(html_body, "html"))

    # Switch to mixed for attachments if needed
    if attachments:
        msg = MIMEMultipart("mixed")
        msg["From"]       = f"ApplySmart AI <{EMAIL_ADDRESS}>"
        msg["To"]         = to_email
        msg["Subject"]    = subject
        msg["X-Priority"] = "3"
        msg["X-Mailer"]   = "ApplySmart AI Job Agent"
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        for filepath in attachments:
            if os.path.exists(filepath):
                try:
                    with open(filepath, "rb") as f:
                        part = MIMEBase("application", "octet-stream")
                        part.set_payload(f.read())
                        encoders.encode_base64(part)
                        part.add_header(
                            "Content-Disposition",
                            f"attachment; filename={os.path.basename(filepath)}"
                        )
                        msg.attach(part)
                    print(f"   ✅ Attached: {os.path.basename(filepath)}")
                except Exception as e:
                    print(f"   ⚠️  Could not attach {filepath}: {e}")
            else:
                print(f"   ⚠️  File not found: {filepath}")

    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        raise RuntimeError("EMAIL_ADDRESS or EMAIL_APP_PASSWORD not set in .env")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        server.sendmail(EMAIL_ADDRESS, to_email, msg.as_string())

    print(f"   ✅ Email sent to {to_email}")
    return {"status": "sent", "to": to_email}


def send_test_email(to_email: str) -> None:
    send_email(
        to_email = to_email,
        subject  = "✅ ApplySmart AI — Email Test",
        body     = (
            "This is a test email from your Job Application Agent.\n\n"
            "If you received this, your email setup is working correctly!\n\n"
            "— ApplySmart AI"
        ),
    )


if __name__ == "__main__":
    send_test_email(os.getenv("TEST_EMAIL", "rishav98sin@gmail.com"))