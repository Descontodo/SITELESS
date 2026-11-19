"""Aplicação local/SaaS de prospecção com OpenStreetMap e contas isoladas."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from auth import get_session_user, hash_password, monthly_window, new_session, utc_now, verify_password
from billing import (
    BillingUnavailable,
    InvalidStripeWebhook,
    construct_webhook_event,
    create_checkout_session,
    create_portal_session,
    invoice_subscription_id,
    plan_for_subscription,
    retrieve_subscription,
    stripe_id,
    stripe_value,
    subscription_period_end,
)
from classifier import (
    classify_website,
    contacts_from_url,
    extract_public_contacts,
    fetch_public_contacts,
    fetch_public_contacts_browser,
    make_whatsapp_link,
    merge_contacts,
    normalize_url,
    phone_to_whatsapp_digits,
)
from scraper import enrich_from_web_search, iter_openstreetmap


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("LEADS_DB_PATH", str(BASE_DIR / "leads_sem_site.db")))
SESSION_COOKIE = "prospector_session"
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@sistema.com").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Admin@1234")
ADMIN_EMAIL_CONFIGURED = "ADMIN_EMAIL" in os.environ
ADMIN_PASSWORD_CONFIGURED = "ADMIN_PASSWORD" in os.environ
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").casefold() in {"1", "true", "yes"}
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_IDS = {
    "starter": os.getenv("STRIPE_PRICE_STARTER_ID", "").strip(),
    "agency": os.getenv("STRIPE_PRICE_AGENCY_ID", "").strip(),
}
STRIPE_PRICE_EXPECTATIONS = {
    "starter": {"currency": "usd", "unit_amount": 1900},
    "agency": {"currency": "usd", "unit_amount": 4900},
}
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
DESKTOP_MODE = os.getenv("SITLESS_DESKTOP", "false").casefold() in {"1", "true", "yes"}

LEAD_COLUMNS = (
    "nome", "nicho", "endereco", "cidade", "pais", "telefone", "whatsapp",
    "link_whatsapp_direto", "email", "instagram", "facebook", "tiktok", "linkedin",
    "youtube", "link_agendamento", "nota", "avaliacoes", "link_maps", "motivo_flag", "criado_em",
)
CONTACT_COLUMNS = (
    "telefone", "whatsapp", "link_whatsapp_direto", "email", "instagram", "facebook",
    "tiktok", "linkedin", "youtube", "link_agendamento",
)

PLANS: dict[str, dict[str, Any]] = {
    "free": {"name": "Grátis", "monthly": 15, "per_search": 10, "xlsx": False, "deep": False, "http_check": False, "pitch": False, "whatsapp_direct": False},
    "starter": {"name": "Starter", "monthly": 600, "per_search": 600, "xlsx": True, "deep": False, "http_check": False, "pitch": True, "whatsapp_direct": True},
    "agency": {"name": "Agency", "monthly": 3000, "per_search": 3000, "xlsx": True, "deep": True, "http_check": True, "pitch": True, "whatsapp_direct": True},
    "admin": {"name": "Admin ilimitado", "monthly": None, "per_search": 5000, "xlsx": True, "deep": True, "http_check": True, "pitch": True, "whatsapp_direct": True},
}


class SearchRequest(BaseModel):
    nicho: str = Field(min_length=2, max_length=120)
    cidade: str = Field(min_length=2, max_length=120)
    pais: str = Field(min_length=2, max_length=120)
    nota_minima: float = Field(default=0, ge=0, le=5)
    avaliacoes_minimas: int = Field(default=0, ge=0, le=1_000_000)
    raio_km: float = Field(default=15, ge=0, le=500)
    max_resultados: int = Field(default=100, ge=1, le=5000)
    so_abertas: bool = False
    testar_url_ativa: bool = False
    varredura_profunda: bool = False


class Credentials(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=10, max_length=256)


class PlanUpdate(BaseModel):
    plan: str


class CheckoutRequest(BaseModel):
    plan: str


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _normalize_key_piece(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (value or "").strip().casefold())
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", ascii_text)).strip()


def _dedupe_key(lead: dict[str, Any]) -> str:
    name = _normalize_key_piece(str(lead.get("nome") or ""))
    address = _normalize_key_piece(str(lead.get("endereco") or ""))
    if not address:
        address = _normalize_key_piece(" ".join((str(lead.get("cidade") or ""), str(lead.get("pais") or ""))))
    return hashlib.sha256(f"{name}|{address}".encode("utf-8")).hexdigest()


def _merge_text_values(*values: Any) -> str:
    merged: list[str] = []
    for value in values:
        for item in str(value or "").split(";"):
            item = item.strip()
            if item and all(item.casefold() != current.casefold() for current in merged):
                merged.append(item)
    return "; ".join(merged)


def _create_leads_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            nome TEXT NOT NULL, nicho TEXT NOT NULL DEFAULT '', endereco TEXT NOT NULL DEFAULT '',
            cidade TEXT NOT NULL DEFAULT '', pais TEXT NOT NULL DEFAULT '', telefone TEXT NOT NULL DEFAULT '',
            whatsapp TEXT NOT NULL DEFAULT '', link_whatsapp_direto TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '', instagram TEXT NOT NULL DEFAULT '', facebook TEXT NOT NULL DEFAULT '',
            tiktok TEXT NOT NULL DEFAULT '', linkedin TEXT NOT NULL DEFAULT '', youtube TEXT NOT NULL DEFAULT '',
            link_agendamento TEXT NOT NULL DEFAULT '', nota REAL, avaliacoes INTEGER NOT NULL DEFAULT 0,
            link_maps TEXT NOT NULL DEFAULT '', motivo_flag TEXT NOT NULL, criado_em TEXT NOT NULL,
            website_original TEXT NOT NULL DEFAULT '', dedupe_key TEXT NOT NULL, place_id TEXT NOT NULL DEFAULT '',
            UNIQUE(user_id, dedupe_key)
        )"""
    )


def _init_auth_tables(connection: sqlite3.Connection) -> int:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', plan TEXT NOT NULL DEFAULT 'free',
            blocked INTEGER NOT NULL DEFAULT 0, usage_count INTEGER NOT NULL DEFAULT 0,
            period_started TEXT NOT NULL, created_at TEXT NOT NULL,
            stripe_customer_id TEXT, stripe_subscription_id TEXT,
            subscription_status TEXT NOT NULL DEFAULT '', subscription_period_end TEXT NOT NULL DEFAULT ''
        )"""
    )
    user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
    for column, definition in (
        ("stripe_customer_id", "TEXT"),
        ("stripe_subscription_id", "TEXT"),
        ("subscription_status", "TEXT NOT NULL DEFAULT ''"),
        ("subscription_period_end", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in user_columns:
            connection.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS payment_events (
            event_id TEXT PRIMARY KEY, email TEXT NOT NULL, plan TEXT NOT NULL, created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS stripe_events (
            event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_stripe_customer ON users(stripe_customer_id) "
        "WHERE stripe_customer_id IS NOT NULL AND stripe_customer_id <> ''"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_stripe_subscription ON users(stripe_subscription_id)"
    )
    now = utc_now().isoformat()
    row = connection.execute("SELECT id,role FROM users WHERE email=? COLLATE NOCASE", (ADMIN_EMAIL,)).fetchone()
    if row and row["role"] == "admin":
        admin_id = int(row["id"])
        connection.execute("UPDATE users SET role='admin',plan='admin',blocked=0 WHERE id=?", (admin_id,))
    elif row:
        previous_admin = connection.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        if previous_admin:
            raise RuntimeError("ADMIN_EMAIL já pertence a outra conta; escolha um e-mail administrativo disponível.")
        admin_id = int(row["id"])
        connection.execute("UPDATE users SET role='admin',plan='admin',blocked=0 WHERE id=?", (admin_id,))
    else:
        previous_admin = connection.execute("SELECT id,email FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        if previous_admin:
            admin_id = int(previous_admin["id"])
            if ADMIN_EMAIL_CONFIGURED:
                connection.execute("UPDATE users SET email=? WHERE id=?", (ADMIN_EMAIL, admin_id))
            connection.execute("UPDATE users SET role='admin',plan='admin',blocked=0 WHERE id=?", (admin_id,))
        else:
            connection.execute(
                "INSERT INTO users(email,password_hash,role,plan,period_started,created_at) VALUES(?,?,'admin','admin',?,?)",
                (ADMIN_EMAIL, hash_password(ADMIN_PASSWORD), now, now),
            )
            admin_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    if ADMIN_PASSWORD_CONFIGURED:
        connection.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(ADMIN_PASSWORD), admin_id))
        connection.execute("DELETE FROM sessions WHERE user_id=?", (admin_id,))
    if ADMIN_PASSWORD == "Admin@1234" and not ADMIN_PASSWORD_CONFIGURED:
        logging.getLogger("prospector").warning(
            "Admin padrão ativo. Defina ADMIN_EMAIL e ADMIN_PASSWORD antes de expor o serviço."
        )
    return admin_id


def init_db() -> None:
    with closing(_connect()) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        admin_id = _init_auth_tables(connection)
        table = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='leads'").fetchone()
        if not table:
            _create_leads_table(connection)
        else:
            old_sql = str(table["sql"] or "").casefold()
            old_columns = {row["name"] for row in connection.execute("PRAGMA table_info(leads)")}
            needs_rebuild = (
                "user_id" not in old_columns
                or set(LEAD_COLUMNS).difference(old_columns)
                or "dedupe_key text not null unique" in old_sql
            )
            if needs_rebuild:
                legacy = [dict(row) for row in connection.execute("SELECT * FROM leads").fetchall()]
                connection.execute("DROP TABLE leads")
                _create_leads_table(connection)
                for old in legacy:
                    row = {column: old.get(column, "") for column in LEAD_COLUMNS}
                    row["nome"] = str(row.get("nome") or "Empresa sem nome")
                    row["motivo_flag"] = str(row.get("motivo_flag") or "sem website")
                    row["criado_em"] = str(row.get("criado_em") or utc_now().isoformat())
                    row["user_id"] = int(old.get("user_id") or admin_id)
                    row["website_original"] = old.get("website_original") or ""
                    row["dedupe_key"] = old.get("dedupe_key") or _dedupe_key(row)
                    row["place_id"] = old.get("place_id") or ""
                    cols = list(LEAD_COLUMNS) + ["website_original", "dedupe_key", "place_id", "user_id"]
                    values = [row.get(column, "") for column in cols]
                    if old.get("id") is not None:
                        cols.insert(0, "id")
                        values.insert(0, old["id"])
                    placeholders = ",".join("?" for _ in cols)
                    try:
                        connection.execute(
                            f"INSERT INTO leads({','.join(cols)}) VALUES({placeholders}) ON CONFLICT(user_id,dedupe_key) DO NOTHING",
                            values,
                        )
                    except sqlite3.IntegrityError:
                        continue
        connection.execute("CREATE INDEX IF NOT EXISTS idx_leads_user_created ON leads(user_id, criado_em DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_now().isoformat(),))
        connection.commit()


def _reset_period(connection: sqlite3.Connection, user: dict[str, Any]) -> dict[str, Any]:
    next_start, periods = monthly_window(str(user.get("period_started") or utc_now().isoformat()))
    if periods:
        connection.execute(
            "UPDATE users SET period_started=?, usage_count=0 WHERE id=?",
            (next_start, user["id"]),
        )
        connection.commit()
        user["period_started"] = next_start
        user["usage_count"] = 0
    return user


def _quota(user: dict[str, Any]) -> dict[str, Any]:
    effective_plan = "admin" if user.get("role") == "admin" else str(user.get("plan") or "free")
    status = str(user.get("subscription_status") or "").casefold()
    if effective_plan in {"starter", "agency"}:
        if status in {"canceled", "unpaid", "incomplete", "incomplete_expired", "paused"}:
            effective_plan = "free"
        elif status == "past_due":
            try:
                period_end = datetime.fromisoformat(str(user.get("subscription_period_end") or ""))
                if period_end.tzinfo is None:
                    period_end = period_end.replace(tzinfo=timezone.utc)
                if period_end <= utc_now():
                    effective_plan = "free"
            except ValueError:
                effective_plan = "free"
    plan = PLANS.get(effective_plan, PLANS["free"])
    limit = plan["monthly"]
    used = int(user.get("usage_count") or 0)
    return {
        "plan": effective_plan,
        "plan_name": plan["name"],
        "monthly_limit": limit,
        "used": used,
        "remaining": None if limit is None else max(0, limit - used),
        "per_search_limit": plan["per_search"],
        "features": {key: plan[key] for key in ("xlsx", "deep", "http_check", "pitch", "whatsapp_direct")},
    }


def save_lead(lead: dict[str, Any], user_id: int) -> tuple[bool, bool]:
    """Insere/enriquece lead dentro da conta; retorna (novo, quota_esgotada)."""
    if not str(lead.get("nome") or "").strip():
        return False, False
    row = {column: lead.get(column) for column in LEAD_COLUMNS}
    for column in LEAD_COLUMNS:
        if row[column] is None and column not in {"nota", "criado_em"}:
            row[column] = 0 if column == "avaliacoes" else ""
    row["nome"] = str(row["nome"]).strip()
    row["criado_em"] = row.get("criado_em") or utc_now().astimezone().isoformat(timespec="seconds")
    row["nota"] = float(row["nota"]) if row.get("nota") not in (None, "") else None
    row["avaliacoes"] = int(row.get("avaliacoes") or 0)
    row["dedupe_key"] = _dedupe_key(row)
    row["place_id"] = str(lead.get("place_id") or "")
    row["website_original"] = str(lead.get("website_original") or "")
    row["user_id"] = user_id
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT id, " + ",".join(CONTACT_COLUMNS) + " FROM leads WHERE user_id=? AND dedupe_key=?",
            (user_id, row["dedupe_key"]),
        ).fetchone()
        if existing:
            for column in CONTACT_COLUMNS:
                if column != "telefone":
                    row[column] = _merge_text_values(existing[column], row[column])
            updates = list(CONTACT_COLUMNS) + ["nota", "avaliacoes", "link_maps", "motivo_flag", "website_original", "place_id"]
            assignments = ",".join(f"{column}=COALESCE(NULLIF(?,''),{column})" for column in updates)
            connection.execute(
                f"UPDATE leads SET {assignments} WHERE id=?",
                (*[row.get(column) for column in updates], existing["id"]),
            )
            connection.commit()
            return False, False
        user_row = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user_row:
            connection.rollback()
            return False, False
        current_user = dict(user_row)
        next_period_start, elapsed_periods = monthly_window(str(current_user.get("period_started") or utc_now().isoformat()))
        if elapsed_periods:
            connection.execute("UPDATE users SET usage_count=0,period_started=? WHERE id=?", (next_period_start, user_id))
            current_user["usage_count"] = 0
            current_user["period_started"] = next_period_start
        effective_plan = "admin" if current_user.get("role") == "admin" else current_user["plan"]
        limit = PLANS.get(effective_plan, PLANS["free"])["monthly"]
        if limit is not None and int(current_user.get("usage_count") or 0) >= limit:
            connection.rollback()
            return False, True
        columns = list(LEAD_COLUMNS) + ["website_original", "dedupe_key", "place_id", "user_id"]
        connection.execute(
            f"INSERT INTO leads({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            [row.get(column, "") for column in columns],
        )
        if limit is not None:
            connection.execute("UPDATE users SET usage_count=usage_count+1 WHERE id=?", (user_id,))
        connection.commit()
        return True, False


def list_leads(user_id: int, *, query: str = "", limit: int | None = 1000, offset: int = 0) -> tuple[list[dict], int]:
    columns = ",".join(("id",) + LEAD_COLUMNS + ("website_original",))
    with closing(_connect()) as connection:
        if query.strip():
            pattern = "%" + query.strip().replace("%", r"\%").replace("_", r"\_") + "%"
            fields = ("nome", "nicho", "endereco", "cidade", "telefone", "whatsapp", "email")
            where = "user_id=? AND (" + " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in fields) + ")"
            params: tuple[Any, ...] = (user_id, *([pattern] * len(fields)))
        else:
            where = "user_id=?"
            params = (user_id,)
        total = int(connection.execute(f"SELECT COUNT(*) FROM leads WHERE {where}", params).fetchone()[0])
        suffix = " ORDER BY criado_em DESC"
        if limit is not None:
            suffix += " LIMIT ? OFFSET ?"
            params = (*params, limit, offset)
        rows = connection.execute(f"SELECT {columns} FROM leads WHERE {where}{suffix}", params).fetchall()
    return [dict(row) for row in rows], total


def current_user(request: Request) -> dict[str, Any]:
    token = request.cookies.get(SESSION_COOKIE)
    with closing(_connect()) as connection:
        user = get_session_user(connection, token)
        if not user:
            raise HTTPException(status_code=401, detail="Entre na sua conta para continuar.")
        user = _reset_period(connection, user)
    if user.get("blocked"):
        raise HTTPException(status_code=403, detail="Esta conta está bloqueada. Fale com o administrador.")
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito à administração.")
    return user


init_db()
app = FastAPI(
    title="Siteless",
    description="Prospecção de empresas sem site próprio com cobrança de assinaturas pelo Stripe.",
    version="2.0.0",
)
_states: dict[int, dict[str, Any]] = {}
_stop_events: dict[int, asyncio.Event] = {}
_search_tasks: dict[int, asyncio.Task] = {}


def _user_state(user_id: int) -> dict[str, Any]:
    return _states.setdefault(user_id, {
        "running": False, "done": True, "stopped": False, "analyzed": 0, "saved": 0,
        "message": "Pronto para iniciar.", "error": None, "source": None, "started_at": None,
    })


def _safe_user(user: dict[str, Any]) -> dict[str, Any]:
    quota = _quota(user)
    return {
        "id": int(user["id"]), "email": user["email"], "role": user["role"],
        "blocked": bool(user.get("blocked")), "quota": quota, "plan": quota["plan"],
        "subscription_status": str(user.get("subscription_status") or ""),
        "can_manage_billing": bool(user.get("stripe_customer_id")),
    }


def _public_leads(user: dict, records: list[dict]) -> list[dict]:
    result = []
    free_user = _quota(user)["plan"] == "free"
    unlocked_ids: set[int] = set()
    if free_user:
        with closing(_connect()) as connection:
            unlocked_ids = {int(row[0]) for row in connection.execute(
                "SELECT id FROM leads WHERE user_id=? ORDER BY criado_em DESC,id DESC LIMIT 5",
                (int(user["id"]),),
            ).fetchall()}
    for item in records:
        row = dict(item)
        locked = free_user and int(row.get("id") or 0) not in unlocked_ids
        row["contacts_locked"] = locked
        if locked:
            for column in CONTACT_COLUMNS:
                row[column] = ""
            row["website_original"] = ""
        if not PLANS.get(_quota(user)["plan"], PLANS["free"])["whatsapp_direct"]:
            row["link_whatsapp_direto"] = ""
        row.pop("id", None)
        result.append(row)
    return result


def _lead_for_db(place: dict, reason: str, contacts: dict[str, Any]) -> dict[str, Any]:
    phone = str(place.get("telefone") or contacts.get("telefone") or "")
    country = str(place.get("pais") or "")
    whatsapp = contacts.get("whatsapp") or ""
    direct_whatsapp = contacts.get("link_whatsapp_direto") or ""
    if not direct_whatsapp:
        digits = phone_to_whatsapp_digits(phone, country)
        if digits:
            whatsapp = _merge_text_values(whatsapp, digits)
            direct_whatsapp = make_whatsapp_link(phone, country) or ""
    return {
        "nome": place.get("nome") or "", "nicho": place.get("nicho") or "",
        "endereco": place.get("endereco") or "", "cidade": place.get("cidade") or "",
        "pais": place.get("pais") or "", "telefone": phone, "whatsapp": whatsapp,
        "link_whatsapp_direto": direct_whatsapp, "email": contacts.get("email") or "",
        "instagram": contacts.get("instagram") or "", "facebook": contacts.get("facebook") or "",
        "tiktok": contacts.get("tiktok") or "", "linkedin": contacts.get("linkedin") or "",
        "youtube": contacts.get("youtube") or "", "link_agendamento": contacts.get("link_agendamento") or "",
        "nota": place.get("nota"), "avaliacoes": place.get("avaliacoes") or 0,
        "link_maps": place.get("link_maps") or "", "website_original": place.get("website_original") or "",
        "motivo_flag": reason, "place_id": place.get("place_id") or "",
    }


def _osm_contacts(place: dict) -> dict[str, Any]:
    raw = place.get("osm_contacts") or {}
    contacts: dict[str, Any] = {
        "whatsapp": None, "link_whatsapp_direto": None, "instagram": None,
        "facebook": None, "tiktok": None, "linkedin": None, "youtube": None,
        "email": None, "link_agendamento": None,
    }
    for field in ("instagram", "facebook", "tiktok", "linkedin", "youtube"):
        if raw.get(field):
            raw_value = str(raw[field]).strip()
            if not re.match(r"(?i)^https?://", raw_value) and (raw_value.startswith("@") or "." not in raw_value):
                host = {"instagram": "instagram.com", "facebook": "facebook.com", "tiktok": "tiktok.com",
                        "linkedin": "linkedin.com", "youtube": "youtube.com"}[field]
                raw_value = "https://" + host + "/" + raw_value.lstrip("@/")
            normalized = normalize_url(raw_value)
            contacts = merge_contacts(contacts, contacts_from_url(normalized))
            if not contacts.get(field):
                contacts[field] = normalized or raw_value
    if raw.get("email"):
        contacts["email"] = str(raw["email"])
    if raw.get("phone"):
        digits = phone_to_whatsapp_digits(str(raw["phone"]), str(place.get("pais") or ""))
        if digits:
            contacts["whatsapp"] = digits
            contacts["link_whatsapp_direto"] = f"https://wa.me/{digits}"
    if raw.get("whatsapp"):
        whatsapp_value = str(raw["whatsapp"]).strip()
        parsed = contacts_from_url(whatsapp_value)
        if parsed.get("whatsapp"):
            contacts = merge_contacts(contacts, parsed)
        else:
            digits = phone_to_whatsapp_digits(whatsapp_value, str(place.get("pais") or ""))
            if digits:
                contacts["whatsapp"] = _merge_text_values(contacts.get("whatsapp"), digits)
                contacts["link_whatsapp_direto"] = _merge_text_values(
                    contacts.get("link_whatsapp_direto"), f"https://wa.me/{digits}"
                )
    tags = place.get("osm_tags") or {}
    booking_url = tags.get("contact:booking") or tags.get("booking") or tags.get("reservation")
    if booking_url:
        contacts["link_agendamento"] = str(booking_url)
    return contacts


async def _run_search(config: SearchRequest, user: dict, state: dict, stop_event: asyncio.Event, target: int) -> None:
    user_id = int(user["id"])
    state["source"] = "OpenStreetMap (Overpass) + Nominatim + busca pública de contatos"
    try:
        state["message"] = "Consultando dados abertos do OpenStreetMap."
        if config.nota_minima or config.avaliacoes_minimas:
            state["message"] = "OpenStreetMap não fornece avaliações; filtros de nota/avaliações são incompatíveis."
        scan_cap = min(max(target * 15, 150), 20_000)
        iterator = iter_openstreetmap(
            niche=config.nicho, city=config.cidade, country=config.pais,
            max_results=scan_cap, min_rating=config.nota_minima,
            min_reviews=config.avaliacoes_minimas, only_open=config.so_abertas,
            radius_km=config.raio_km, deep_scan=config.varredura_profunda,
            stop_event=stop_event,
        )
        async for place in iterator:
            if stop_event.is_set() or state["saved"] >= target:
                break
            state["analyzed"] += 1
            is_lead, reason = await asyncio.to_thread(
                classify_website, place.get("website_original"),
                test_active=config.testar_url_ativa,
            )
            if not is_lead or not reason:
                continue
            contacts = merge_contacts(_osm_contacts(place), contacts_from_url(place.get("website_original")))
            website = place.get("website_original")
            if website:
                contacts = merge_contacts(contacts, await fetch_public_contacts(website))
                if any(not contacts.get(field) for field in ("email", "instagram", "facebook", "tiktok", "linkedin", "youtube", "whatsapp")):
                    contacts = merge_contacts(contacts, await fetch_public_contacts_browser(website))
            if any(not contacts.get(field) for field in ("email", "instagram", "facebook", "tiktok", "linkedin", "youtube", "whatsapp")):
                contacts = merge_contacts(contacts, await enrich_from_web_search(
                    {**place, **_lead_for_db(place, reason, contacts)}, stop_event=stop_event,
                ))
            if stop_event.is_set():
                break
            saved, quota_exhausted = await asyncio.to_thread(save_lead, _lead_for_db(place, reason, contacts), user_id)
            if quota_exhausted:
                state["message"] = "Limite mensal do plano atingido. Faça upgrade para continuar."
                break
            if saved:
                state["saved"] += 1
            state["message"] = f"Analisados: {state['analyzed']} · novos leads salvos: {state['saved']}."
        if stop_event.is_set():
            state["stopped"] = True
            state["message"] = "Busca interrompida. Os dados já salvos foram mantidos."
        elif state["saved"] >= target:
            state["message"] = f"Meta de {target} novos leads alcançada."
        elif not state["error"] and "Limite mensal" not in state["message"]:
            state["message"] = "Varredura concluída. Consulte o resultado; o OpenStreetMap pode não conter todos os estabelecimentos."
    except asyncio.CancelledError:
        state["stopped"] = True
        state["message"] = "Busca interrompida. Os dados já salvos foram mantidos."
    except Exception as exc:
        state["error"] = str(exc)[:500]
        state["message"] = "A busca terminou com erro. Os leads gravados continuam disponíveis."
    finally:
        state["running"] = False
        state["done"] = True


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return HTML_PAGE


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(user: dict = Depends(admin_user)) -> str:
    del user
    return ADMIN_PAGE


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": "siteless", "source": "OpenStreetMap"}


def _email_valid(email: str) -> bool:
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email.strip()))


def _set_session_cookie(response: Response, connection: sqlite3.Connection, user_id: int) -> None:
    token, _expires = new_session(connection, user_id)
    connection.commit()
    response.set_cookie(
        SESSION_COOKIE, token, max_age=30 * 24 * 60 * 60, httponly=True,
        secure=COOKIE_SECURE, samesite="lax", path="/",
    )


@app.post("/api/auth/register")
async def register(credentials: Credentials, response: Response) -> dict[str, Any]:
    email = credentials.email.strip().lower()
    if not _email_valid(email):
        raise HTTPException(status_code=422, detail="Informe um e-mail válido.")
    try:
        password_hash = hash_password(credentials.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    now = utc_now().isoformat()
    with closing(_connect()) as connection:
        try:
            cursor = connection.execute(
                "INSERT INTO users(email,password_hash,role,plan,period_started,created_at) VALUES(?,?,'user','free',?,?)",
                (email, password_hash, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Este e-mail já está cadastrado.") from exc
        user_id = int(cursor.lastrowid)
        _set_session_cookie(response, connection, user_id)
    with closing(_connect()) as connection:
        user = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _safe_user(dict(user))


@app.post("/api/auth/login")
async def login(credentials: Credentials, request: Request, response: Response) -> dict[str, Any]:
    email = credentials.email.strip().lower()
    with closing(_connect()) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS auth_attempts(email TEXT NOT NULL, ip TEXT NOT NULL, at TEXT NOT NULL)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_auth_attempts_email ON auth_attempts(email,at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_auth_attempts_ip ON auth_attempts(ip,at)")
        connection.execute("DELETE FROM auth_attempts WHERE at<?", ((utc_now() - timedelta(days=1)).isoformat(),))
        cutoff = utc_now().timestamp() - 900
        recent = connection.execute("SELECT at FROM auth_attempts WHERE email=? OR ip=?", (email, request.client.host if request.client else "" )).fetchall()
        attempts = sum(1 for row in recent if datetime.fromisoformat(row["at"]).timestamp() > cutoff)
        if attempts >= 10:
            raise HTTPException(status_code=429, detail="Muitas tentativas. Aguarde 15 minutos.")
        row = connection.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
        if not row or not verify_password(credentials.password, row["password_hash"]):
            connection.execute("INSERT INTO auth_attempts(email,ip,at) VALUES(?,?,?)", (email, request.client.host if request.client else "", utc_now().isoformat()))
            connection.commit()
            raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")
        if row["blocked"]:
            raise HTTPException(status_code=403, detail="Esta conta está bloqueada.")
        connection.execute("DELETE FROM auth_attempts WHERE email=? OR ip=?", (email, request.client.host if request.client else ""))
        _set_session_cookie(response, connection, int(row["id"]))
    with closing(_connect()) as connection:
        user = connection.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
        clean = _reset_period(connection, dict(user))
    return _safe_user(clean)


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response, user: dict = Depends(current_user)) -> dict[str, bool]:
    del user
    token = request.cookies.get(SESSION_COOKIE, "")
    token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    with closing(_connect()) as connection:
        connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
        connection.commit()
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return {"ok": True}


@app.get("/api/auth/me")
async def me(user: dict = Depends(current_user)) -> dict[str, Any]:
    return _safe_user(user)


@app.post("/api/billing/checkout")
async def billing_checkout(request: CheckoutRequest, user: dict = Depends(current_user)) -> dict[str, str]:
    if DESKTOP_MODE:
        raise HTTPException(status_code=503, detail="Assinaturas ficam disponíveis na versão hospedada do Siteless.")
    plan = request.plan.strip().casefold()
    if plan not in {"starter", "agency"}:
        raise HTTPException(status_code=422, detail="Escolha o plano Starter ou Agency.")
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Configure STRIPE_SECRET_KEY no arquivo .env antes de receber pagamentos.")
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Configure o webhook Stripe antes de habilitar o Checkout.")
    if user.get("role") == "admin":
        raise HTTPException(status_code=409, detail="A conta administradora já tem acesso ilimitado.")
    current_plan = _quota(user)["plan"]
    status = str(user.get("subscription_status") or "").casefold()
    if current_plan in {"starter", "agency"} and status in {"active", "trialing", "past_due"}:
        raise HTTPException(status_code=409, detail="Você já tem uma assinatura. Use Gerenciar assinatura para alterá-la.")
    try:
        session = await asyncio.to_thread(
            create_checkout_session,
            secret_key=STRIPE_SECRET_KEY,
            price_id=STRIPE_PRICE_IDS.get(plan, ""),
            plan=plan,
            **STRIPE_PRICE_EXPECTATIONS[plan],
            user_id=int(user["id"]),
            email=str(user["email"]),
            customer_id=str(user.get("stripe_customer_id") or ""),
            base_url=APP_BASE_URL,
        )
    except BillingUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logging.getLogger("prospector").exception("Falha ao criar sessão de Checkout Stripe.")
        raise HTTPException(status_code=502, detail="O Stripe não conseguiu iniciar o checkout. Tente novamente.") from exc
    checkout_url = str(stripe_value(session, "url", "") or "")
    if not checkout_url:
        raise HTTPException(status_code=502, detail="O Stripe não retornou a página de pagamento.")
    return {"url": checkout_url}


@app.post("/api/billing/portal")
async def billing_portal(user: dict = Depends(current_user)) -> dict[str, str]:
    if DESKTOP_MODE:
        raise HTTPException(status_code=503, detail="O gerenciamento de assinatura fica disponível na versão hospedada do Siteless.")
    customer_id = str(user.get("stripe_customer_id") or "")
    if not customer_id:
        raise HTTPException(status_code=409, detail="Sua conta ainda não tem uma assinatura Stripe.")
    try:
        session = await asyncio.to_thread(
            create_portal_session,
            secret_key=STRIPE_SECRET_KEY,
            customer_id=customer_id,
            return_url=f"{APP_BASE_URL}/",
        )
    except BillingUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logging.getLogger("prospector").exception("Falha ao criar sessão do portal Stripe.")
        raise HTTPException(status_code=502, detail="Não foi possível abrir o portal de cobrança.") from exc
    portal_url = str(stripe_value(session, "url", "") or "")
    if not portal_url:
        raise HTTPException(status_code=502, detail="O Stripe não retornou o portal de cobrança.")
    return {"url": portal_url}


@app.get("/api/status")
async def status(user: dict = Depends(current_user)) -> dict[str, Any]:
    state = _user_state(int(user["id"]))
    _, total = list_leads(int(user["id"]), limit=0)
    return {**{key: state[key] for key in ("running", "done", "stopped", "analyzed", "saved", "message", "error", "source", "started_at")},
            "database_total": total, "quota": _quota(user)}


@app.post("/api/search/start")
async def start_search(request: SearchRequest, user: dict = Depends(current_user)) -> dict[str, Any]:
    user_id = int(user["id"])
    state = _user_state(user_id)
    if state["running"]:
        raise HTTPException(status_code=409, detail="Já existe uma busca em andamento na sua conta.")
    plan = PLANS.get(_quota(user)["plan"], PLANS["free"])
    if request.max_resultados > plan["per_search"]:
        raise HTTPException(status_code=403, detail=f"Seu plano permite até {plan['per_search']} resultados por busca.")
    if request.varredura_profunda and not plan["deep"]:
        raise HTTPException(status_code=403, detail="A varredura profunda por regiões está disponível no plano Agency.")
    if request.testar_url_ativa and not plan["http_check"]:
        raise HTTPException(status_code=403, detail="A verificação HTTP ativa está disponível no plano Agency.")
    quota = _quota(user)
    target = min(request.max_resultados, quota["remaining"] if quota["remaining"] is not None else request.max_resultados)
    if target <= 0:
        raise HTTPException(status_code=403, detail="Sua quota de 30 dias acabou. Faça upgrade ou aguarde a renovação.")
    stop_event = asyncio.Event()
    _stop_events[user_id] = stop_event
    state.update({
        "running": True, "done": False, "stopped": False, "analyzed": 0, "saved": 0,
        "message": "Preparando a busca…", "error": None, "source": None,
        "started_at": utc_now().astimezone().isoformat(timespec="seconds"),
    })
    _search_tasks[user_id] = asyncio.create_task(_run_search(request, dict(user), state, stop_event, target))
    return {"started": True, "source": "OpenStreetMap (Overpass)", "target": target}


@app.post("/api/search/stop")
async def stop_search(user: dict = Depends(current_user)) -> dict[str, Any]:
    user_id = int(user["id"])
    state = _user_state(user_id)
    if not state["running"]:
        return {"stopped": False, "message": "Não há busca em andamento."}
    event = _stop_events.get(user_id)
    if event:
        event.set()
    task = _search_tasks.get(user_id)
    if task and not task.done():
        task.cancel()
    return {"stopped": True, "message": "Interrupção solicitada; os dados já gravados permanecem disponíveis."}


@app.get("/api/leads")
async def get_leads(
    q: str = Query(default="", max_length=200), limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0), user: dict = Depends(current_user),
) -> dict[str, Any]:
    items, total = list_leads(int(user["id"]), query=q, limit=limit, offset=offset)
    return {"items": _public_leads(user, items), "total": total, "limit": limit, "offset": offset}


def _export_frame(user: dict, query: str = "") -> pd.DataFrame:
    records, _ = list_leads(int(user["id"]), query=query, limit=None)
    return pd.DataFrame(_public_leads(user, records), columns=LEAD_COLUMNS)


@app.get("/api/export/csv")
async def export_csv(user: dict = Depends(current_user)) -> StreamingResponse:
    frame = _export_frame(user)
    content = frame.to_csv(index=False, lineterminator="\n").encode("utf-8-sig")
    return StreamingResponse(io.BytesIO(content), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="leads_sem_site.csv"'})


@app.get("/api/export/xlsx")
async def export_xlsx(user: dict = Depends(current_user)) -> StreamingResponse:
    if not PLANS.get(_quota(user)["plan"], PLANS["free"])["xlsx"]:
        raise HTTPException(status_code=403, detail="A exportação Excel está disponível nos planos Starter, Agency e Admin.")
    frame = _export_frame(user)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="Leads")
        worksheet = writer.sheets["Leads"]
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for cells in worksheet.columns:
            width = min(max(max((len(str(cell.value or "")) for cell in cells), default=10) + 2, 12), 54)
            worksheet.column_dimensions[cells[0].column_letter].width = width
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": 'attachment; filename="leads_sem_site.xlsx"'})


@app.get("/api/admin/users")
async def admin_users(_user: dict = Depends(admin_user)) -> dict[str, Any]:
    with closing(_connect()) as connection:
        rows = connection.execute("SELECT id,email,role,plan,blocked,usage_count,period_started,created_at FROM users ORDER BY created_at DESC").fetchall()
    return {"items": [dict(row) for row in rows]}


@app.patch("/api/admin/users/{user_id}/plan")
async def admin_set_plan(user_id: int, request: PlanUpdate, _user: dict = Depends(admin_user)) -> dict[str, bool]:
    if request.plan not in PLANS:
        raise HTTPException(status_code=422, detail="Plano inválido.")
    with closing(_connect()) as connection:
        target_user = connection.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if target_user and target_user["role"] == "admin" and request.plan != "admin":
            raise HTTPException(status_code=422, detail="A conta administrativa mantém o plano ilimitado.")
        cursor = connection.execute("UPDATE users SET plan=? WHERE id=?", (request.plan, user_id))
        connection.commit()
    if not cursor.rowcount:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/reset-quota")
async def admin_reset_quota(user_id: int, _user: dict = Depends(admin_user)) -> dict[str, bool]:
    with closing(_connect()) as connection:
        cursor = connection.execute("UPDATE users SET usage_count=0,period_started=? WHERE id=?", (utc_now().isoformat(), user_id))
        connection.commit()
    if not cursor.rowcount:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/block")
async def admin_block(user_id: int, blocked: bool = Query(...), _user: dict = Depends(admin_user)) -> dict[str, bool]:
    with closing(_connect()) as connection:
        target_user = connection.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if target_user and target_user["role"] == "admin" and blocked:
            raise HTTPException(status_code=422, detail="Contas administrativas não podem ser bloqueadas por este painel.")
        cursor = connection.execute("UPDATE users SET blocked=? WHERE id=?", (int(blocked), user_id))
        if blocked:
            connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        connection.commit()
    if not cursor.rowcount:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    return {"ok": True}


def _event_metadata(obj: Any) -> dict[str, Any]:
    metadata = stripe_value(obj, "metadata", {}) or {}
    return dict(metadata) if hasattr(metadata, "items") else {}


def _subscription_for_event(event_type: str, obj: Any) -> Any | None:
    return obj if event_type.startswith("customer.subscription.") else None


@app.post("/api/webhook/stripe")
async def stripe_webhook(request: Request) -> dict[str, Any]:
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Configure STRIPE_WEBHOOK_SECRET para validar o webhook.")
    payload = await request.body()
    if len(payload) > 1_000_000:
        raise HTTPException(status_code=413, detail="Payload do webhook muito grande.")
    try:
        event = await asyncio.to_thread(
            construct_webhook_event,
            payload=payload,
            signature=request.headers.get("Stripe-Signature", ""),
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except InvalidStripeWebhook as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BillingUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logging.getLogger("prospector").exception("Falha ao validar evento Stripe.")
        raise HTTPException(status_code=400, detail="Não foi possível validar o evento Stripe.") from exc

    event_id = str(stripe_value(event, "id", "") or "")
    event_type = str(stripe_value(event, "type", "") or "")
    event_data = stripe_value(event, "data", {}) or {}
    obj = stripe_value(event_data, "object", {}) or {}
    if not event_id or not event_type:
        raise HTTPException(status_code=400, detail="Evento Stripe sem identificador ou tipo.")

    subscription = _subscription_for_event(event_type, obj)
    subscription_id = stripe_id(obj) if subscription is not None else ""
    if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed"}:
        subscription_id = stripe_id(stripe_value(obj, "subscription"))
    if (event_type.startswith("invoice.") or event_type.startswith("checkout.session.")) and subscription_id and STRIPE_SECRET_KEY:
        try:
            subscription = await asyncio.to_thread(
                retrieve_subscription,
                secret_key=STRIPE_SECRET_KEY,
                subscription_id=subscription_id,
            )
        except Exception:
            logging.getLogger("prospector").exception("Falha ao consultar assinatura Stripe associada ao evento.")
            subscription = subscription if event_type.startswith("customer.subscription.") else None
    if event_type.startswith("invoice."):
        subscription_id = invoice_subscription_id(obj)
        if subscription_id and STRIPE_SECRET_KEY and subscription is None:
            try:
                subscription = await asyncio.to_thread(
                    retrieve_subscription,
                    secret_key=STRIPE_SECRET_KEY,
                    subscription_id=subscription_id,
                )
            except Exception:
                logging.getLogger("prospector").exception("Falha ao consultar assinatura Stripe do evento de fatura.")

    metadata = _event_metadata(subscription or obj)
    user_id_value = metadata.get("user_id") or stripe_value(obj, "client_reference_id", "")
    try:
        user_id = int(user_id_value) if user_id_value not in (None, "") else None
    except (TypeError, ValueError):
        user_id = None
    customer_id = stripe_id(stripe_value(obj, "customer"))
    if not customer_id and subscription is not None:
        customer_id = stripe_id(stripe_value(subscription, "customer"))

    subscription_plan = plan_for_subscription(subscription, STRIPE_PRICE_IDS) if subscription is not None else None
    metadata_plan = str(metadata.get("plan", "")).casefold()
    if metadata_plan not in {"starter", "agency"}:
        metadata_plan = ""
    subscription_plan = subscription_plan or metadata_plan or None
    period_end = subscription_period_end(subscription) if subscription is not None else ""
    new_plan: str | None = None
    new_status: str | None = None

    if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        if stripe_value(obj, "mode", "") != "subscription":
            new_status = "incomplete"
            new_plan = "free"
        elif (
            stripe_value(obj, "payment_status", "") in {"paid", "no_payment_required"}
            and subscription_plan
            and str(stripe_value(subscription, "status", "active") or "active").casefold() in {"active", "trialing"}
        ):
            new_status = str(stripe_value(subscription, "status", "active") or "active").casefold()
            new_plan = subscription_plan
        else:
            new_status = "incomplete"
            new_plan = "free"
        subscription_id = stripe_id(stripe_value(obj, "subscription")) or subscription_id
    elif event_type in {"customer.subscription.created", "customer.subscription.updated"}:
        new_status = str(stripe_value(subscription, "status", "") or "").casefold()
        if new_status in {"active", "trialing"} and subscription_plan:
            new_plan = subscription_plan
        elif new_status in {"canceled", "unpaid", "incomplete_expired", "paused", "incomplete"}:
            new_plan = "free"
    elif event_type == "customer.subscription.deleted":
        new_status = "canceled"
        new_plan = "free"
    elif event_type == "invoice.paid":
        new_status = str(stripe_value(subscription, "status", "active") or "active").casefold()
        if new_status in {"active", "trialing"} and subscription_plan:
            new_plan = subscription_plan
        elif new_status in {"canceled", "unpaid", "incomplete_expired", "paused"}:
            new_plan = "free"
    elif event_type == "invoice.payment_failed":
        new_status = str(stripe_value(subscription, "status", "past_due") or "past_due").casefold()
        if new_status in {"canceled", "unpaid", "incomplete_expired", "paused"}:
            new_plan = "free"

    recognized = event_type in {
        "checkout.session.completed", "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed", "customer.subscription.created",
        "customer.subscription.updated", "customer.subscription.deleted",
        "invoice.paid", "invoice.payment_failed",
    }
    if event_type == "checkout.session.async_payment_failed":
        new_status, new_plan = "incomplete", "free"

    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO stripe_events(event_id,event_type,created_at) VALUES(?,?,?)",
                (event_id, event_type, utc_now().isoformat()),
            ).rowcount
            if not inserted:
                connection.commit()
                return {"ok": True, "duplicate": True}
            if not recognized:
                connection.commit()
                return {"ok": True, "ignored": True}

            user_row = None
            if user_id is not None:
                user_row = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if user_row is None and subscription_id:
                user_row = connection.execute(
                    "SELECT * FROM users WHERE stripe_subscription_id=?", (subscription_id,)
                ).fetchone()
            if user_row is None and customer_id:
                user_row = connection.execute(
                    "SELECT * FROM users WHERE stripe_customer_id=?", (customer_id,)
                ).fetchone()
            if user_row is None:
                logging.getLogger("prospector").warning(
                    "Evento Stripe %s (%s) recebido sem usuário Siteless correspondente.", event_id, event_type
                )
                connection.commit()
                return {"ok": True, "ignored": True}

            assignments: list[str] = []
            values: list[Any] = []
            if customer_id:
                assignments.append("stripe_customer_id=?")
                values.append(customer_id)
            if subscription_id:
                assignments.append("stripe_subscription_id=?")
                values.append(subscription_id)
            if new_status:
                assignments.append("subscription_status=?")
                values.append(new_status)
            if period_end:
                assignments.append("subscription_period_end=?")
                values.append(period_end)
            if new_plan:
                assignments.append("plan=?")
                values.append(new_plan)
            if assignments:
                values.append(int(user_row["id"]))
                connection.execute(f"UPDATE users SET {', '.join(assignments)} WHERE id=?", values)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {"ok": True, "event": event_type, "plan": new_plan or "unchanged"}


HTML_PAGE = r'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prospector de empresas sem site</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
:root{--ink:#172b4d;--muted:#667085;--line:#e5e9f0;--accent:#315efb}body{background:#f5f7fb;color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",sans-serif}.app-shell{max-width:1500px;margin:auto;padding:26px 18px 54px}.hero{display:flex;justify-content:space-between;gap:18px;margin-bottom:22px}.hero h1{font-weight:750;letter-spacing:-.04em;font-size:clamp(1.8rem,4vw,2.7rem);margin:0 0 7px}.hero p,.small-hint{color:var(--muted)}.card{border:1px solid var(--line);border-radius:16px;box-shadow:0 5px 22px #16223d0b}.form-label{font-size:.88rem;font-weight:650}.form-control{min-height:43px;border-color:#dce2ec}.btn-primary{background:var(--accent);border-color:var(--accent)}.toolbar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px}.stat{background:#f8faff;border:1px solid var(--line);border-radius:12px;padding:11px 14px;min-width:130px}.stat strong{display:block;font-size:1.35rem}.stat span{color:var(--muted);font-size:.77rem}.table-wrap{overflow:auto;max-height:65vh}table{min-width:1250px}th{position:sticky;top:0;background:#f7f9fc!important;color:#596579!important;font-size:.76rem;text-transform:uppercase}td{vertical-align:middle;font-size:.87rem}.badge-flag{white-space:normal;text-align:left}.empty{color:var(--muted);text-align:center;padding:38px!important}.status-line{min-height:24px;color:var(--muted);font-size:.9rem}.locked-contact{filter:blur(5px);user-select:none}.login-box{max-width:460px;margin:9vh auto}.admin-link{display:none}@media(max-width:700px){.app-shell{padding:18px 12px}.hero{display:block}}
</style></head><body>
<main class="app-shell">
<section id="authPanel" class="card login-box"><div class="card-body p-4"><h1 class="h3 mb-2">Prospector</h1><p class="small-hint">Entre ou crie sua conta grátis para começar.</p><div id="authError" class="alert alert-danger d-none"></div><label class="form-label" for="authEmail">E-mail</label><input class="form-control mb-3" id="authEmail" type="email" autocomplete="username"><label class="form-label" for="authPassword">Senha (mínimo 10 caracteres)</label><input class="form-control mb-3" id="authPassword" type="password" autocomplete="current-password"><div class="d-flex gap-2"><button id="loginButton" class="btn btn-primary flex-fill">Entrar</button><button id="registerButton" class="btn btn-outline-primary flex-fill">Criar conta grátis</button></div></div></section>
<div id="mainApp" class="d-none">
<header class="hero"><div><h1>Prospector de empresas sem site</h1><p>Estabelecimentos e contatos públicos a partir de dados abertos, com armazenamento local por conta.</p></div><div class="text-end"><span id="userBadge" class="badge text-bg-light border rounded-pill px-3 py-2"></span><div class="mt-2"><a id="adminLink" class="btn btn-sm btn-outline-secondary admin-link" href="/admin">Painel Admin</a> <button id="logoutButton" class="btn btn-sm btn-outline-secondary">Sair</button></div></div></header>
<section class="alert alert-primary d-flex flex-wrap justify-content-between align-items-center gap-2"><span id="planSummary"></span><button class="btn btn-sm btn-primary" data-bs-toggle="modal" data-bs-target="#upgradeModal">Ver planos</button></section>
<section class="card mb-3"><div class="card-body p-4"><form id="searchForm"><div class="row g-3">
<div class="col-12 col-md-4"><label class="form-label" for="nicho">Nicho</label><input class="form-control" id="nicho" placeholder="Ex.: salão de beleza, dentista" required minlength="2"></div>
<div class="col-6 col-md-4"><label class="form-label" for="cidade">Cidade</label><input class="form-control" id="cidade" placeholder="Ex.: Curitiba" required minlength="2"></div>
<div class="col-6 col-md-4"><label class="form-label" for="pais">País</label><input class="form-control" id="pais" value="Brasil" required minlength="2"></div>
<div class="col-6 col-md-2"><label class="form-label" for="nota_minima">Nota mínima</label><input class="form-control" type="number" id="nota_minima" min="0" max="5" step=".1" value="0"></div>
<div class="col-6 col-md-2"><label class="form-label" for="avaliacoes_minimas">Avaliações mínimas</label><input class="form-control" type="number" id="avaliacoes_minimas" min="0" value="0"></div>
<div class="col-6 col-md-2"><label class="form-label" for="raio_km">Raio (km)</label><input class="form-control" type="number" id="raio_km" min="0" max="500" value="15"></div>
<div class="col-6 col-md-2"><label class="form-label" for="max_resultados">Máx. novos leads</label><input class="form-control" type="number" id="max_resultados" min="1" max="5000" value="10"></div>
<div class="col-12 col-md-4 d-flex align-items-end gap-3 pb-2 flex-wrap"><div class="form-check"><input class="form-check-input" type="checkbox" id="so_abertas"><label class="form-check-label small" for="so_abertas">Só abertas agora</label></div><div class="form-check"><input class="form-check-input" type="checkbox" id="testar_url_ativa"><label class="form-check-label small" for="testar_url_ativa">Testar URL ativa (Agency)</label></div><div class="form-check"><input class="form-check-input" type="checkbox" id="varredura_profunda"><label class="form-check-label small" for="varredura_profunda">Varredura profunda (Agency)</label></div></div>
<div class="col-12 small-hint">Fonte: OpenStreetMap. Nota, avaliações e horário aberto não são fornecidos de forma consistente pelo OSM; os filtros correspondentes podem encerrar a consulta com aviso.</div>
<div class="col-12 d-flex flex-wrap gap-2 align-items-center"><button class="btn btn-primary px-4" id="startButton">Iniciar busca</button><button class="btn btn-outline-danger px-4" type="button" id="stopButton" disabled>Parar</button><a class="btn btn-outline-secondary ms-md-auto" href="/api/export/csv">Exportar CSV</a><a class="btn btn-outline-secondary" id="xlsxLink" href="/api/export/xlsx">Exportar Excel</a></div>
</div></form></div></section>
<section class="card mb-3"><div class="card-body p-3 p-md-4"><div class="toolbar mb-3"><div class="d-flex flex-wrap gap-2"><div class="stat"><strong id="analyzedCount">0</strong><span>Analisados</span></div><div class="stat"><strong id="savedCount">0</strong><span>Novos leads</span></div><div class="stat"><strong id="totalCount">0</strong><span>Total nesta conta</span></div></div><input class="form-control" id="tableFilter" type="search" style="max-width:450px" placeholder="Filtrar nome, telefone, cidade, e-mail..."></div><div class="progress mb-2" style="height:8px"><div class="progress-bar" id="progressBar"></div></div><div class="status-line" id="statusLine">Pronto para iniciar.</div></div></section>
<section class="card"><div class="card-header bg-white border-0 px-4 pt-4 pb-2 d-flex justify-content-between"><div><h2 class="h5 mb-1">Empresas qualificadas</h2><div class="small-hint">Contatos de páginas e resultados públicos; confirme os dados antes de abordar.</div></div><button class="btn btn-sm btn-outline-secondary" id="refreshButton">Atualizar</button></div><div class="table-wrap"><table class="table table-hover mb-0"><thead><tr><th>Empresa</th><th>Nicho</th><th>Cidade / País</th><th>Telefone</th><th>WhatsApp</th><th>Nota</th><th>Motivo</th><th>Links e contatos</th><th>Pitch</th></tr></thead><tbody id="leadsBody"><tr><td class="empty" colspan="9">Carregando…</td></tr></tbody></table></div><div class="card-footer bg-white border-0 px-4 py-3 small-hint" id="tableFooter"></div></section>
<p class="small-hint text-center mt-3">Dados de estabelecimentos © OpenStreetMap contributors, licenciados sob ODbL. Consulte os termos aplicáveis e respeite as regras locais de comunicação.</p>
</div></main>
<div class="modal fade" id="upgradeModal" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content"><div class="modal-header"><h2 class="modal-title fs-5">Escolha seu plano</h2><button class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><p><b>Grátis</b>: 15 leads a cada 30 dias, até 10 por busca, CSV e 5 contatos visíveis.</p><p><b>Starter — $19/month</b>: 600 leads, Excel, WhatsApp direto e pitch multilíngue.</p><p><b>Agency — $49/month</b>: 3.000 leads, varredura profunda e verificação HTTP.</p><div class="d-flex flex-wrap gap-2"><button id="starterCheckout" class="btn btn-outline-primary">Assinar Starter</button><button id="agencyCheckout" class="btn btn-primary">Assinar Agency</button><button id="manageBilling" class="btn btn-outline-secondary d-none">Gerenciar assinatura</button></div><div id="billingError" class="alert alert-danger py-2 mt-3 d-none"></div><div class="small-hint mt-3">Pagamento seguro pelo Stripe. O plano é liberado após a confirmação do pagamento.</div></div></div></div></div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
let me=null,allLeads=[],pollTimer=null,searchTarget=0;const form=document.getElementById('searchForm'),body=document.getElementById('leadsBody');
function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function safeLink(v){let s=String(v||'').trim();return /^https?:\/\//i.test(s)?s:''}function links(label,v){return String(v||'').split(';').map(x=>{let u=safeLink(x);return u?'<a class="me-2" target="_blank" rel="noopener noreferrer" href="'+esc(u)+'">'+esc(label)+'</a>':''}).join('')}function emails(v){return String(v||'').split(';').map(x=>x.trim()?'<div><a href="mailto:'+esc(x.trim())+'">'+esc(x.trim())+'</a></div>':'').join('')}
function normalizeCountry(c){return String(c||'').trim().toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/[^a-z0-9]+/g,' ').trim()}function language(c){let n=normalizeCountry(c);if(['brasil','brazil','br','portugal','pt'].includes(n))return'pt';if(['spain','espana','es','mexico','mx','argentina','ar','bolivia','bo','chile','cl','colombia','co','costa rica','cr','cuba','cu','dominican republic','republica dominicana','do','ecuador','ec','el salvador','sv','guatemala','gt','honduras','hn','nicaragua','ni','panama','pa','paraguay','py','peru','pe','uruguay','uy','venezuela','ve','puerto rico','pr'].includes(n))return'es';return'en'}function pitch(l){let n=l.nome||'sua empresa',s=l.nicho||'serviços';switch(language(l.pais)){case'pt':return'Olá! Encontrei a '+n+' e vi que vocês oferecem '+s+'. Posso ajudar a criar um site simples para apresentar seus serviços e facilitar novos contatos. Faz sentido conversarmos?';case'es':return'¡Hola! Encontré '+n+' y vi que ofrecen '+s+'. Puedo ayudarles a crear un sitio web sencillo para presentar sus servicios y facilitar nuevos contactos. ¿Te gustaría conversar?';default:return'Hi! I found '+n+' and saw that you offer '+s+'. I can help create a simple website to showcase your services and make it easier for new customers to reach you. Would you be open to a quick chat?'}}function flag(reason){let c=reason==='sem website'?'text-bg-danger':reason.includes('estacionado')?'text-bg-warning':reason.includes('gratuita')||reason.includes('amadora')?'text-bg-info':'text-bg-primary';return'<span class="badge '+c+'">'+esc(reason)+'</span>'}
function render(){let f=document.getElementById('tableFilter').value.trim().toLowerCase(),filtered=allLeads.filter(x=>Object.values(x).join(' ').toLowerCase().includes(f));if(!filtered.length){body.innerHTML='<tr><td class="empty" colspan="9">'+(allLeads.length?'Nenhum resultado corresponde ao filtro.':'Nenhum lead salvo ainda.')+'</td></tr>';return}body.innerHTML=filtered.map((l,i)=>{let locked=l.contacts_locked,wrap=content=>locked?'<div class="locked-contact">'+content+'</div><button class="btn btn-link btn-sm p-0" data-bs-toggle="modal" data-bs-target="#upgradeModal">Desbloquear contatos</button>':content;let socials=['Instagram|instagram','Facebook|facebook','TikTok|tiktok','LinkedIn|linkedin','YouTube|youtube','Agendar|link_agendamento'].map(x=>{let [label,key]=x.split('|');return links(label,l[key])}).join('');let details=wrap(socials+emails(l.email)+(l.website_original?'<div class="small text-secondary text-break">'+esc(l.website_original)+'</div>':''));let whatsapp=wrap(links('Abrir WhatsApp',l.link_whatsapp_direto)+(l.whatsapp?'<div class="small text-secondary">'+esc(l.whatsapp)+'</div>':'—'));let rate=l.nota==null?'—':esc(Number(l.nota).toFixed(1))+' ★';let pitchBtn=me.quota.features.pitch?'<button class="btn btn-sm btn-outline-primary pitch-button" data-index="'+i+'">Copiar Pitch WhatsApp</button>':'<button class="btn btn-sm btn-outline-secondary" data-bs-toggle="modal" data-bs-target="#upgradeModal">Pitch (upgrade)</button>';return'<tr><td><div class="fw-semibold">'+esc(l.nome)+'</div><div class="small text-secondary">'+esc(l.endereco||'Endereço não informado')+'</div></td><td>'+esc(l.nicho)+'</td><td>'+esc([l.cidade,l.pais].filter(Boolean).join(' · '))+'</td><td>'+wrap(l.telefone?'<a href="tel:'+esc(l.telefone.replace(/[^\d+]/g,''))+'">'+esc(l.telefone)+'</a>':'—')+'</td><td>'+whatsapp+'</td><td>'+rate+'<div class="small text-secondary">'+esc(l.avaliacoes||0)+' avaliações</div></td><td>'+flag(l.motivo_flag)+'</td><td>'+links('Mapa',l.link_maps)+details+'</td><td>'+pitchBtn+'</td></tr>'}).join('');document.querySelectorAll('.pitch-button').forEach(b=>b.onclick=async()=>{await navigator.clipboard.writeText(pitch(filtered[Number(b.dataset.index)]));b.textContent='Copiado';setTimeout(()=>b.textContent='Copiar Pitch WhatsApp',1400)})}
async function api(url,opts={}){let r=await fetch(url,{...opts,headers:{...(opts.headers||{}),...(opts.body?{'Content-Type':'application/json'}:{})}}),d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.detail||'Falha ao falar com o servidor.');return d}
async function loadLeads(){let d=await api('/api/leads?limit=5000');allLeads=d.items||[];document.getElementById('tableFooter').textContent='Exibindo '+allLeads.length+' de '+d.total+' registros nesta conta.';document.getElementById('totalCount').textContent=d.total||0;render()}
async function updateStatus(){try{let d=await api('/api/status');document.getElementById('analyzedCount').textContent=d.analyzed||0;document.getElementById('savedCount').textContent=d.saved||0;document.getElementById('totalCount').textContent=d.database_total||0;if(d.quota){me.quota=d.quota;document.getElementById('planSummary').textContent=d.quota.remaining==null?'Uso ilimitado de administrador':d.quota.used+' / '+d.quota.monthly_limit+' leads neste período de 30 dias · restam '+d.quota.remaining}document.getElementById('statusLine').textContent=d.error?d.message+' '+d.error:d.message;document.getElementById('startButton').disabled=!!d.running;document.getElementById('stopButton').disabled=!d.running;let p=d.running?Math.min(99,(d.saved/Math.max(1,searchTarget||Number(document.getElementById('max_resultados').value)))*100):(d.done&&d.analyzed?100:0);document.getElementById('progressBar').style.width=p+'%';if(!d.running&&pollTimer){clearInterval(pollTimer);pollTimer=null;await loadLeads()}}catch(e){document.getElementById('statusLine').textContent=e.message}}
async function beginCheckout(plan,button){let box=document.getElementById('billingError');box.classList.add('d-none');button.disabled=true;try{let d=await api('/api/billing/checkout',{method:'POST',body:JSON.stringify({plan})});location.assign(d.url)}catch(e){box.textContent=e.message;box.classList.remove('d-none');button.disabled=false}}
async function openBillingPortal(){let box=document.getElementById('billingError');box.classList.add('d-none');try{let d=await api('/api/billing/portal',{method:'POST'});location.assign(d.url)}catch(e){box.textContent=e.message;box.classList.remove('d-none')}}
async function showApp(){try{me=await api('/api/auth/me');document.getElementById('authPanel').classList.add('d-none');document.getElementById('mainApp').classList.remove('d-none');let q=me.quota;document.getElementById('userBadge').textContent=me.email+' · '+q.plan_name;document.getElementById('planSummary').textContent=q.remaining==null?'Uso ilimitado de administrador':q.used+' / '+q.monthly_limit+' leads neste período de 30 dias · restam '+q.remaining;document.getElementById('adminLink').style.display=me.role==='admin'?'inline-block':'none';document.getElementById('xlsxLink').style.display=q.features.xlsx?'inline-block':'none';document.getElementById('max_resultados').max=q.per_search_limit;document.getElementById('max_resultados').value=Math.min(Number(document.getElementById('max_resultados').value)||10,q.per_search_limit);for(let id of ['testar_url_ativa','varredura_profunda'])document.getElementById(id).disabled=!(id==='testar_url_ativa'?q.features.http_check:q.features.deep);document.getElementById('manageBilling').classList.toggle('d-none',!me.can_manage_billing);await loadLeads();await updateStatus()}catch(_){document.getElementById('authPanel').classList.remove('d-none');document.getElementById('mainApp').classList.add('d-none')}}
document.getElementById('starterCheckout').onclick=e=>beginCheckout('starter',e.currentTarget);document.getElementById('agencyCheckout').onclick=e=>beginCheckout('agency',e.currentTarget);document.getElementById('manageBilling').onclick=openBillingPortal;async function auth(action){let box=document.getElementById('authError');box.classList.add('d-none');try{await api('/api/auth/'+action,{method:'POST',body:JSON.stringify({email:document.getElementById('authEmail').value,password:document.getElementById('authPassword').value})});await showApp()}catch(e){box.textContent=e.message;box.classList.remove('d-none')}}document.getElementById('loginButton').onclick=()=>auth('login');document.getElementById('registerButton').onclick=()=>auth('register');document.getElementById('logoutButton').onclick=async()=>{await api('/api/auth/logout',{method:'POST'});location.reload()};
form.addEventListener('submit',async e=>{e.preventDefault();let payload={nicho:document.getElementById('nicho').value.trim(),cidade:document.getElementById('cidade').value.trim(),pais:document.getElementById('pais').value.trim(),nota_minima:Number(document.getElementById('nota_minima').value||0),avaliacoes_minimas:Number(document.getElementById('avaliacoes_minimas').value||0),raio_km:Number(document.getElementById('raio_km').value||0),max_resultados:Number(document.getElementById('max_resultados').value||10),so_abertas:document.getElementById('so_abertas').checked,testar_url_ativa:document.getElementById('testar_url_ativa').checked,varredura_profunda:document.getElementById('varredura_profunda').checked};try{let started=await api('/api/search/start',{method:'POST',body:JSON.stringify(payload)});searchTarget=started.target||payload.max_resultados;if(pollTimer)clearInterval(pollTimer);pollTimer=setInterval(updateStatus,1200);await updateStatus()}catch(err){document.getElementById('statusLine').textContent=err.message}});document.getElementById('stopButton').onclick=async()=>{await api('/api/search/stop',{method:'POST'});await updateStatus()};document.getElementById('refreshButton').onclick=loadLeads;document.getElementById('tableFilter').oninput=render;showApp();
</script></body></html>'''
HTML_PAGE = HTML_PAGE.replace("<title>Prospector de empresas sem site</title>", "<title>Siteless</title>")
HTML_PAGE = HTML_PAGE.replace('<h1 class="h3 mb-2">Prospector</h1>', '<h1 class="h3 mb-2">Siteless</h1>')
HTML_PAGE = HTML_PAGE.replace("<h1>Prospector de empresas sem site</h1>", "<h1>Siteless</h1>")


ADMIN_PAGE = r'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin · Prospector</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet"></head><body class="bg-light"><main class="container py-4"><div class="d-flex justify-content-between"><h1 class="h3">Painel Admin</h1><a href="/" class="btn btn-outline-secondary">Voltar</a></div><p class="text-secondary">Planos, quotas e bloqueio de contas.</p><div id="message" class="alert d-none"></div><div class="table-responsive"><table class="table table-striped bg-white"><thead><tr><th>ID</th><th>E-mail</th><th>Plano</th><th>Uso / 30 dias</th><th>Conta</th><th>Ações</th></tr></thead><tbody id="users"></tbody></table></div></main><script>
async function req(url,opt={}){let r=await fetch(url,{...opt,headers:{...(opt.headers||{}),...(opt.body?{'Content-Type':'application/json'}:{})}}),d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.detail||'Erro');return d}async function load(){let d=await req('/api/admin/users');document.getElementById('users').innerHTML=d.items.map(u=>'<tr><td>'+u.id+'</td><td>'+u.email+'</td><td><select class="form-select form-select-sm" id="plan'+u.id+'">'+['free','starter','agency','admin'].map(p=>'<option '+(p===u.plan?'selected':'')+'>'+p+'</option>').join('')+'</select></td><td>'+u.usage_count+'</td><td>'+(u.blocked?'Bloqueada':'Ativa')+'</td><td class="d-flex gap-1"><button class="btn btn-sm btn-primary" onclick="changePlan('+u.id+')">Salvar plano</button><button class="btn btn-sm btn-outline-secondary" onclick="reset('+u.id+')">Zerar quota</button><button class="btn btn-sm btn-outline-danger" onclick="block('+u.id+','+(!u.blocked)+')">'+(u.blocked?'Desbloquear':'Bloquear')+'</button></td></tr>').join('')}async function changePlan(id){await req('/api/admin/users/'+id+'/plan',{method:'PATCH',body:JSON.stringify({plan:document.getElementById('plan'+id).value})});load()}async function reset(id){await req('/api/admin/users/'+id+'/reset-quota',{method:'POST'});load()}async function block(id,b){await req('/api/admin/users/'+id+'/block?blocked='+b,{method:'POST'});load()}load().catch(e=>{let m=document.getElementById('message');m.textContent=e.message;m.className='alert alert-danger'})</script></body></html>'''
