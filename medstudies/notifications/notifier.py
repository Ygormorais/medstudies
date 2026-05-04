"""
Notifier — envia o plano diário por email (SMTP) e/ou Telegram.

Configuração via variáveis de ambiente:

  Email:
    MEDSTUDIES_EMAIL_FROM      — remetente (ex: seuemail@gmail.com)
    MEDSTUDIES_EMAIL_TO        — destinatário (padrão = FROM)
    MEDSTUDIES_EMAIL_PASSWORD  — senha de app Gmail ou SMTP
    MEDSTUDIES_SMTP_HOST       — padrão smtp.gmail.com
    MEDSTUDIES_SMTP_PORT       — padrão 587

  Telegram:
    MEDSTUDIES_TG_TOKEN        — token do bot (@BotFather)
    MEDSTUDIES_TG_CHAT_ID      — chat_id do usuário ou grupo

Uso:
  from medstudies.notifications.notifier import Notifier
  n = Notifier()
  n.send_daily_plan(plan_dict)
"""
from __future__ import annotations

import os
import smtplib
import logging
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

ACTION_EMOJI = {"REVIEW": "📖", "PRACTICE": "✏️", "REINFORCE": "🔁"}


@dataclass
class NotifyResult:
    email_sent: bool = False
    telegram_sent: bool = False
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []

    @property
    def ok(self) -> bool:
        return not self.errors


class Notifier:
    def __init__(
        self,
        email_from: str | None = None,
        email_to: str | None = None,
        email_password: str | None = None,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        tg_token: str | None = None,
        tg_chat_id: str | None = None,
    ):
        self.email_from     = email_from     or os.getenv("MEDSTUDIES_EMAIL_FROM", "")
        self.email_to       = email_to       or os.getenv("MEDSTUDIES_EMAIL_TO", self.email_from)
        self.email_password = email_password or os.getenv("MEDSTUDIES_EMAIL_PASSWORD", "")
        self.smtp_host      = smtp_host      or os.getenv("MEDSTUDIES_SMTP_HOST", "smtp.gmail.com")
        self.smtp_port      = smtp_port      or int(os.getenv("MEDSTUDIES_SMTP_PORT", "587"))
        self.tg_token       = tg_token       or os.getenv("MEDSTUDIES_TG_TOKEN", "")
        self.tg_chat_id     = tg_chat_id     or os.getenv("MEDSTUDIES_TG_CHAT_ID", "")

    # ── Public ─────────────────────────────────────────────────────────────────

    def send_daily_plan(self, plan: dict) -> NotifyResult:
        """
        Envia plano diário por todos os canais configurados.

        plan: dicionário retornado por /api/plan/generate ou DailyStudyPlan.to_json()
        """
        result = NotifyResult()

        if self.email_from and self.email_password:
            err = self._send_email(plan)
            if err:
                result.errors.append(f"Email: {err}")
            else:
                result.email_sent = True

        if self.tg_token and self.tg_chat_id:
            err = self._send_telegram(plan)
            if err:
                result.errors.append(f"Telegram: {err}")
            else:
                result.telegram_sent = True

        if not self.email_from and not self.tg_token:
            result.errors.append(
                "Nenhum canal configurado. Defina MEDSTUDIES_EMAIL_FROM+PASSWORD ou MEDSTUDIES_TG_TOKEN+CHAT_ID."
            )

        return result

    def send_text(self, message: str) -> NotifyResult:
        """Envia mensagem de texto livre por todos os canais configurados."""
        result = NotifyResult()

        if self.email_from and self.email_password:
            err = self._send_email_raw(
                subject="MedStudies — notificação",
                html=f"<pre>{message}</pre>",
                text=message,
            )
            if err:
                result.errors.append(f"Email: {err}")
            else:
                result.email_sent = True

        if self.tg_token and self.tg_chat_id:
            err = self._telegram_post(message)
            if err:
                result.errors.append(f"Telegram: {err}")
            else:
                result.telegram_sent = True

        return result

    # ── Email ──────────────────────────────────────────────────────────────────

    def _send_email(self, plan: dict) -> str | None:
        items = plan.get("items", [])
        plan_date = plan.get("plan_date", date.today().isoformat())
        subject = f"📚 Plano de Estudos — {plan_date}"

        html = _build_html(plan_date, items)
        text = _build_text(plan_date, items)
        return self._send_email_raw(subject, html, text)

    def _send_email_raw(self, subject: str, html: str, text: str) -> str | None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = self.email_from
        msg["To"]      = self.email_to
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html",  "utf-8"))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.login(self.email_from, self.email_password)
                server.sendmail(self.email_from, self.email_to, msg.as_bytes())
            logger.info("Email enviado para %s", self.email_to)
            return None
        except smtplib.SMTPAuthenticationError:
            return "Autenticação SMTP falhou. Use senha de app (Gmail: Segurança → Senhas de app)."
        except Exception as exc:
            return str(exc)

    # ── Telegram ───────────────────────────────────────────────────────────────

    def _send_telegram(self, plan: dict) -> str | None:
        items = plan.get("items", [])
        plan_date = plan.get("plan_date", date.today().isoformat())
        text = _build_telegram(plan_date, items)
        return self._telegram_post(text)

    def _telegram_post(self, text: str) -> str | None:
        import requests

        url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
        payload = {
            "chat_id": self.tg_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200 and resp.json().get("ok"):
                logger.info("Telegram enviado para chat_id %s", self.tg_chat_id)
                return None
            return f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:
            return str(exc)


# ── Formatters ─────────────────────────────────────────────────────────────────

def _build_text(plan_date: str, items: list[dict]) -> str:
    lines = [f"PLANO DE ESTUDOS — {plan_date}", "=" * 40]
    for item in items:
        emoji = ACTION_EMOJI.get(item.get("action", ""), "")
        err   = item.get("error_rate_pct", 0)
        lines.append(
            f"{item.get('rank', '')}. {emoji} {item.get('action')} | "
            f"{item.get('subject_name')} › {item.get('topic_name')} | "
            f"Erro: {err:.0f}%"
        )
    lines.append("")
    lines.append("Bons estudos!")
    return "\n".join(lines)


def _build_html(plan_date: str, items: list[dict]) -> str:
    ACTION_COLOR = {"REVIEW": "#0ea5e9", "PRACTICE": "#f59e0b", "REINFORCE": "#a855f7"}

    rows = ""
    for item in items:
        action = item.get("action", "")
        color  = ACTION_COLOR.get(action, "#64748b")
        emoji  = ACTION_EMOJI.get(action, "")
        err    = item.get("error_rate_pct", 0)
        rows += f"""
        <tr>
          <td style="padding:8px 12px;color:#94a3b8;font-size:13px">{item.get('rank','')}</td>
          <td style="padding:8px 12px">
            <span style="color:{color};font-weight:600">{emoji} {action}</span>
          </td>
          <td style="padding:8px 12px">
            <strong>{item.get('subject_name','')}</strong> › {item.get('topic_name','')}
          </td>
          <td style="padding:8px 12px;color:#ef4444;text-align:right">{err:.0f}%</td>
          <td style="padding:8px 12px;color:#94a3b8;font-size:12px">{item.get('reason','')}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;padding:24px">
  <h2 style="color:#38bdf8;margin-bottom:4px">📚 Plano de Estudos</h2>
  <p style="color:#94a3b8;margin-top:0">{plan_date}</p>
  <table style="border-collapse:collapse;width:100%;background:#1e293b;border-radius:8px;overflow:hidden">
    <thead>
      <tr style="background:#0f172a;color:#94a3b8;font-size:12px;text-transform:uppercase">
        <th style="padding:8px 12px;text-align:left">#</th>
        <th style="padding:8px 12px;text-align:left">Ação</th>
        <th style="padding:8px 12px;text-align:left">Tópico</th>
        <th style="padding:8px 12px;text-align:right">Erro%</th>
        <th style="padding:8px 12px;text-align:left">Por quê</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="color:#64748b;font-size:12px;margin-top:16px">
    Gerado por MedStudies · <a href="http://localhost:8000" style="color:#38bdf8">Abrir dashboard</a>
  </p>
</body>
</html>"""


def _build_telegram(plan_date: str, items: list[dict]) -> str:
    lines = [f"<b>📚 Plano de Estudos — {plan_date}</b>", ""]
    for item in items:
        emoji = ACTION_EMOJI.get(item.get("action", ""), "")
        err   = item.get("error_rate_pct", 0)
        lines.append(
            f"{item.get('rank','')}) {emoji} <b>{item.get('action')}</b> "
            f"<i>{item.get('subject_name')} › {item.get('topic_name')}</i> "
            f"({err:.0f}% erro)"
        )
    lines.append("")
    lines.append("💪 Bons estudos!")
    return "\n".join(lines)
