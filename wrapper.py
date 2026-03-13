import asyncio
import csv
import dataclasses
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import shutil
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx
import redis.asyncio as aioredis

# ---------------------------------------------------------------------------
# UTF-8 JSON Response
# ---------------------------------------------------------------------------

class UTF8JSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False).encode("utf-8")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wrapper")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GOSOM_API_URL = os.environ.get("GOSOM_API_URL", "http://localhost:8080/api/v1")
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "gmapsdata/json")).resolve()
MAX_POLLS = 720          # ~36 dk (ortalama 3sn aralıkla)

# job_id doğrulama: kesin UUID formatı
_VALID_JOB_ID = re.compile(
    r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$"
)

# Klasör adı doğrulama: YYYY-MM-DD formatı
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Redis TTL: 48 saat
_REDIS_TTL = 172800

DASHBOARD_PASSWORD_HASH = os.environ.get("DASHBOARD_PASSWORD_HASH", "").strip()
DASHBOARD_SESSION_TTL_HOURS = int(os.environ.get("DASHBOARD_SESSION_TTL_HOURS", "12"))
DASHBOARD_COOKIE_NAME = os.environ.get("DASHBOARD_COOKIE_NAME", "gmaps_admin_session")
DASHBOARD_SECURE_COOKIES = os.environ.get("DASHBOARD_SECURE_COOKIES", "true").strip().lower() not in {
    "0", "false", "no"
}
_ADMIN_SESSION_PREFIX = "admin:session:"
_ADMIN_LOGIN_ATTEMPT_PREFIX = "admin:login:"
_ADMIN_LOGIN_ATTEMPT_LIMIT = 10
_ADMIN_LOGIN_ATTEMPT_WINDOW = 900

_password_hasher = PasswordHasher()
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


def _build_redis_url() -> str:
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        return redis_url

    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = os.environ.get("REDIS_PORT", "6379")
    redis_db = os.environ.get("REDIS_DB", "0")
    return f"redis://{redis_host}:{redis_port}/{redis_db}"


REDIS_URL = _build_redis_url()

# ---------------------------------------------------------------------------
# Value / CSV helpers  (mevcut – değişiklik yok)
# ---------------------------------------------------------------------------

def _clean_value(v):
    """Tek bir hücreyi temizle: NaN/inf → None, JSON string → parsed obje, 'null' → None."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s == "null" or s == "":
        return None
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            return json.loads(s)
        except (json.JSONDecodeError, ValueError):
            pass
    return v


def _parse_csv(text: str) -> list[dict]:
    """CSV text → temizlenmiş JSON-uyumlu dict listesi (pandas'sız)."""
    reader = csv.DictReader(io.StringIO(text))
    results = []
    for row in reader:
        results.append({k: _clean_value(v) for k, v in row.items()})
    return results

# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------

def _cache_key_hash(
    query: str,
    depth: int,
    max_reviews: int,
    extra_reviews: bool = False,
    place_id: str | None = None,
    lang: str = "tr",
    geo: str | None = None,
    zoom: int | None = None,
    radius: float | None = None,
    email: bool = False,
    fast_mode: bool = False,
) -> str:
    """Dedup için deterministic hash."""
    today = date.today().isoformat()
    raw = (
        f"{query.strip().lower()}:{depth}:{max_reviews}:{int(extra_reviews)}:"
        f"{(place_id or '').strip().lower()}:{lang.strip().lower()}:"
        f"{(geo or '').strip().lower()}:{zoom if zoom is not None else ''}:"
        f"{radius if radius is not None else ''}:{int(email)}:{int(fast_mode)}:{today}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

# ---------------------------------------------------------------------------
# JobRecord
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class JobRecord:
    job_id: str
    query: str
    depth: int
    max_reviews: int
    status: str           # "pending" | "ok" | "failed"
    created_at: str       # ISO datetime
    date: str             # "YYYY-MM-DD"
    result_count: int | None = None
    error: str | None = None
    extra_reviews: bool = False
    place_id: str | None = None
    lang: str = "tr"
    geo: str | None = None
    zoom: int | None = None
    radius: float | None = None
    email: bool = False
    fast_mode: bool = False

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "JobRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

# ---------------------------------------------------------------------------
# Module-level state — Redis replaces in-memory dicts
# ---------------------------------------------------------------------------

_redis: aioredis.Redis | None = None
_active_tasks: set[asyncio.Task] = set()  # per-pod background poll task'ları

# ---------------------------------------------------------------------------
# Path helpers — job'un kendi tarihini kullanır
# ---------------------------------------------------------------------------

def _validate_job_id(job_id: str) -> None:
    """job_id'nin UUID formatında olduğunu doğrula (path traversal koruması)."""
    if not _VALID_JOB_ID.match(job_id):
        raise ValueError(f"Geçersiz job_id formatı: {job_id}")


def _normalize_place_id(place_id: str | None) -> str | None:
    if place_id is None:
        return None
    value = str(place_id).strip()
    if not value:
        return None
    if len(value) > 256:
        raise ValueError("place_id çok uzun")
    return value


def _normalize_lang(lang: str | None) -> str:
    value = str(lang or "tr").strip().lower()
    if not value:
        return "tr"
    if len(value) > 12 or not re.match(r"^[a-z-]+$", value):
        raise ValueError("lang geçersiz")
    return value


def _normalize_geo(geo: str | None) -> str | None:
    if geo is None:
        return None
    value = str(geo).strip()
    if not value:
        return None
    if not re.match(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?$", value):
        raise ValueError("geo 'lat,lng' formatında olmalı")
    return value


def _build_gosom_keyword(query: str, place_id: str | None) -> str:
    if not place_id:
        return query
    label = query.strip() or "Google"
    return (
        "https://www.google.com/maps/search/?api=1"
        f"&query={quote_plus(label)}&query_place_id={quote_plus(place_id)}"
    )


def _bool_from_input(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _dashboard_enabled() -> bool:
    return bool(DASHBOARD_PASSWORD_HASH)


def _admin_session_key(session_id: str) -> str:
    return f"{_ADMIN_SESSION_PREFIX}{session_id}"


def _admin_login_attempt_key(remote_addr: str) -> str:
    return f"{_ADMIN_LOGIN_ATTEMPT_PREFIX}{remote_addr}"


def _require_dashboard_enabled() -> None:
    if not _dashboard_enabled():
        raise HTTPException(status_code=503, detail="Dashboard devre dışı")


def _render_template(request: Request, template_name: str, context: dict, status_code: int = 200):
    ctx = {
        "request": request,
        "dashboard_enabled": _dashboard_enabled(),
        **context,
    }
    response = templates.TemplateResponse(request, template_name, ctx, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


async def _create_admin_session() -> tuple[str, dict]:
    session_id = secrets.token_urlsafe(32)
    session = {
        "csrf": secrets.token_urlsafe(24),
        "created_at": datetime.now().isoformat(),
    }
    ttl_seconds = max(DASHBOARD_SESSION_TTL_HOURS, 1) * 3600
    await _redis.set(_admin_session_key(session_id), json.dumps(session), ex=ttl_seconds)
    return session_id, session


async def _get_admin_session(request: Request) -> tuple[str, dict] | None:
    _require_dashboard_enabled()
    session_id = request.cookies.get(DASHBOARD_COOKIE_NAME)
    if not session_id:
        return None
    raw = await _redis.get(_admin_session_key(session_id))
    if not raw:
        return None
    try:
        return session_id, json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _destroy_admin_session(session_id: str | None) -> None:
    if not session_id:
        return
    await _redis.delete(_admin_session_key(session_id))


async def _record_failed_login(remote_addr: str) -> int:
    key = _admin_login_attempt_key(remote_addr)
    attempts = await _redis.incr(key)
    if attempts == 1:
        await _redis.expire(key, _ADMIN_LOGIN_ATTEMPT_WINDOW)
    return int(attempts)


async def _clear_failed_logins(remote_addr: str) -> None:
    await _redis.delete(_admin_login_attempt_key(remote_addr))


async def _too_many_login_attempts(remote_addr: str) -> bool:
    attempts = await _redis.get(_admin_login_attempt_key(remote_addr))
    return int(attempts or 0) >= _ADMIN_LOGIN_ATTEMPT_LIMIT


def _set_admin_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        DASHBOARD_COOKIE_NAME,
        session_id,
        httponly=True,
        secure=DASHBOARD_SECURE_COOKIES,
        samesite="strict",
        max_age=max(DASHBOARD_SESSION_TTL_HOURS, 1) * 3600,
        path="/",
    )


def _clear_admin_cookie(response: Response) -> None:
    response.delete_cookie(DASHBOARD_COOKIE_NAME, path="/")


def _redirect_to_login() -> RedirectResponse:
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


def _verify_password(password: str) -> bool:
    try:
        return _password_hasher.verify(DASHBOARD_PASSWORD_HASH, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def _remote_addr_from_request(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _validate_csrf(session: dict, csrf_token: str | None) -> None:
    expected = str(session.get("csrf", ""))
    provided = str(csrf_token or "")
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        raise HTTPException(status_code=403, detail="Geçersiz CSRF token")

def _ensure_dir_for_date(job_date: str) -> Path:
    """Yazma işlemleri için: klasörü oluşturarak döner."""
    d = DATA_ROOT / job_date
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dir_for_date(job_date: str) -> Path:
    """Okuma işlemleri için: klasörü oluşturmadan döner."""
    return DATA_ROOT / job_date


def _index_path_for_date(job_date: str) -> Path:
    return _ensure_dir_for_date(job_date) / "index.json"


def _result_path_for_write(job_id: str, job_date: str) -> Path:
    """Yazma: klasörü oluşturur, path traversal korumalı."""
    _validate_job_id(job_id)
    p = (_ensure_dir_for_date(job_date) / f"{job_id}.json").resolve()
    root = DATA_ROOT.resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"Geçersiz yol: {p}")
    return p


def _result_path_for_read(job_id: str, job_date: str) -> Path:
    """Okuma: klasör oluşturmaz, path traversal korumalı."""
    _validate_job_id(job_id)
    p = (_dir_for_date(job_date) / f"{job_id}.json").resolve()
    root = DATA_ROOT.resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"Geçersiz yol: {p}")
    return p


def _job_result_urls(job_id: str) -> dict[str, str]:
    return {
        "download_url": f"/api/jobs/{job_id}/result",
        "result_url": f"/api/jobs/{job_id}/result/json",
    }


def _result_redis_key(job_id: str) -> str:
    _validate_job_id(job_id)
    return f"result:{job_id}"

# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

async def _save_job_to_redis(rec: JobRecord, retries: int = 3):
    """JobRecord'u Redis'e kaydet. Terminal state'lerde retry ile güvenli."""
    for attempt in range(1, retries + 1):
        try:
            pipe = _redis.pipeline()
            pipe.set(f"job:{rec.job_id}", json.dumps(rec.to_dict(), ensure_ascii=False), ex=_REDIS_TTL)
            pipe.sadd(f"jobs:date:{rec.date}", rec.job_id)
            pipe.expire(f"jobs:date:{rec.date}", _REDIS_TTL)
            await pipe.execute()
            return
        except Exception as e:
            if attempt == retries:
                raise
            log.warning(f"[{rec.job_id[:8]}] Redis kayıt denemesi {attempt}/{retries} başarısız: {e}")
            await asyncio.sleep(0.5 * attempt)


async def _get_job_from_redis(job_id: str) -> JobRecord | None:
    """Redis'ten JobRecord oku."""
    data = await _redis.get(f"job:{job_id}")
    if not data:
        return None
    try:
        return JobRecord.from_dict(json.loads(data))
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


async def _save_result_to_redis(job_id: str, result_text: str) -> None:
    await _redis.set(_result_redis_key(job_id), result_text, ex=_REDIS_TTL)


async def _get_result_from_redis(job_id: str) -> str | None:
    return await _redis.get(_result_redis_key(job_id))


async def _get_result_content_or_raise(rec: JobRecord) -> str | None:
    result_text = await _get_result_from_redis(rec.job_id)
    if result_text is not None:
        return result_text

    result_file = _result_path_for_read(rec.job_id, rec.date)
    if not result_file.exists():
        raise HTTPException(status_code=410, detail="Sonuç dosyası silinmiş")

    try:
        result_text = await asyncio.to_thread(result_file.read_text, encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Sonuç dosyası okunamadı: {e}")

    try:
        await _save_result_to_redis(rec.job_id, result_text)
    except Exception as e:
        log.warning(f"[{rec.job_id[:8]}] Sonuç Redis cache'e yazılamadı: {e}")

    return result_text


async def _build_result_response(rec: JobRecord, inline: bool) -> Response:
    result_text = await _get_result_content_or_raise(rec)
    headers = {}
    if inline:
        headers["Content-Disposition"] = "inline"
    else:
        headers["Content-Disposition"] = f'attachment; filename="{rec.job_id}.json"'
    return Response(
        content=result_text,
        media_type="application/json; charset=utf-8",
        headers=headers,
    )

# ---------------------------------------------------------------------------
# Disk persistence helpers (backup — Redis primary source of truth)
# ---------------------------------------------------------------------------

def _write_index_records(job_date: str, records: list[dict]) -> None:
    """index.json'ı atomik olarak diske yaz (senkron, thread'de çağrılır)."""
    path = _index_path_for_date(job_date)
    tmp = path.with_name("index.json.tmp")
    try:
        tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        log.error(f"index.json yazılamadı ({job_date}): {e}")
        raise


async def _save_index(job_date: str | None = None):
    """Belirtilen günün index.json'ını disk backup olarak yaz.
    Redis'ten o günün job'larını alıp diske yazar."""
    target_date = job_date or date.today().isoformat()
    try:
        job_ids = await _redis.smembers(f"jobs:date:{target_date}")
        if not job_ids:
            return
        records = []
        for jid in job_ids:
            rec = await _get_job_from_redis(jid)
            if rec:
                records.append(rec.to_dict())
        await asyncio.to_thread(_write_index_records, target_date, records)
    except Exception as e:
        log.warning(f"Index disk backup yazılamadı ({target_date}): {e}")


async def _load_todays_index():
    """Startup'ta Redis'ten bugünün job'larını yükle. Fallback: disk."""
    today = date.today().isoformat()

    # Redis'ten yükle
    try:
        job_ids = await _redis.smembers(f"jobs:date:{today}")
        if job_ids:
            count = 0
            for jid in job_ids:
                rec = await _get_job_from_redis(jid)
                if not rec:
                    continue
                count += 1
                if rec.status == "pending":
                    # Sonuç Redis veya disk backup'ta varsa Redis'i güncelle, tekrar poll'a gerek yok
                    try:
                        result_text = await _get_result_from_redis(rec.job_id)
                        if result_text is not None:
                            data = json.loads(result_text)
                            rec.status = "ok"
                            rec.result_count = len(data)
                            await _save_job_to_redis(rec)
                            log.info(f"[{rec.job_id[:8]}] Redis'ten recover edildi ({rec.result_count} kayıt)")
                            continue
                        result_file = _result_path_for_read(rec.job_id, rec.date)
                        if result_file.exists():
                            data = json.loads(result_file.read_text(encoding="utf-8"))
                            rec.status = "ok"
                            rec.result_count = len(data)
                            await _save_result_to_redis(
                                rec.job_id,
                                json.dumps(data, ensure_ascii=False, indent=2),
                            )
                            await _save_job_to_redis(rec)
                            log.info(f"[{rec.job_id[:8]}] Disk'ten recover edildi ({rec.result_count} kayıt)")
                            continue
                    except Exception:
                        pass  # Dosya okunamadı, normal polling'e devam
                    log.info(f"Pending job için polling yeniden başlatılıyor: {rec.job_id[:8]}")
                    task = asyncio.create_task(_poll_and_finalize(rec))
                    _active_tasks.add(task)
                    task.add_done_callback(_active_tasks.discard)
            log.info(f"Redis'ten {count} job yüklendi (bugün)")
            return
    except Exception as e:
        log.warning(f"Redis'ten yükleme başarısız, disk fallback: {e}")

    # Fallback: disk'ten yükle ve Redis'e aktar
    path = _dir_for_date(today) / "index.json"
    if not path.exists():
        return
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"index.json okunamadı: {e}")
        return
    for d in records:
        try:
            rec = JobRecord.from_dict(d)
        except (TypeError, KeyError) as e:
            log.warning(f"index.json kaydı atlandı (şema uyumsuzluğu): {e}")
            continue
        # Redis'e aktar
        try:
            await _save_job_to_redis(rec)
            h = _cache_key_hash(
                rec.query,
                rec.depth,
                rec.max_reviews,
                extra_reviews=rec.extra_reviews,
                place_id=rec.place_id,
                lang=rec.lang,
                geo=rec.geo,
                zoom=rec.zoom,
                radius=rec.radius,
                email=rec.email,
                fast_mode=rec.fast_mode,
            )
            await _redis.set(f"qidx:{rec.date}:{h}", rec.job_id, ex=_REDIS_TTL)
        except Exception as e:
            log.warning(f"Disk→Redis aktarım hatası [{rec.job_id[:8]}]: {e}")
        if rec.status == "pending":
            log.info(f"Pending job için polling yeniden başlatılıyor: {rec.job_id[:8]}")
            task = asyncio.create_task(_poll_and_finalize(rec))
            _active_tasks.add(task)
            task.add_done_callback(_active_tasks.discard)
    log.info(f"Diskten {len(records)} job yüklendi ve Redis'e aktarıldı (bugün)")

# ---------------------------------------------------------------------------
# Daily cleanup — dünden eski klasörleri sil (bugün+dün korunur)
# ---------------------------------------------------------------------------

_last_cleanup_date: str = ""
_cleanup_fail_count: int = 0


async def _daily_cleanup():
    global _last_cleanup_date, _cleanup_fail_count
    today = date.today().isoformat()
    if _last_cleanup_date == today:
        return
    _cleanup_fail_count = 0
    _last_cleanup_date = today

    if not DATA_ROOT.exists():
        return

    yesterday = (date.today() - timedelta(days=1)).isoformat()

    # Aktif job tarihlerini Redis'ten al
    active_dates = set()
    try:
        job_ids = await _redis.smembers(f"jobs:date:{today}")
        if job_ids:
            for jid in job_ids:
                rec = await _get_job_from_redis(jid)
                if rec and rec.status == "pending":
                    active_dates.add(rec.date)
    except Exception as e:
        log.warning(f"Redis'ten aktif tarihler alınamadı: {e}")

    dirs_to_delete = []
    try:
        children = list(DATA_ROOT.iterdir())
        _cleanup_fail_count = 0
    except OSError as e:
        _cleanup_fail_count += 1
        if _cleanup_fail_count < 3:
            log.error(f"DATA_ROOT listelenemedi, cleanup atlandı (#{_cleanup_fail_count}/3), tekrar denenecek: {e}")
            _last_cleanup_date = ""
        else:
            log.error(f"DATA_ROOT listelenemedi (#{_cleanup_fail_count}/3), bugün artık denenmeyecek: {e}")
        return

    for child in children:
        if child.is_dir() and _DATE_DIR_RE.match(child.name) and child.name < yesterday and child.name not in active_dates:
            dirs_to_delete.append(child)

    for d in dirs_to_delete:
        log.info(f"Eski klasör siliniyor: {d}")
        try:
            await asyncio.to_thread(shutil.rmtree, d)
        except Exception as e:
            log.warning(f"Klasör silinemedi {d}: {e}")

# ---------------------------------------------------------------------------
# Background polling
# ---------------------------------------------------------------------------

def _write_result_sync(job_id: str, job_date: str, result_text: str) -> None:
    """JSON sonucu diske atomik olarak yaz (senkron, thread'de çağrılır)."""
    result_path = _result_path_for_write(job_id, job_date)
    tmp = result_path.parent / f"{result_path.name}.tmp"
    try:
        tmp.write_text(result_text, encoding="utf-8")
        os.replace(tmp, result_path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


async def _poll_and_finalize(rec: JobRecord):
    """Gosom'u poll et, tamamlanınca CSV indir → JSON'a çevir → diske yaz."""
    poll_count = 0
    poll_interval = 1.0

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            while poll_count < MAX_POLLS:
                await asyncio.sleep(poll_interval)
                poll_count += 1

                try:
                    res = await client.get(f"{GOSOM_API_URL}/jobs/{rec.job_id}")
                    data = res.json()
                except Exception as e:
                    log.warning(f"[{rec.job_id[:8]}] Poll #{poll_count} hata: {e}")
                    poll_interval = min(poll_interval + 0.5, 3.0)
                    continue

                status = data.get("status") or data.get("Status")
                log.info(f"[{rec.job_id[:8]}] Poll #{poll_count}: status='{status}'")

                if status in ("ok", "finished", "completed", "success"):
                    # CSV indir
                    try:
                        csv_res = await client.get(
                            f"{GOSOM_API_URL}/jobs/{rec.job_id}/download",
                            timeout=120,
                        )
                    except Exception as e:
                        rec.status = "failed"
                        rec.error = f"CSV indirme hatası: {e}"
                        await _save_job_to_redis(rec)
                        await _save_index(rec.date)
                        log.error(f"[{rec.job_id[:8]}] CSV indirilemedi: {e}")
                        return

                    try:
                        csv_text = csv_res.content.decode("utf-8")
                    except UnicodeDecodeError:
                        csv_text = csv_res.content.decode("utf-8", errors="replace")
                        log.warning(f"[{rec.job_id[:8]}] CSV UTF-8 decode hatası, replace ile devam")

                    if not csv_text.strip():
                        json_data: list[dict] = []
                    else:
                        json_data = _parse_csv(csv_text)
                    result_text = json.dumps(json_data, ensure_ascii=False, indent=2)

                    try:
                        await _save_result_to_redis(rec.job_id, result_text)
                    except Exception as e:
                        rec.status = "failed"
                        rec.error = f"Sonuç Redis'e yazılamadı: {e}"
                        await _save_job_to_redis(rec)
                        await _save_index(rec.date)
                        log.error(f"[{rec.job_id[:8]}] Redis sonuç yazılamadı: {e}")
                        return

                    # Disk backup; yazılamazsa sonuç yine Redis'ten servis edilir
                    try:
                        await asyncio.to_thread(
                            _write_result_sync, rec.job_id, rec.date, result_text
                        )
                    except (OSError, ValueError) as e:
                        log.warning(f"[{rec.job_id[:8]}] Disk backup yazılamadı: {e}")

                    rec.result_count = len(json_data)
                    rec.status = "ok"
                    await _save_job_to_redis(rec)
                    await _save_index(rec.date)
                    log.info(
                        f"[{rec.job_id[:8]}] Tamamlandı: {rec.result_count} kayıt "
                        f"({poll_count} poll)"
                    )
                    return

                elif status in ("failed", "error"):
                    rec.status = "failed"
                    rec.error = data.get("error") or data.get("Error") or str(data)
                    await _save_job_to_redis(rec)
                    await _save_index(rec.date)
                    log.error(f"[{rec.job_id[:8]}] Gosom hata: {rec.error}")
                    return

                # pending / working → devam
                poll_interval = min(poll_interval + 0.5, 3.0)

        # Max poll aşıldı
        rec.status = "failed"
        rec.error = f"Timeout: {MAX_POLLS} poll sonra tamamlanmadı"
        await _save_job_to_redis(rec)
        await _save_index(rec.date)
        log.error(f"[{rec.job_id[:8]}] Polling timeout")

    except asyncio.CancelledError:
        log.info(f"[{rec.job_id[:8]}] Poll task iptal edildi")
    except Exception as e:
        rec.status = "failed"
        rec.error = str(e)
        log.exception(f"[{rec.job_id[:8]}] Beklenmeyen hata: {e}")
        try:
            await _save_job_to_redis(rec)
            await _save_index(rec.date)
        except Exception as save_err:
            log.error(f"[{rec.job_id[:8]}] Redis/Index kaydedilemedi: {save_err}")

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis

    # Startup — Redis bağlantısı
    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await _redis.ping()
        log.info(f"Redis bağlantısı başarılı: {REDIS_URL}")
    except Exception as e:
        log.error(f"Redis bağlantı hatası: {e}")
        raise

    await _load_todays_index()
    await _daily_cleanup()

    yield

    # Shutdown — aktif task'ları cancel
    for task in _active_tasks:
        task.cancel()
    if _active_tasks:
        await asyncio.gather(*_active_tasks, return_exceptions=True)

    # Redis bağlantısını kapat
    if _redis:
        await _redis.aclose()
        log.info("Redis bağlantısı kapatıldı")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Google Maps Custom JSON API", lifespan=lifespan)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _job_payload(rec: JobRecord) -> dict:
    payload = {
        "job_id": rec.job_id,
        "query": rec.query,
        "place_id": rec.place_id,
        "status": rec.status,
        "created_at": rec.created_at,
        "date": rec.date,
        "depth": rec.depth,
        "max_reviews": rec.max_reviews,
        "extra_reviews": rec.extra_reviews,
        "lang": rec.lang,
        "geo": rec.geo,
        "zoom": rec.zoom,
        "radius": rec.radius,
        "email": rec.email,
        "fast_mode": rec.fast_mode,
    }
    if rec.status == "ok":
        payload["result_count"] = rec.result_count
        payload.update(_job_result_urls(rec.job_id))
    elif rec.status == "failed":
        payload["error"] = rec.error
    return payload


async def _list_jobs_data(target_date: str | None = None) -> list[dict]:
    await _daily_cleanup()
    date_key = target_date or date.today().isoformat()
    job_ids = await _redis.smembers(f"jobs:date:{date_key}")
    jobs = []
    for jid in job_ids:
        rec = await _get_job_from_redis(jid)
        if not rec or rec.date != date_key:
            continue
        jobs.append(_job_payload(rec))
    jobs.sort(key=lambda item: item["created_at"], reverse=True)
    return jobs


async def _get_job_detail_data(job_id: str) -> dict:
    try:
        _validate_job_id(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Geçersiz job_id formatı")

    rec = await _get_job_from_redis(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job bulunamadı")
    return _job_payload(rec)


async def _create_job_internal(body: dict) -> tuple[dict, int]:
    query = str(body.get("query", "")).strip()
    try:
        place_id = _normalize_place_id(body.get("place_id"))
        lang = _normalize_lang(body.get("lang"))
        geo = _normalize_geo(body.get("geo"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not query and not place_id:
        raise HTTPException(status_code=400, detail="query veya place_id gerekli")
    if place_id and not query:
        raise HTTPException(
            status_code=400,
            detail="place_id kullanırken ilgili yerin adını query olarak da göndermelisin",
        )

    try:
        depth = int(body.get("depth", 1))
        max_reviews = int(body.get("max_reviews", 10))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="depth ve max_reviews tam sayı olmalı")

    if not (1 <= depth <= 10):
        raise HTTPException(status_code=400, detail="depth 1-10 arasında olmalı")
    if not (0 <= max_reviews <= 500):
        raise HTTPException(status_code=400, detail="max_reviews 0-500 arasında olmalı")

    extra_reviews = _bool_from_input(body.get("extra_reviews", False))
    email = _bool_from_input(body.get("email", False))
    fast_mode = _bool_from_input(body.get("fast_mode", False))

    zoom_raw = body.get("zoom")
    if zoom_raw in (None, ""):
        zoom = None
    else:
        try:
            zoom = int(zoom_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="zoom tam sayı olmalı")
        if not (0 <= zoom <= 21):
            raise HTTPException(status_code=400, detail="zoom 0-21 arasında olmalı")

    radius_raw = body.get("radius")
    if radius_raw in (None, ""):
        radius = None
    else:
        try:
            radius = float(radius_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="radius sayı olmalı")
        if radius <= 0:
            raise HTTPException(status_code=400, detail="radius 0'dan büyük olmalı")

    await _daily_cleanup()

    today = date.today().isoformat()
    effective_query = query or f"place_id:{place_id}"
    gosom_keyword = _build_gosom_keyword(query, place_id)
    h = _cache_key_hash(
        effective_query,
        depth,
        max_reviews,
        extra_reviews=extra_reviews,
        place_id=place_id,
        lang=lang,
        geo=geo,
        zoom=zoom,
        radius=radius,
        email=email,
        fast_mode=fast_mode,
    )

    existing_job_id = await _redis.get(f"qidx:{today}:{h}")
    if existing_job_id:
        existing = await _get_job_from_redis(existing_job_id)
        if existing:
            return _job_payload(existing), 200

    lock_key = f"lock:{h}"
    acquired = await _redis.set(lock_key, "1", nx=True, ex=30)
    if not acquired:
        raise HTTPException(
            status_code=409,
            detail="Bu sorgu için job zaten oluşturuluyor, lütfen biraz bekleyin",
        )

    try:
        payload = {
            "name": f"wrapper_{datetime.now().strftime('%H%M%S')}",
            "keywords": [gosom_keyword],
            "lang": lang,
            "depth": depth,
            "max_reviews": max_reviews,
            "max_time": 3600,
        }
        if extra_reviews:
            payload["extra_reviews"] = True
        if email:
            payload["email"] = True
        if geo:
            payload["geo"] = geo
        if zoom is not None:
            payload["zoom"] = zoom
        if radius is not None:
            payload["radius"] = radius
        if fast_mode:
            payload["fast_mode"] = True

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                res = await client.post(f"{GOSOM_API_URL}/jobs", json=payload)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Gosom'a bağlanılamadı: {e}")

        if res.status_code != 201:
            raise HTTPException(
                status_code=502,
                detail=f"Gosom job oluşturamadı: {res.status_code} - {res.text[:300]}",
            )

        try:
            job_id = res.json().get("id")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Gosom geçersiz yanıt gövdesi: {e}")
        if not job_id:
            raise HTTPException(status_code=502, detail="Gosom job id döndürmedi")

        try:
            _validate_job_id(job_id)
        except ValueError:
            raise HTTPException(status_code=502, detail=f"Gosom geçersiz job_id döndürdü: {job_id}")

        log.info(
            f"Yeni job oluşturuldu: {job_id[:8]} query='{effective_query}' "
            f"place_id='{place_id}' extra_reviews={extra_reviews} "
            f"lang={lang} geo={geo} zoom={zoom} radius={radius} "
            f"email={email} fast_mode={fast_mode}"
        )

        rec = JobRecord(
            job_id=job_id,
            query=effective_query,
            depth=depth,
            max_reviews=max_reviews,
            extra_reviews=extra_reviews,
            place_id=place_id,
            lang=lang,
            geo=geo,
            zoom=zoom,
            radius=radius,
            email=email,
            fast_mode=fast_mode,
            status="pending",
            created_at=datetime.now().isoformat(),
            date=today,
        )

        try:
            await _save_job_to_redis(rec)
            await _redis.set(f"qidx:{today}:{h}", job_id, ex=_REDIS_TTL)
        except Exception as redis_err:
            log.error(f"[{job_id[:8]}] Redis kayıt başarısız (Gosom job orphan kalabilir): {redis_err}")
            raise HTTPException(
                status_code=503,
                detail="Job oluşturuldu ancak kaydedilemedi, lütfen tekrar deneyin",
            )

        await _save_index(rec.date)

        task = asyncio.create_task(_poll_and_finalize(rec))
        _active_tasks.add(task)
        task.add_done_callback(_active_tasks.discard)

        return _job_payload(rec), 201
    finally:
        try:
            await _redis.delete(lock_key)
        except Exception:
            pass


async def _get_dashboard_session_or_redirect(request: Request):
    session_data = await _get_admin_session(request)
    if not session_data:
        return None, _redirect_to_login()
    return session_data, None


def _parse_dashboard_date(raw_value: str | None) -> str:
    value = str(raw_value or "").strip()
    if not value:
        return date.today().isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(status_code=400, detail="Geçersiz tarih formatı")


async def _build_dashboard_page_context(job_date: str, status_filter: str) -> dict:
    jobs = await _list_jobs_data(job_date)
    allowed_filters = {"all", "pending", "ok", "failed"}
    active_filter = status_filter if status_filter in allowed_filters else "all"
    if active_filter != "all":
        jobs = [job for job in jobs if job["status"] == active_filter]

    summary = {
        "total": len(jobs),
        "pending": sum(1 for job in jobs if job["status"] == "pending"),
        "ok": sum(1 for job in jobs if job["status"] == "ok"),
        "failed": sum(1 for job in jobs if job["status"] == "failed"),
    }
    return {
        "jobs": jobs,
        "job_date": job_date,
        "status_filter": active_filter,
        "summary": summary,
    }

# ---------------------------------------------------------------------------
# POST /api/jobs — Job oluştur veya cache hit dön
# ---------------------------------------------------------------------------

@app.post("/api/jobs")
async def create_job(body: dict):
    content, status_code = await _create_job_internal(body)
    return UTF8JSONResponse(content=content, status_code=status_code)

# ---------------------------------------------------------------------------
# GET /api/jobs — Bugünün tüm job'larını listele
# ---------------------------------------------------------------------------

@app.get("/api/jobs")
async def list_jobs():
    return UTF8JSONResponse(content=await _list_jobs_data())

# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id} — Tek job durumu
# ---------------------------------------------------------------------------

@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    return UTF8JSONResponse(content=await _get_job_detail_data(job_id))

# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/result — JSON sonucu indir
# ---------------------------------------------------------------------------

@app.get("/api/jobs/{job_id}/result")
async def get_result(job_id: str):
    try:
        _validate_job_id(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Geçersiz job_id formatı")

    rec = await _get_job_from_redis(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job bulunamadı")

    if rec.status == "pending":
        return UTF8JSONResponse(
            content={"status": "pending", "message": "Job henüz tamamlanmadı"},
            status_code=202,
        )

    if rec.status == "failed":
        raise HTTPException(status_code=422, detail=f"Job başarısız: {rec.error}")

    return await _build_result_response(rec, inline=False)


@app.get("/api/jobs/{job_id}/result/json")
async def get_result_json(job_id: str):
    try:
        _validate_job_id(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Geçersiz job_id formatı")

    rec = await _get_job_from_redis(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job bulunamadı")

    if rec.status == "pending":
        return UTF8JSONResponse(
            content={"status": "pending", "message": "Job henüz tamamlanmadı"},
            status_code=202,
        )

    if rec.status == "failed":
        raise HTTPException(status_code=422, detail=f"Job başarısız: {rec.error}")

    return await _build_result_response(rec, inline=True)


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    _require_dashboard_enabled()
    session_data = await _get_admin_session(request)
    if session_data:
        response = RedirectResponse(url="/dashboard", status_code=303)
        response.headers["Cache-Control"] = "no-store"
        return response
    return _render_template(
        request,
        "login.html",
        {
            "error": None,
            "password_hint": "Dashboard tek şifre ile korunur. Session cookie tarayıcıya HttpOnly olarak yazılır.",
        },
    )


@app.post("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request, password: str = Form(...)):
    _require_dashboard_enabled()
    remote_addr = _remote_addr_from_request(request)
    if await _too_many_login_attempts(remote_addr):
        return _render_template(
            request,
            "login.html",
            {
                "error": "Çok fazla hatalı deneme yapıldı. Biraz sonra tekrar dene.",
                "password_hint": None,
            },
            status_code=429,
        )

    if not _verify_password(password):
        await _record_failed_login(remote_addr)
        return _render_template(
            request,
            "login.html",
            {
                "error": "Şifre hatalı.",
                "password_hint": None,
            },
            status_code=401,
        )

    await _clear_failed_logins(remote_addr)
    session_id, _session = await _create_admin_session()
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    _set_admin_cookie(response, session_id)
    return response


@app.post("/admin/logout")
async def admin_logout(request: Request, csrf_token: str = Form(...)):
    _require_dashboard_enabled()
    session_data, redirect = await _get_dashboard_session_or_redirect(request)
    if redirect:
        return redirect
    session_id, session = session_data
    _validate_csrf(session, csrf_token)
    await _destroy_admin_session(session_id)
    response = _redirect_to_login()
    _clear_admin_cookie(response)
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_home(request: Request, job_date: str | None = None, status: str = "all"):
    _require_dashboard_enabled()
    session_data, redirect = await _get_dashboard_session_or_redirect(request)
    if redirect:
        return redirect
    _session_id, session = session_data
    target_date = _parse_dashboard_date(job_date)
    context = await _build_dashboard_page_context(target_date, status)
    context.update(
        {
            "csrf_token": session["csrf"],
            "error": None,
            "success": request.query_params.get("created"),
            "form_values": {
                "query": "",
                "place_id": "",
                "depth": 1,
                "max_reviews": 10,
                "extra_reviews": False,
                "lang": "tr",
                "geo": "",
                "zoom": "",
                "radius": "",
                "email": False,
                "fast_mode": False,
            },
        }
    )
    return _render_template(request, "dashboard.html", context)


@app.post("/dashboard/jobs", response_class=HTMLResponse)
async def dashboard_create_job(
    request: Request,
    csrf_token: str = Form(...),
    query: str = Form(""),
    place_id: str = Form(""),
    depth: str = Form("1"),
    max_reviews: str = Form("10"),
    extra_reviews: str | None = Form(None),
    lang: str = Form("tr"),
    geo: str = Form(""),
    zoom: str = Form(""),
    radius: str = Form(""),
    email: str | None = Form(None),
    fast_mode: str | None = Form(None),
):
    _require_dashboard_enabled()
    session_data, redirect = await _get_dashboard_session_or_redirect(request)
    if redirect:
        return redirect
    _session_id, session = session_data
    _validate_csrf(session, csrf_token)

    body = {
        "query": query,
        "place_id": place_id,
        "depth": depth,
        "max_reviews": max_reviews,
        "extra_reviews": extra_reviews,
        "lang": lang,
        "geo": geo,
        "zoom": zoom,
        "radius": radius,
        "email": email,
        "fast_mode": fast_mode,
    }

    try:
        payload, status_code = await _create_job_internal(body)
        if status_code in {200, 201}:
            response = RedirectResponse(url=f"/dashboard?created={payload['job_id']}", status_code=303)
            response.headers["Cache-Control"] = "no-store"
            return response
        raise HTTPException(status_code=status_code, detail="Job oluşturulamadı")
    except HTTPException as exc:
        target_date = date.today().isoformat()
        context = await _build_dashboard_page_context(target_date, "all")
        context.update(
            {
                "csrf_token": session["csrf"],
                "error": exc.detail,
                "success": None,
                "form_values": {
                    "query": query,
                    "place_id": place_id,
                    "depth": depth,
                    "max_reviews": max_reviews,
                    "extra_reviews": _bool_from_input(extra_reviews),
                    "lang": lang,
                    "geo": geo,
                    "zoom": zoom,
                    "radius": radius,
                    "email": _bool_from_input(email),
                    "fast_mode": _bool_from_input(fast_mode),
                },
            }
        )
        return _render_template(request, "dashboard.html", context, status_code=exc.status_code)


@app.get("/dashboard/jobs/{job_id}", response_class=HTMLResponse)
async def dashboard_job_detail(request: Request, job_id: str):
    _require_dashboard_enabled()
    session_data, redirect = await _get_dashboard_session_or_redirect(request)
    if redirect:
        return redirect
    _session_id, session = session_data

    try:
        _validate_job_id(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Geçersiz job_id formatı")

    rec = await _get_job_from_redis(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job bulunamadı")

    preview = None
    preview_error = None
    if rec.status == "ok":
        try:
            result_text = await _get_result_content_or_raise(rec)
            preview = json.loads(result_text)[:5]
        except HTTPException as exc:
            preview_error = exc.detail
        except json.JSONDecodeError:
            preview_error = "Sonuç JSON parse edilemedi"

    return _render_template(
        request,
        "job_detail.html",
        {
            "csrf_token": session["csrf"],
            "job": _job_payload(rec),
            "preview": preview,
            "preview_error": preview_error,
        },
    )
