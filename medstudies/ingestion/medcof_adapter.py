"""
MedcofAdapter — scraper para a plataforma Medcof (medcof.com.br).

Fluxo:
  1. Login com email + senha → obtém cookie de sessão
  2. Lista simulados concluídos
  3. Para cada simulado, extrai questões (tópico, disciplina, acerto/erro)
  4. Ingere via MockExamAdapter

Configuração via variáveis de ambiente (ou kwargs):
  MEDCOF_EMAIL    — e-mail de login
  MEDCOF_PASSWORD — senha

Uso:
  from medstudies.ingestion.medcof_adapter import MedcofAdapter
  adapter = MedcofAdapter(db)
  result = adapter.ingest(email="...", password="...")

NOTA: Medcof usa SPA React. Se as páginas não carregarem com requests puro,
instale playwright (`pip install playwright && playwright install chromium`)
e defina MEDCOF_USE_PLAYWRIGHT=1.
"""
from __future__ import annotations

import os
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from medstudies.ingestion.base import BaseIngestionAdapter, IngestResult
from medstudies.ingestion.mock_exam_adapter import MockExamAdapter

logger = logging.getLogger(__name__)

# ── Medcof endpoints ──────────────────────────────────────────────────────────
BASE_URL        = "https://medcof.com.br"
LOGIN_URL       = f"{BASE_URL}/api/auth/login"
SIMULADOS_URL   = f"{BASE_URL}/api/simulados"      # lista de simulados do usuário
GABARITO_URL    = f"{BASE_URL}/api/simulados/{{id}}/gabarito"  # resultado por simulado

# Mapeamento Medcof discipline_name → Subject name interno
DISCIPLINE_MAP: dict[str, str] = {
    "Clínica Médica":               "Clínica Médica",
    "Clinica Medica":               "Clínica Médica",
    "Cirurgia":                     "Cirurgia Geral",
    "Cirurgia Geral":               "Cirurgia Geral",
    "Ginecologia e Obstetrícia":    "Ginecologia e Obstetrícia",
    "GO":                           "Ginecologia e Obstetrícia",
    "Pediatria":                    "Pediatria",
    "Medicina Preventiva":          "Medicina Preventiva e Social",
    "Preventiva":                   "Medicina Preventiva e Social",
    "Saúde Mental":                 "Saúde Mental",
    "Psiquiatria":                  "Saúde Mental",
    "Urgência":                     "Urgência e Emergência",
    "Urgência e Emergência":        "Urgência e Emergência",
}


@dataclass
class MedcofConfig:
    email: str = ""
    password: str = ""
    base_url: str = BASE_URL
    use_playwright: bool = False
    timeout: int = 30


class MedcofAdapter(BaseIngestionAdapter):
    """Scraper para a plataforma Medcof."""

    def __init__(self, session: Session, config: MedcofConfig | None = None):
        self._db = session
        self._cfg = config or MedcofConfig(
            email=os.getenv("MEDCOF_EMAIL", ""),
            password=os.getenv("MEDCOF_PASSWORD", ""),
            use_playwright=bool(os.getenv("MEDCOF_USE_PLAYWRIGHT", "")),
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def ingest(
        self,
        email: str | None = None,
        password: str | None = None,
        simulado_ids: list[int] | None = None,
        since: datetime | None = None,
    ) -> IngestResult:
        """
        Sincroniza resultados de simulados da conta Medcof.

        Args:
            email / password: sobrescreve variáveis de ambiente.
            simulado_ids: lista de IDs específicos; None = todos disponíveis.
            since: importa apenas simulados realizados depois desta data.
        """
        cfg = self._cfg
        if email:
            cfg.email = email
        if password:
            cfg.password = password

        if not cfg.email or not cfg.password:
            return IngestResult(
                ok=False,
                errors=["MEDCOF_EMAIL e MEDCOF_PASSWORD não configurados."],
            )

        try:
            if cfg.use_playwright:
                return self._ingest_playwright(cfg, simulado_ids, since)
            else:
                return self._ingest_requests(cfg, simulado_ids, since)
        except Exception as exc:
            logger.exception("Erro ao sincronizar Medcof")
            return IngestResult(ok=False, errors=[str(exc)])

    # ── requests (API direta) ──────────────────────────────────────────────────

    def _ingest_requests(
        self,
        cfg: MedcofConfig,
        simulado_ids: list[int] | None,
        since: datetime | None,
    ) -> IngestResult:
        import requests

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        # 1. Login
        login_resp = session.post(
            LOGIN_URL,
            json={"email": cfg.email, "password": cfg.password},
            timeout=cfg.timeout,
        )

        if login_resp.status_code == 404:
            # Endpoint não mapeado — tenta fallback com form POST
            login_resp = session.post(
                f"{cfg.base_url}/login",
                data={"email": cfg.email, "password": cfg.password},
                timeout=cfg.timeout,
                allow_redirects=True,
            )

        if login_resp.status_code not in (200, 201, 302):
            return IngestResult(
                ok=False,
                errors=[f"Login falhou: HTTP {login_resp.status_code}. Verifique credenciais."],
            )

        # Tenta extrair token JWT do corpo
        token = None
        try:
            body = login_resp.json()
            token = body.get("token") or body.get("access_token") or body.get("jwt")
        except Exception:
            pass

        if token:
            session.headers["Authorization"] = f"Bearer {token}"

        # 2. Lista simulados
        simulados = self._fetch_simulados(session, cfg, simulado_ids, since)
        if isinstance(simulados, IngestResult):
            return simulados

        if not simulados:
            return IngestResult(ok=True, records_created=0,
                                   metadata={"message": "Nenhum simulado encontrado."})

        # 3. Para cada simulado, baixa gabarito e ingere
        total_created = 0
        all_errors: list[str] = []

        mock_adapter = MockExamAdapter(self._db)

        for sim in simulados:
            sim_id   = sim.get("id") or sim.get("simuladoId")
            sim_name = sim.get("titulo") or sim.get("nome") or sim.get("title") or f"Medcof #{sim_id}"
            sim_date = sim.get("data") or sim.get("realizadoEm") or sim.get("createdAt")

            answered_at = None
            if sim_date:
                try:
                    answered_at = datetime.fromisoformat(sim_date[:19])
                except Exception:
                    pass

            questions_raw = self._fetch_gabarito(session, cfg, sim_id)
            if not questions_raw:
                all_errors.append(f"Sem questões para simulado '{sim_name}'.")
                continue

            questions = [self._parse_question(q) for q in questions_raw]
            questions = [q for q in questions if q]

            result = mock_adapter.ingest(
                questions=questions,
                source=sim_name,
                answered_at=answered_at,
            )
            total_created += result.records_created
            all_errors.extend(result.errors)

        return IngestResult(
            ok=len(all_errors) == 0,
            records_created=total_created,
            errors=all_errors,
            metadata={"simulados_synced": len(simulados)},
        )

    def _fetch_simulados(
        self,
        session,
        cfg: MedcofConfig,
        simulado_ids: list[int] | None,
        since: datetime | None,
    ) -> list[dict] | IngestResult:
        import requests

        try:
            resp = session.get(SIMULADOS_URL, timeout=cfg.timeout)
            if resp.status_code == 404:
                # Tenta URL alternativa
                resp = session.get(f"{cfg.base_url}/api/meus-simulados", timeout=cfg.timeout)
            if resp.status_code != 200:
                return IngestResult(
                    ok=False,
                    errors=[f"Não foi possível listar simulados: HTTP {resp.status_code}"],
                )
            data = resp.json()
        except requests.exceptions.ConnectionError:
            return IngestResult(
                ok=False,
                errors=["Não foi possível conectar ao Medcof. Verifique a conexão."],
            )
        except Exception as exc:
            return IngestResult(ok=False, errors=[f"Erro ao listar simulados: {exc}"])

        # data pode ser lista direta ou {'simulados': [...]}
        if isinstance(data, list):
            simulados = data
        elif isinstance(data, dict):
            simulados = (data.get("simulados") or data.get("data") or data.get("results") or [])
        else:
            simulados = []

        if simulado_ids:
            simulados = [s for s in simulados if s.get("id") in simulado_ids]

        if since:
            def _dt(s):
                raw = s.get("data") or s.get("realizadoEm") or s.get("createdAt") or ""
                try:
                    return datetime.fromisoformat(raw[:19])
                except Exception:
                    return datetime.min
            simulados = [s for s in simulados if _dt(s) >= since]

        return simulados

    def _fetch_gabarito(self, session, cfg: MedcofConfig, sim_id) -> list[dict]:
        url = GABARITO_URL.format(id=sim_id)
        try:
            resp = session.get(url, timeout=cfg.timeout)
            if resp.status_code == 404:
                resp = session.get(f"{cfg.base_url}/api/simulados/{sim_id}/questoes", timeout=cfg.timeout)
            if resp.status_code != 200:
                logger.warning("Gabarito %s retornou HTTP %s", sim_id, resp.status_code)
                return []
            data = resp.json()
        except Exception as exc:
            logger.warning("Erro ao buscar gabarito %s: %s", sim_id, exc)
            return []

        if isinstance(data, list):
            return data
        return data.get("questoes") or data.get("questions") or data.get("data") or []

    def _parse_question(self, raw: dict) -> dict | None:
        """Normaliza questão Medcof → formato MockExamAdapter."""
        # Medcof usa diferentes nomes de campo dependendo da versão
        topic = (
            raw.get("tema") or raw.get("topico") or raw.get("topic") or
            raw.get("assunto") or raw.get("subject") or ""
        ).strip()

        discipline = (
            raw.get("disciplina") or raw.get("area") or
            raw.get("discipline") or raw.get("especialidade") or ""
        ).strip()

        correct_raw = (
            raw.get("acertou") or raw.get("correto") or
            raw.get("correct") or raw.get("isCorrect") or
            raw.get("acerto") or False
        )

        if isinstance(correct_raw, str):
            correct = correct_raw.lower() in ("true", "1", "sim", "s", "yes")
        else:
            correct = bool(correct_raw)

        if not topic or not discipline:
            return None

        subject_name = DISCIPLINE_MAP.get(discipline, discipline)

        return {
            "topic_name": topic,
            "subject_name": subject_name,
            "correct": correct,
            "notes": raw.get("observacao") or raw.get("notes") or "",
        }

    # ── Playwright fallback (JS-rendered) ─────────────────────────────────────

    def _ingest_playwright(
        self,
        cfg: MedcofConfig,
        simulado_ids: list[int] | None,
        since: datetime | None,
    ) -> IngestResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return IngestResult(
                ok=False,
                errors=["playwright não instalado. Execute: pip install playwright && playwright install chromium"],
            )

        results: list[dict] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # Login
            page.goto(f"{cfg.base_url}/login", wait_until="networkidle")
            page.fill("input[type='email']", cfg.email)
            page.fill("input[type='password']", cfg.password)
            page.click("button[type='submit']")
            page.wait_for_url("**/dashboard**", timeout=15_000)

            # Intercepta respostas JSON durante navegação
            captured: list[dict] = []

            def _handle_response(response):
                if "simulados" in response.url and response.status == 200:
                    try:
                        captured.append({"url": response.url, "body": response.json()})
                    except Exception:
                        pass

            page.on("response", _handle_response)

            page.goto(f"{cfg.base_url}/simulados", wait_until="networkidle")
            browser.close()

        # Processa captured
        simulados_data: list[dict] = []
        for item in captured:
            body = item["body"]
            if isinstance(body, list):
                simulados_data.extend(body)
            elif isinstance(body, dict):
                simulados_data.extend(
                    body.get("simulados") or body.get("data") or []
                )

        if not simulados_data:
            return IngestResult(
                ok=False,
                errors=["Playwright: nenhum simulado capturado. Inspecione a rede do Medcof e atualize os seletores."],
            )

        mock_adapter = MockExamAdapter(self._db)
        total_created = 0
        all_errors: list[str] = []

        for sim in simulados_data:
            questions_raw = sim.get("questoes") or sim.get("questions") or []
            questions = [self._parse_question(q) for q in questions_raw]
            questions = [q for q in questions if q]
            if not questions:
                continue
            sim_name = sim.get("titulo") or sim.get("nome") or "Medcof"
            result = mock_adapter.ingest(questions=questions, source=sim_name)
            total_created += result.records_created
            all_errors.extend(result.errors)

        return IngestResult(
            ok=len(all_errors) == 0,
            records_created=total_created,
            errors=all_errors,
        )
