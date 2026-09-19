import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from shared.integrations_core.config import settings


def send_email(to_email: str, subject: str, body: str) -> bool:
    if not settings.EMAIL_ADDRESS or not settings.EMAIL_PASSWORD:
        print("Email not sent: EMAIL_ADDRESS/EMAIL_PASSWORD not configured")
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = settings.EMAIL_ADDRESS
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(settings.EMAIL_ADDRESS, settings.EMAIL_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as e:
        print(f"Email send failed: {e}")
        return False
