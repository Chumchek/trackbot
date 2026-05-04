"""Sync app status changes to CRM (accounts + apps)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import certifi
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase

import config

logger = logging.getLogger(__name__)

_crm_client: AsyncIOMotorClient | None = None
_crm_db: AsyncIOMotorDatabase | None = None


def _bundle_id_from_link(link: str) -> str | None:
    try:
        qs = parse_qs(urlparse(link).query)
        ids = qs.get("id") or []
        return ids[0].strip() if ids and ids[0] else None
    except Exception:
        return None


def _get_crm_db() -> AsyncIOMotorDatabase | None:
    global _crm_client, _crm_db
    if not config.CRM_DB_MONGO_URI or not config.CRM_DB_NAME:
        return None
    if _crm_db is None:
        _crm_client = AsyncIOMotorClient(
            config.CRM_DB_MONGO_URI,
            tlsCAFile=certifi.where(),
        )
        _crm_db = _crm_client[config.CRM_DB_NAME]
    return _crm_db


def _accounts_collection() -> AsyncIOMotorCollection | None:
    db = _get_crm_db()
    if db is None:
        return None
    return db[config.CRM_ACCOUNTS_COLLECTION]


def _apps_collection() -> AsyncIOMotorCollection | None:
    db = _get_crm_db()
    if db is None:
        return None
    return db[config.CRM_APPS_COLLECTION]


async def _get_crm_account_names() -> List[str]:
    """Повертає список name з усіх акаунтів CRM."""
    coll = _accounts_collection()
    if coll is None:
        return []
    cursor = coll.find({}, projection={"name": 1, "_id": 0})
    names: List[str] = []
    async for doc in cursor:
        n = doc.get("name")
        if n and isinstance(n, str) and n.strip():
            names.append(n.strip())
    return names


async def _update_account_status(developer: str, available: bool) -> None:
    """Оновлює status акаунта: in_store якщо available, dead якщо ні."""
    coll = _accounts_collection()
    if coll is None:
        return
    new_status = "in_store" if available else "dead"

    account = await coll.find_one({"name": developer})
    if account is None:
        return

    old_status = account.get("status")
    status_history_entry = {
        "fromStatus": old_status,
        "toStatus": new_status,
        "date": datetime.now(timezone.utc)
    }

    res = await coll.update_one(
        {"name": developer},
        {
            "$set": {"status": new_status, "updatedAt": datetime.now(timezone.utc)},
            "$push": {"statusHistory": status_history_entry}
        },
    )
    if res.modified_count:
        logger.info("CRM: оновлено акаунт (name=%s) → status=%s", developer, new_status)


async def _find_crm_app_by_bundle(bundle_id: str) -> Optional[Dict[str, Any]]:
    """Шукає додаток в CRM_APPS за bundle_id (з поля link)."""
    coll = _apps_collection()
    if coll is None:
        return None
    # link format: https://play.google.com/store/apps/details?id=com.bundle.com
    pattern = "id=" + re.escape(bundle_id) + r"($|&)"
    return await coll.find_one({"link": {"$regex": pattern}})


async def _update_crm_app(
    bundle_id: str,
    link: str,
    available: bool,
    app_name: Optional[str],
    custom_id: Optional[str],
) -> None:
    """Оновлює існуючий додаток в CRM: status, custom_id, name, type."""
    coll = _apps_collection()
    if coll is None:
        return
    existing = await _find_crm_app_by_bundle(bundle_id)
    if not existing:
        return

    new_status = "in_store" if available else "unavailable"
    update: Dict[str, Any] = {
        "status": new_status,
        "type": config.CRM_TYPE_APPS,
        "updatedAt": datetime.now(timezone.utc),
    }
    if app_name:
        update["name"] = app_name
    if custom_id:
        update["custom_id"] = custom_id

    await coll.update_one(
        {"_id": existing["_id"]},
        {"$set": update},
    )
    logger.info("CRM: оновлено app bundle=%s → status=%s", bundle_id, new_status)


async def _create_crm_app(
    link: str,
    bundle_id: str,
    available: bool,
    app_name: Optional[str],
    custom_id: Optional[str],
    developer: Optional[str] = None
) -> None:
    """Створює новий додаток в CRM."""
    apps_coll = _apps_collection()
    accounts_coll = _accounts_collection()
    if apps_coll is None or accounts_coll is None:
        return

    # status: moderation якщо ще ні разу не був available, інакше in_store
    status = "in_store" if available else "moderation"
    now = datetime.now(timezone.utc)
    doc: Dict[str, Any] = {
        "link": link,
        "custom_id": custom_id or "",
        "name": app_name or bundle_id,
        "status": status,
        "type": config.CRM_TYPE_APPS,
        "createdAt": now,
        "updatedAt": now,
        "__v": 0,
    }
    result = await apps_coll.insert_one(doc)
    app_id = result.inserted_id
    if developer:
        await accounts_coll.update_one(
            {"name": developer},
            {"$push": {"apps": app_id}}
        )
    logger.info("CRM: створено app bundle=%s status=%s developer=%s", bundle_id, status, developer)


def _link_from_package(package: str) -> str:
    return f"https://play.google.com/store/apps/details?id={package}"


async def sync_app_status_to_crm(
    package: str,
    developer: Optional[str],
    available: bool,
    app_name: Optional[str],
    custom_id: Optional[str],
) -> None:
    """
    Синхронізує зміну статусу додатку в CRM тільки якщо developer входить
    в список name з CRM_ACCOUNTS_COLLECTION:
    - оновлює status акаунта (in_store/dead)
    - оновлює або створює додаток в CRM_APPS за bundle_id (package)
    """
    if _get_crm_db() is None:
        logger.info("CRM: не налаштовано, пропуск")
        return

    if not developer or not developer.strip():
        logger.info("CRM: немає developer, пропуск")
        return

    account_names = await _get_crm_account_names()
    if developer.strip() not in account_names:
        logger.info("CRM: developer %s не в списку акаунтів, пропуск", developer)
        return

    if not package:
        logger.warning("CRM: порожній package")
        return
    # await _update_account_status(developer.strip(), available)
    link = _link_from_package(package)
    existing = await _find_crm_app_by_bundle(package)
    if existing:
        await _update_crm_app(package, link, available, app_name, custom_id)
    else:
        await _create_crm_app(link, package, available, app_name, custom_id, developer)
