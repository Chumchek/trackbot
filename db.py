from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError
import certifi

from config import MONGO_URI, DB_NAME, APPS_COLLECTION, CHATS_COLLECTION


_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None
_apps: AsyncIOMotorCollection | None = None
_chats: AsyncIOMotorCollection | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _package_from_link(link: str) -> str:
    """Extract package id from Play link."""
    qs = parse_qs(urlparse(link).query)
    pkg_list = qs.get("id")
    if not pkg_list or not pkg_list[0]:
        raise ValueError("Не найден параметр id в ссылке")
    return pkg_list[0].strip()


def link_from_package(package: str) -> str:
    """Build canonical Play link from package."""
    return f"https://play.google.com/store/apps/details?id={package}"


def get_package(raw: str) -> str:
    """Parse user input: link or package. Returns package."""
    raw = raw.strip()
    if "play.google.com" in raw or raw.startswith("http"):
        return _package_from_link(normalize_play_link(raw))
    if not re.fullmatch(r"[a-zA-Z0-9_\.]+", raw):
        raise ValueError("Некорректний package або посилання")
    return raw


def normalize_play_link(raw_link: str) -> str:
    """Normalize a Google Play link. Preserves hl/gl locale if present.

    - id=com.app → id=com.app (no locale, fetch will use en/US)
    - id=com.app&hl=uk&gl=UA → id=com.app&hl=uk&gl=UA (preserved)
    """
    link = raw_link.strip()
    if not link:
        raise ValueError("Пустая ссылка")

    parsed = urlparse(link)
    if parsed.netloc not in {"play.google.com", "www.play.google.com"}:
        raise ValueError("Ссылка должна быть на play.google.com")

    if not parsed.path.startswith("/store/apps/details"):
        raise ValueError("Поддерживаются только ссылки /store/apps/details?id=<package>")

    qs = parse_qs(parsed.query)
    package_name = _package_from_link(link)
    if not re.fullmatch(r"[a-zA-Z0-9_\.]+", package_name):
        raise ValueError("Некорректный package name в параметре id")

    base = f"https://play.google.com/store/apps/details?id={package_name}"
    # Preserve hl and gl if present (locale for consistent metadata comparison)
    hl = (qs.get("hl") or [None])[0]
    gl = (qs.get("gl") or [None])[0]
    if hl or gl:
        parts = [f"hl={hl or 'en'}", f"gl={gl or 'US'}"]
        return f"{base}&{'&'.join(parts)}"
    return base


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
    return _client


def get_db() -> AsyncIOMotorDatabase:
    global _db
    if _db is None:
        _db = get_client()[DB_NAME]
    return _db


def get_apps_collection() -> AsyncIOMotorCollection:
    global _apps
    if _apps is None:
        _apps = get_db()[APPS_COLLECTION]
    return _apps


def get_chats_collection() -> AsyncIOMotorCollection:
    global _chats
    if _chats is None:
        _chats = get_db()[CHATS_COLLECTION]
    return _chats


async def init_db() -> None:
    apps = get_apps_collection()
    # Migrate: add package to docs that don't have it
    async for doc in apps.find({"package": {"$exists": False}}, {"link": 1, "_id": 1}):
        try:
            pkg = _package_from_link(doc["link"])
            await apps.update_one({"_id": doc["_id"]}, {"$set": {"package": pkg}})
        except (ValueError, KeyError):
            pass
    # Dedupe by package: keep one doc per package (prefer link with locale)
    pipeline = [{"$group": {"_id": "$package", "docs": {"$push": {"id": "$_id", "link": "$link"}}, "count": {"$sum": 1}}}, {"$match": {"count": {"$gt": 1}}}]
    async for dup in apps.aggregate(pipeline):
        ids = [d["id"] for d in dup["docs"]]
        links = [d["link"] for d in dup["docs"]]
        keep_id = ids[0]
        for i, lnk in enumerate(links):
            if "hl=" in lnk or "gl=" in lnk:
                keep_id = ids[i]
                break
        for oid in ids:
            if oid != keep_id:
                await apps.delete_one({"_id": oid})
    # Drop old link unique index if exists (we use package now)
    async for idx in apps.list_indexes():
        if idx.get("name") == "link_1":
            await apps.drop_index("link_1")
            break
    # Unique by package (one app per bundle)
    await apps.create_index("package", unique=True)
    await apps.create_index("status")
    await apps.create_index("last_checked_at")

    chats = get_chats_collection()
    await chats.create_index("chat_id", unique=True)
    await chats.create_index("subscribed")


async def add_app(link_or_package: str, trello_app_id: Optional[str] = None) -> Dict[str, Any]:
    """Add app. Accepts link or package. link is stored for fetch (preserves locale)."""
    raw = link_or_package.strip()
    if "play.google.com" in raw or raw.startswith("http"):
        canonical = normalize_play_link(raw)
        package = _package_from_link(canonical)
    else:
        package = get_package(raw)
        canonical = link_from_package(package)
    now = _utcnow()
    doc = {
        "package": package,
        "link": canonical,  # тільки для fetch при порівнянні стану (може містити locale hl/gl)
        "status": "unknown",
        "banned_at": None,
        "renew_at": None,
        "last_checked_at": None,
        "created_at": now,
        "first_time_added": True,  # Флаг для отслеживания первого добавления
        # metadata
        "name": None,
        "version": None,
        "developer": None,
        "icon": None,
        "short_desc": None,
        "long_desc": None,
        "updated_on_text": None,
        "screenshots": [],
        "last_name_change_at": None,
        "last_version_change_at": None,
        "last_developer_change_at": None,
        "last_icon_change_at": None,
        "last_screenshots_change_at": None,
        # histories
        "name_history": [],
        "version_history": [],
        "developer_history": [],
        "icon_history": [],
        "screenshots_history": [],
        "desc_history": [],
        "updated_on_history": [],
        "trello_app_id": (trello_app_id.strip() if trello_app_id else None),
    }
    apps = get_apps_collection()
    try:
        await apps.insert_one(doc)
    except DuplicateKeyError:
        # Already exists - if new link has locale, update stored link
        if "hl=" in canonical or "gl=" in canonical:
            await apps.update_one(
                {"package": package},
                {"$set": {"link": canonical}},
            )
        raise
    return doc


async def update_trello_app_id(link_or_package: str, trello_app_id: Optional[str]) -> None:
    package = get_package(link_or_package)
    apps = get_apps_collection()
    val = trello_app_id.strip() if trello_app_id else None
    await apps.update_one(
        {"package": package},
        {"$set": {"trello_app_id": val}},
    )


async def remove_app(link_or_package: str) -> bool:
    package = get_package(link_or_package)
    apps = get_apps_collection()
    res = await apps.delete_one({"package": package})
    return res.deleted_count > 0


async def list_apps(limit: int = 100) -> List[Dict[str, Any]]:
    apps = get_apps_collection()
    cursor = apps.find({}, sort=[("created_at", -1)], limit=limit)
    return [doc async for doc in cursor]


async def list_apps_page(skip: int, limit: int) -> List[Dict[str, Any]]:
    apps = get_apps_collection()
    cursor = apps.find({}, sort=[("created_at", -1)], skip=skip, limit=limit)
    return [doc async for doc in cursor]


async def count_apps() -> int:
    apps = get_apps_collection()
    return await apps.count_documents({})


async def get_apps_for_check(force_all: bool) -> List[Tuple[str, str]]:
    """Returns [(package, fetch_link), ...]. fetch_link = stored link (with locale) or link_from_package.
    force_all: all apps. False: exclude old banned (NOT first_time_added AND banned_at > 1 month)."""
    apps = get_apps_collection()
    if not force_all:
        cutoff = _utcnow() - timedelta(days=30)
        query = {
            "$or": [
                {"first_time_added": True},
                {"banned_at": None},
                {"banned_at": {"$gte": cutoff}},
            ]
        }
        cursor = apps.find(query, projection={"_id": 0, "package": 1, "link": 1})
    else:
        cursor = apps.find({}, projection={"_id": 0, "package": 1, "link": 1})
    result: List[Tuple[str, str]] = []
    async for doc in cursor:
        pkg = doc["package"]
        fetch_link = doc.get("link") or link_from_package(pkg)
        result.append((pkg, fetch_link))
    return result


async def register_chat(chat_id: int) -> None:
    chats = get_chats_collection()
    now = _utcnow()
    await chats.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )


async def subscribe_chat(chat_id: int) -> None:
    chats = get_chats_collection()
    now = _utcnow()
    await chats.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "subscribed": True, "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )


async def unsubscribe_chat(chat_id: int) -> None:
    chats = get_chats_collection()
    now = _utcnow()
    await chats.update_one(
        {"chat_id": chat_id},
        {"$set": {"subscribed": False, "updated_at": now}},
        upsert=True,
    )


async def get_subscribed_chats() -> List[int]:
    chats = get_chats_collection()
    cursor = chats.find({"subscribed": True}, projection={"_id": 0, "chat_id": 1})
    return [doc["chat_id"] async for doc in cursor]



async def get_all_chats() -> List[int]:
    chats = get_chats_collection()
    cursor = chats.find({}, projection={"_id": 0, "chat_id": 1})
    return [doc["chat_id"] async for doc in cursor]


async def get_app_by_package(package: str) -> Optional[Dict[str, Any]]:
    """Get app document by package."""
    apps = get_apps_collection()
    return await apps.find_one({"package": package})


async def get_app_by_link_or_package(raw: str) -> Optional[Dict[str, Any]]:
    """Get app document by link or package (user input)."""
    package = get_package(raw)
    return await get_app_by_package(package)


async def update_after_metadata_check(
    package: str, available: bool, meta: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
    """Update status + metadata based on check result.

    Returns (updated_doc, changes, details)
    changes can include: "banned", "renewed", "moderation_passed", "name_changed", "version_changed", "desc_changed", 
                        "updated_on_changed", "developer_changed", "icon_changed", "screenshots_changed"
    details contains optional keys: old_name, new_name, old_version, new_version, old_developer, 
                        new_developer, old_icon, new_icon, old_screenshots_count, new_screenshots_count
    """
    apps = get_apps_collection()
    now = _utcnow()

    existing = await apps.find_one({"package": package})
    if existing is None:
        # App was removed during check iteration - don't recreate
        return {}, [], {}
    old_status = existing.get("status")
    old_name = existing.get("name") if existing else None
    old_version = existing.get("version") if existing else None
    old_developer = existing.get("developer") if existing else None
    old_icon = existing.get("icon") if existing else None
    old_screenshots = existing.get("screenshots", []) if existing else []
    old_short = existing.get("short_desc") if existing else None
    old_long = existing.get("long_desc") if existing else None
    old_updated_on = existing.get("updated_on_text") if existing else None
    first_time_added = existing.get("first_time_added", False) if existing else False

    changes: List[str] = []
    details: Dict[str, Any] = {}

    update: Dict[str, Any] = {"last_checked_at": now}
    push_ops: Dict[str, Any] = {}

    # Status update logic with first-time moderation detection
    if available:
        # App is available - clear any banned status
        update["status"] = "available"
        
        # Check if this is first-time moderation passing
        if first_time_added and (old_status == "unavailable" or old_status == "unknown"):
            # This is the first time the app becomes available after being added
            # This means it passed moderation for the first time
            update["renew_at"] = now
            update["first_time_added"] = False  # Mark as no longer first time
            changes.append("moderation_passed")
        elif old_status == "unavailable":
            # App was banned before but now returned
            update["renew_at"] = now
            changes.append("renewed")
            
        # Clear banned timestamp when app becomes available again
        update["banned_at"] = None
    else:
        # App is unavailable - mark as banned
        if old_status != "unavailable":
            update["status"] = "unavailable"
            update["banned_at"] = now
            changes.append("banned")
        # If already unavailable, just update check time

    # Metadata fields
    name = meta.get("name") if meta else None
    version = meta.get("version") if meta else None
    developer = meta.get("developer") if meta else None
    icon = meta.get("icon") if meta else None
    screenshots = meta.get("screenshots", []) if existing else []
    short_desc = meta.get("short_desc") if meta else None
    long_desc = meta.get("long_desc") if meta else None
    updated_on_text = meta.get("updated_on_text") if meta else None

    if name and name != old_name:
        update["name"] = name
        update["last_name_change_at"] = now
        if old_name is not None:
            changes.append("name_changed")
            details["old_name"] = old_name
            details["new_name"] = name
            push_ops.setdefault("name_history", {})["$each"] = push_ops.get("name_history", {}).get("$each", []) + [
                {"at": now, "old": old_name, "new": name}
            ]

    if version and version != old_version:
        update["version"] = version
        update["last_version_change_at"] = now
        changes.append("version_changed")
        details["old_version"] = old_version
        details["new_version"] = version
        push_ops.setdefault("version_history", {})["$each"] = push_ops.get("version_history", {}).get("$each", []) + [
            {"at": now, "old": old_version, "new": version}
        ]

    if short_desc and short_desc != old_short:
        update["short_desc"] = short_desc
        # Only report description changes if app remains available and was available before
        if available and (old_status == "available" or old_short is not None):
            changes.append("desc_changed")
        push_ops.setdefault("desc_history", {})["$each"] = push_ops.get("desc_history", {}).get("$each", []) + [
            {"at": now, "type": "short", "old": old_short, "new": short_desc}
        ]

    if long_desc and long_desc != old_long:
        update["long_desc"] = long_desc
        # Only report description changes if app remains available and was available before
        if available and (old_status == "available" or old_long is not None):
            changes.append("desc_changed")
        push_ops.setdefault("desc_history", {})["$each"] = push_ops.get("desc_history", {}).get("$each", []) + [
            {"at": now, "type": "long", "old": old_long, "new": long_desc}
        ]

    if updated_on_text and updated_on_text != old_updated_on:
        update["updated_on_text"] = updated_on_text
        changes.append("updated_on_changed")
        push_ops.setdefault("updated_on_history", {})["$each"] = push_ops.get("updated_on_history", {}).get("$each", []) + [
            {"at": now, "old": old_updated_on, "new": updated_on_text}
        ]

    if developer and developer != old_developer:
        update["developer"] = developer
        update["last_developer_change_at"] = now
        if old_developer is not None:
            changes.append("developer_changed")
            details["old_developer"] = old_developer
            details["new_developer"] = developer
            push_ops.setdefault("developer_history", {})["$each"] = push_ops.get("developer_history", {}).get("$each", []) + [
                {"at": now, "old": old_developer, "new": developer}
            ]

    if icon and icon != old_icon:
        update["icon"] = icon
        update["last_icon_change_at"] = now
        # Only report icon changes if app remains available and was available before or had an icon
        if available and (old_status == "available" or old_icon is not None) and old_icon is not None:
            changes.append("icon_changed")
            details["old_icon"] = old_icon
            details["new_icon"] = icon
        push_ops.setdefault("icon_history", {})["$each"] = push_ops.get("icon_history", {}).get("$each", []) + [
            {"at": now, "old": old_icon, "new": icon}
        ]

    # Compare screenshots lists - but only if app was previously available
    # Don't report screenshot changes when app goes from unavailable to available
    if screenshots != old_screenshots:
        update["screenshots"] = screenshots
        update["last_screenshots_change_at"] = now
        
        # Only report screenshot changes if:
        # 1. App remains available (not going to banned state)
        # 2. AND either app was available before OR both old and new have screenshots
        should_report_change = (
            available and  # App is still available (not being banned)
            (old_status == "available" or (len(old_screenshots) > 0 and len(screenshots) > 0))
        )
        
        if should_report_change:
            changes.append("screenshots_changed")
            details["old_screenshots_count"] = len(old_screenshots)
            details["new_screenshots_count"] = len(screenshots)
            
        # Always log to history for debugging
        push_ops.setdefault("screenshots_history", {})["$each"] = push_ops.get("screenshots_history", {}).get("$each", []) + [
            {"at": now, "old_count": len(old_screenshots), "new_count": len(screenshots), "old": old_screenshots, "new": screenshots, "reported": should_report_change}
        ]

    # Set other fields if they have values (for first time or updates)
    if developer:
        update["developer"] = developer
    if icon:
        update["icon"] = icon
    if screenshots:
        update["screenshots"] = screenshots

    update_doc: Dict[str, Any] = {"$set": update, "$setOnInsert": {"created_at": now}}
    if push_ops:
        # transform push_ops to $push with $each arrays
        pushes: Dict[str, Any] = {}
        for field, spec in push_ops.items():
            pushes[field] = spec
        update_doc["$push"] = pushes

    update_doc["$setOnInsert"] = update_doc.get("$setOnInsert", {})
    update_doc["$setOnInsert"]["package"] = package
    update_doc["$setOnInsert"]["link"] = existing["link"] if existing else link_from_package(package)

    await apps.update_one(
        {"package": package},
        update_doc,
        upsert=True,
    )

    doc = await apps.find_one({"package": package})
    assert doc is not None
    return doc, changes, details
