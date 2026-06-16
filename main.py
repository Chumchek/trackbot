from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import timezone, datetime
from typing import List, Tuple, Dict, Optional

from logger import setup_logging

setup_logging()

from pymongo.errors import DuplicateKeyError
from telegram import ReplyKeyboardMarkup, Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    TypeHandler,
    filters,
)
from telegram.ext import ApplicationHandlerStop

import config
import db
from crm_sync import sync_app_status_to_crm
from checker import fetch_app_metadata
from utils.trello_api import TrelloAPI

logger = logging.getLogger(__name__)

_trello_api: TrelloAPI | None = None


def get_trello_api() -> TrelloAPI | None:
    global _trello_api
    if _trello_api is not None:
        return _trello_api
    if not all([
        config.TRELLO_API_KEY,
        config.TRELLO_API_SECRET,
        config.TRELLO_TOKEN,
        config.TRELLO_PROCESSING_BOARD_ID,
        config.TRELLO_BANNED_LIST_ID,
        config.TRELLO_IN_MARKET_LIST_ID,
    ]):
        return None
    _trello_api = TrelloAPI(
        api_key=config.TRELLO_API_KEY,
        api_secret=config.TRELLO_API_SECRET,
        token=config.TRELLO_TOKEN,
    )
    logger.info("Trello: клієнт ініціалізовано")
    return _trello_api


async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not config.ALLOWED_USER_IDS:
        return
    user = update.effective_user
    if user is None:
        raise ApplicationHandlerStop
    if user.id in config.ALLOWED_USER_IDS:
        return
    if await db.is_user_allowed_in_db(user.id):
        return
    if update.message:
        await update.message.reply_text("⛔ Access denied.")
    elif update.callback_query:
        await update.callback_query.answer("⛔ Access denied.", show_alert=True)
    raise ApplicationHandlerStop


def _is_env_admin(user_id: int) -> bool:
    return user_id in config.ALLOWED_USER_IDS


BUTTON_CHECK_NOW = "Проверить приложения"
PAGE_SIZE = 10
NAV_KEY = "list_nav_msg_id"
LIST_IDS_KEY = "list_msg_ids"
PAGE_KEY = "list_page"
TOTAL_PAGES_KEY = "list_total_pages"


def build_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BUTTON_CHECK_NOW]], resize_keyboard=True)


async def _register_chat_from_update(update: Update) -> None:
    chat = update.effective_chat
    if chat:
        try:
            await db.register_chat(chat.id)
        except Exception as e:  # noqa: BLE001
            logger.debug("register_chat failed for %s: %s", chat.id, e)


def _parse_add_args(args: List[str]) -> Tuple[str, Optional[str]]:
    """Перший аргумент — link або package; решта — trello_app_id (наприклад id для CRM)."""
    link = args[0]
    manual = None
    if len(args) > 1:
        manual = " ".join(args[1:]).strip()
    return link, (manual if manual else None)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_chat_from_update(update)
    await update.message.reply_text(
        "Привет! Я слежу за доступностью приложений Google Play.\n"
        "Команды: /help",
        reply_markup=build_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_chat_from_update(update)
    await update.message.reply_text(
        "Команды:\n"
        "/start — приветствие и клавиатура\n"
        "/help — список команд\n"
        "/add <link або package> — добавить приложение\n"
        "/remove <link або package> — удалить приложение\n"
        "/list — список приложений с пагинацией\n"
        "/subscribe — подписка на уведомления\n"
        "/unsubscribe — отписка от уведомлений\n"
        "/check — проверить все приложения сейчас\n"
        "/debug <link або package> — отладка статуса приложения\n"
        "/test_app <link або package> — тест приложения (проверка текущего статуса)\n"
        "/fix_app <link або package> — исправить приложение, если оно было неправильно помечено как заблокированное\n\n"
        "Кнопка в клавиатуре: 'Проверить приложения' запускает ручную проверку."
    )


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_chat_from_update(update)
    chat_id = update.effective_chat.id
    await db.subscribe_chat(chat_id)
    await update.message.reply_text(
        "🔔 Вы подписаны на уведомления об изменениях!\n\n"
        "📋 Как работает подписка:\n"
        "• Бот автоматически проверяет ВСЕ приложения каждые 20 минут\n"
        "• Проверка происходит независимо от подписок\n"
        "• Подписчики получают уведомления о любых изменениях:\n"
        "  - Новые версии\n"
        "  - Изменения описания\n"
        "  - Обновления скриншотов\n"
        "  - Смена разработчика\n"
        "  - Блокировка/разблокировка приложений\n\n"
        "🔕 Чтобы отписаться: /unsubscribe"
    )


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_chat_from_update(update)
    chat_id = update.effective_chat.id
    await db.unsubscribe_chat(chat_id)
    await update.message.reply_text("Вы отписаны от уведомлений.")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_chat_from_update(update)
    if not context.args:
        await update.message.reply_text("Использование: /add <link або package> [trello app id]")
        return
    link, trello_app_id_arg = _parse_add_args(context.args)
    try:
        doc = await db.add_app(link, trello_app_id=trello_app_id_arg)
    except ValueError as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return
    except DuplicateKeyError:
        try:
            existing_app = await db.get_app_by_link_or_package(link)
            app_name = existing_app.get("name", "Неизвестное приложение") if existing_app else "Неизвестное приложение"
            pkg = existing_app.get("package", "")
            if trello_app_id_arg:
                await db.update_trello_app_id(link, trello_app_id_arg)
                await update.message.reply_text(
                    f"⚠️ Додаток уже в базі.\n📱 {app_name}\n📦 {pkg}\n\n"
                    f"Оновлено Trello app id: {trello_app_id_arg}"
                )
                return
            msg = f"⚠️ Приложение уже в базе данных!\n\n"
            msg += f"📱 Название: {app_name}\n"
            msg += f"📦 Package: {pkg}\n"
            msg += f"\n💡 Приложение автоматически отслеживается каждые 20 минут"
            await update.message.reply_text(msg)
        except Exception:
            await update.message.reply_text("⚠️ Это приложение уже добавлено в базу данных.")
        return

    msg = f"Добавлено: {doc['package']}\n{doc.get('link') or db.link_from_package(doc['package'])}"
    if trello_app_id_arg:
        msg += f"\nTrello app id: {trello_app_id_arg}"
    await update.message.reply_text(msg)
    
    # Немедленно проверяем статус добавленного приложения (link зберігає локаль для порівняння)
    await update.message.reply_text("🔍 Проверяю статус приложения...")
    try:
        pkg = doc["package"]
        fetch_link = doc.get("link") or db.link_from_package(pkg)
        available, meta = await fetch_app_metadata(fetch_link)
        doc_updated, changes, details = await db.update_after_metadata_check(pkg, available, meta)

        if changes:
            app_message = _format_single_app_event(doc_updated, changes, details)
            if app_message:
                await update.message.reply_text(app_message)
            status_changed = any(c in changes for c in ("banned", "renewed", "moderation_passed"))
            if status_changed:
                try:
                    app_id = _resolve_app_id_for_doc(doc_updated, pkg)
                    await sync_app_status_to_crm(
                        package=pkg,
                        developer=doc_updated.get("developer"),
                        available=available,
                        app_name=doc_updated.get("name") or meta.get("name"),
                        custom_id=app_id,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("CRM: помилка синхронізації для %s: %s", pkg, e)
        else:
            # Просто сообщаем статус
            status = "доступно" if available else "недоступно"
            await update.message.reply_text(f"Статус приложения: {status}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при проверке приложения: {e}")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_chat_from_update(update)
    if not context.args:
        await update.message.reply_text("Использование: /remove <ссылка на Google Play>")
        return
    link = " ".join(context.args)
    try:
        removed = await db.remove_app(link)
    except ValueError as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return

    if removed:
        await update.message.reply_text("Удалено")
    else:
        await update.message.reply_text("Не найдено")


def _status_emoji(status: str) -> str:
    return {
        "available": "🟢",
        "unavailable": "🔴",
        "unknown": "⚪️",
    }.get(status, "⚪️")


async def _delete_previous_list_messages(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    ids: List[int] = context.chat_data.get(LIST_IDS_KEY) or []
    if not ids:
        return
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
    context.chat_data[LIST_IDS_KEY] = []


def _nav_markup(page: int, total_pages: int) -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    if page > 0:
        row.append(InlineKeyboardButton("⟵ Пред", callback_data=f"list:{page-1}"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("След ⟶", callback_data=f"list:{page+1}"))
    if not row:
        row.append(InlineKeyboardButton("—", callback_data="noop"))
    buttons.append(row)
    return InlineKeyboardMarkup(buttons)


async def _send_list_page(chat_id: int, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    total = await db.count_apps()
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    await _delete_previous_list_messages(chat_id, context)

    items = await db.list_apps_page(skip=page * PAGE_SIZE, limit=PAGE_SIZE)
    sent_ids: List[int] = []
    for d in items:
        status = d.get("status", "unknown")
        pkg = d.get("package", "")
        link = d.get("link") or db.link_from_package(pkg)
        text = f"{_status_emoji(status)} {status} — {pkg}\n{link}"
        m = await context.bot.send_message(chat_id=chat_id, text=text)
        sent_ids.append(m.message_id)

    context.chat_data[LIST_IDS_KEY] = sent_ids
    context.chat_data[PAGE_KEY] = page
    context.chat_data[TOTAL_PAGES_KEY] = total_pages

    nav_text = f"Страница {page + 1}/{total_pages}"
    nav_id = context.chat_data.get(NAV_KEY)
    if nav_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=nav_id,
                text=nav_text,
                reply_markup=_nav_markup(page, total_pages),
            )
            return
        except Exception:
            pass
    nav_msg = await context.bot.send_message(chat_id=chat_id, text=nav_text, reply_markup=_nav_markup(page, total_pages))
    context.chat_data[NAV_KEY] = nav_msg.message_id


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_chat_from_update(update)
    total = await db.count_apps()
    if total == 0:
        await update.message.reply_text("Список пуст")
        return
    chat_id = update.effective_chat.id
    await _send_list_page(chat_id, context, page=0)


async def on_list_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_chat_from_update(update)
    if not update.callback_query or not update.callback_query.data:
        return
    data = update.callback_query.data
    if not data.startswith("list:"):
        return
    await update.callback_query.answer()
    try:
        page = int(data.split(":", 1)[1])
    except Exception:
        return
    chat_id = update.effective_chat.id
    await _send_list_page(chat_id, context, page)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_chat_from_update(update)
    if update.message and update.message.text == BUTTON_CHECK_NOW:
        await cmd_check(update, context)


async def _fmt_dt(dt: Optional[object]) -> str:
    try:
        if not dt:
            return "-"
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(dt)


def _get_app_id_from_trello(package: str) -> str | None:
    api = get_trello_api()
    if not api or not package:
        return None
    board = api.get_board(config.TRELLO_PROCESSING_BOARD_ID)
    if not board:
        return None
    _, app_id = api.get_card_and_app_id_by_bundle(board, package)
    return app_id


def _trello_app_id_from_doc(doc: Dict | None) -> str | None:
    """Ручний id: trello_app_id (нові записи)"""
    if not doc:
        return None
    v = doc.get("trello_app_id")
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _resolve_app_id_for_doc(doc: Dict, package: str) -> str | None:
    """PARSE_APP_METHOD: 0=немає, 1=Trello, 2=ручний, 3=Trello>ручний, 4=ручний>Trello."""
    m = config.PARSE_APP_METHOD
    if m == 0:
        return None
    manual = _trello_app_id_from_doc(doc)
    trello = None
    if m in (1, 3, 4):
        trello = _get_app_id_from_trello(package)
    if m == 1:
        return trello
    if m == 2:
        return manual
    if m == 3:
        return trello or manual
    if m == 4:
        return manual or trello
    return trello


def _format_single_app_event(doc: Dict, changes: List[str], details: Dict[str, any]) -> str:
    """Format notification message for a single app."""
    pkg = doc.get("package", "")
    link = doc.get("link") or db.link_from_package(pkg)
    name = doc.get("name") or pkg
    developer = doc.get("developer") or "-"
    # dev_team = doc.get("dev_team")  # закоментовано: не показуємо Trello ID у повідомленнях
    app_id = _resolve_app_id_for_doc(doc, pkg)
    header_line = f"📱 [{app_id}] {name} ({pkg})" if app_id else f"📱 {name} ({pkg})"

    # Специальный формат для прохождения модерации
    if "moderation_passed" in changes:
        block: List[str] = []
        block.append(header_line)
        block.append(link)
        block.append("")  # Empty line for spacing
        block.append(f"{pkg} {name} прошло модерацию!")
        block.append(f"Аккаунт: {developer}")
        # if dev_team:
        #     block.append(f"Trello ID: {dev_team}")
        return "\n".join(block)
    
    # Стандартный формат для остальных случаев
    # Start with header containing app name and package
    block = []
    block.append(header_line)
    block.append(link)
    block.append("")  # Empty line for spacing
    
    # Group changes by type
    version_changes = []
    content_changes = []
    status_changes = []
    
    if "name_changed" in changes:
        old_name = details.get("old_name") or ""
        new_name = details.get("new_name") or name
        content_changes.append(f"🏷️ Название: {old_name} → {new_name}")
        
    if "moderation_passed" in changes:
        status_changes.append(f"✅ {name} прошло модерацию!")
        
    if "renewed" in changes:
        status_changes.append(f"✅ {name} вернулась в стор!")
        
    if "banned" in changes:
        status_changes.append(f"❌ {name} забанено!")

    if "version_changed" in changes:
        old_v = details.get("old_version")
        new_v = details.get("new_version")
        if old_v:
            version_changes.append(f"🔢 Версия: {old_v} → {new_v}")
        else:
            version_changes.append(f"🆕 Первая версия: {new_v}")
            
    if "developer_changed" in changes:
        old_dev = details.get("old_developer") or ""
        new_dev = details.get("new_developer") or ""
        content_changes.append(f"👤 Разработчик: {old_dev} → {new_dev}")
        
    if "desc_changed" in changes:
        content_changes.append("📝 Обновлено описание приложения")
        
    if "icon_changed" in changes:
        content_changes.append("🖼️ Обновлена иконка приложения")
        
    if "screenshots_changed" in changes:
        old_count = details.get("old_screenshots_count", 0)
        new_count = details.get("new_screenshots_count", 0)
        content_changes.append(f"📸 Скриншоты: {old_count} → {new_count}")
        
    if "updated_on_changed" in changes:
        new_u = doc.get("updated_on_text")
        if new_u:
            content_changes.append(f"📅 Обновлено: {new_u}")
    
    # Add changes to block
    if version_changes:
        block.extend(version_changes)
    if content_changes:
        block.extend(content_changes)
    if status_changes:
        block.extend(status_changes)
        
    # Add footer with developer info
    block.append("")  # Empty line for spacing
    block.append(f"👨‍💻 Аккаунт: {developer}")
    # if dev_team:
    #     block.append(f"🏢 Trello ID: {dev_team}")
    return "\n".join(block)


def _format_events(events: List[Tuple[Dict, List[str], Dict[str, any]]]) -> str:
    if not events:
        return ""
    lines: List[str] = []
    for doc, changes, details in events:
        pkg = doc.get("package", "")
        link = doc.get("link") or db.link_from_package(pkg)
        name = doc.get("name") or pkg
        developer = doc.get("developer") or "-"
        # dev_team = doc.get("dev_team")  # закоментовано
        app_id = _resolve_app_id_for_doc(doc, pkg)
        header_line = f"📱 [{app_id}] {name} ({pkg})" if app_id else f"📱 {name} ({pkg})"

        # Start with header containing app name and package
        block = []
        block.append(header_line)
        block.append(link)
        block.append("")  # Empty line for spacing
        
        # Group changes by type
        version_changes = []
        content_changes = []
        status_changes = []

        
        if "name_changed" in changes:
            old_name = details.get("old_name") or ""
            new_name = details.get("new_name") or name
            content_changes.append(f"🏷️ Название: {old_name} → {new_name}")
        
        if "moderation_passed" in changes:
            status_changes.append(f"✅ {name} прошло модерацию!")
            
        if "renewed" in changes:
            status_changes.append(f"✅ {name} вернулась в стор!")
            
        if "banned" in changes:
            status_changes.append(f"❌ {name} забанено!")

        if "version_changed" in changes:
            old_v = details.get("old_version")
            new_v = details.get("new_version")
            if old_v:
                version_changes.append(f"🔢 Версия: {old_v} → {new_v}")
            else:
                version_changes.append(f"🆕 Первая версия: {new_v}")
                
        if "developer_changed" in changes:
            old_dev = details.get("old_developer") or ""
            new_dev = details.get("new_developer") or ""
            content_changes.append(f"👤 Разработчик: {old_dev} → {new_dev}")
            
        if "desc_changed" in changes:
            content_changes.append("📝 Обновлено описание приложения")
            
        if "icon_changed" in changes:
            content_changes.append("🖼️ Обновлена иконка приложения")
            
        if "screenshots_changed" in changes:
            old_count = details.get("old_screenshots_count", 0)
            new_count = details.get("new_screenshots_count", 0)
            content_changes.append(f"📸 Скриншоты: {old_count} → {new_count}")
            
        if "updated_on_changed" in changes:
            new_u = doc.get("updated_on_text")
            if new_u:
                content_changes.append(f"📅 Обновлено: {new_u}")
                
        
        # Add changes to block
        if version_changes:
            block.extend(version_changes)
        if content_changes:
            block.extend(content_changes)
        if status_changes:
            block.extend(status_changes)
            
        # Add footer with developer info
        block.append("")  # Empty line for spacing
        block.append(f"👨‍💻 Аккаунт: {developer}")
        # if dev_team:
        #     block.append(f"🏢 Trello ID: {dev_team}")
        lines.append("\n".join(block))
    return "\n\n".join(lines)


def _sync_trello_move_by_status(package: str, available: bool) -> None:
    """Синхронно: знайти картку за bundle_id і перемістити в BANNED або IN_MARKET список (використовує спільний TrelloAPI)."""
    api = get_trello_api()
    if api is None or not package:
        logger.debug("Trello: не налаштовано, пропуск переміщення картки для %s", package)
        return
    api.move_app_card_by_status(
        board_id=config.TRELLO_PROCESSING_BOARD_ID,
        bundle_id=package,
        available=available,
        banned_list_id=config.TRELLO_BANNED_LIST_ID,
        in_market_list_id=config.TRELLO_IN_MARKET_LIST_ID,
    )


async def run_check_all_apps(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    force_all: bool = False,
) -> Tuple[int, List[Tuple[Dict, List[str], Dict[str, any]]]]:
    """Check apps sequentially with 2-second delays. Returns (processed_count, change_events).
    force_all=True: check all apps (manual /check). force_all=False: skip old banned (periodic job)."""
    apps_to_check = await db.get_apps_for_check(force_all)
    if not apps_to_check:
        return 0, []

    events: List[Tuple[Dict, List[str], Dict[str, any]]] = []

    for i, (package, fetch_link) in enumerate(apps_to_check):
        try:
            if await db.get_app_by_package(package) is None:
                continue
            logger.info("Checking app %d/%d: %s", i + 1, len(apps_to_check), package)

            t_iter = time.perf_counter()
            available, meta = await fetch_app_metadata(fetch_link)
            t_after_fetch = time.perf_counter()
            doc, changes, details = await db.update_after_metadata_check(package, available, meta)
            t_after_db = time.perf_counter()
            if config.CHECK_VERBOSE_LOGS:
                logger.info(
                    "[check] %s summary: fetch=%.3fs db_update=%.3fs step_total=%.3fs",
                    package,
                    t_after_fetch - t_iter,
                    t_after_db - t_after_fetch,
                    t_after_db - t_iter,
                )

            if changes:
                events.append((doc, changes, details))
                logger.info("App %s: Status changed to %s", package, "available" if available else "unavailable")

                status_changed = any(c in changes for c in ("banned", "renewed", "moderation_passed"))
                if status_changed:
                    try:
                        await asyncio.to_thread(_sync_trello_move_by_status, package, available)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Trello: помилка під час переміщення картки для %s: %s", package, e)

                    try:
                        app_id = _resolve_app_id_for_doc(doc, package)
                        await sync_app_status_to_crm(
                            package=package,
                            developer=doc.get("developer"),
                            available=available,
                            app_name=doc.get("name") or meta.get("name"),
                            custom_id=app_id,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning("CRM: помилка синхронізації для %s: %s", package, e)

            if i < len(apps_to_check) - 1:
                await asyncio.sleep(2)

        except Exception as e:  # noqa: BLE001
            logger.warning("Error checking %s: %s", package, e)

    return len(apps_to_check), events


async def _broadcast_to_subscribed_chats(context: ContextTypes.DEFAULT_TYPE, message: str) -> None:
    chats = await db.get_subscribed_chats()
    for chat_id in chats:
        try:
            await context.application.bot.send_message(chat_id=chat_id, text=message)
        except Exception as e:  # noqa: BLE001
            logger.debug("broadcast failed to %s: %s", chat_id, e)


async def job_check_all_apps(context: ContextTypes.DEFAULT_TYPE) -> None:
    count, events = await run_check_all_apps(context, force_all=False)
    logger.info("Periodic check finished for %s apps", count)

    if not events:
        return
    
    # Send separate message for each app change
    for doc, changes, details in events:
        app_message = _format_single_app_event(doc, changes, details)
        if app_message:
            await _broadcast_to_subscribed_chats(context, app_message)
            # Small delay between messages to avoid rate limiting
            if len(events) > 1:
                await asyncio.sleep(0.5)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_chat_from_update(update)
    await update.message.reply_text("Запускаю проверку...")
    n, events = await run_check_all_apps(context, force_all=True)
    
    if events:
        # Send separate message for each app change
        for doc, changes, details in events:
            app_message = _format_single_app_event(doc, changes, details)
            if app_message:
                await update.message.reply_text(app_message)
                # Small delay between messages to avoid rate limiting
                if len(events) > 1:
                    await asyncio.sleep(0.5)
    else:
        await update.message.reply_text("Изменений нет")
    
    await update.message.reply_text(f"Проверено: {n}")


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Debug command to check app status."""
    await _register_chat_from_update(update)
    if not context.args:
        await update.message.reply_text("Использование: /debug <ссылка на Google Play>")
        return
    
    raw = " ".join(context.args)
    try:
        app_doc = await db.get_app_by_link_or_package(raw)
        if not app_doc:
            await update.message.reply_text("Приложение не найдено в базе")
            return
        
        status = app_doc.get("status", "unknown")
        name = app_doc.get("name", "N/A")
        last_checked = app_doc.get("last_checked_at")
        banned_at = app_doc.get("banned_at")
        renew_at = app_doc.get("renew_at")
        
        debug_text = f"🔍 Отладка приложения:\n"
        debug_text += f"Название: {name}\n"
        debug_text += f"Статус: {_status_emoji(status)} {status}\n"
        debug_text += f"Последняя проверка: {await _fmt_dt(last_checked)}\n"
        
        if banned_at:
            debug_text += f"Забанено: {await _fmt_dt(banned_at)}\n"
        if renew_at:
            debug_text += f"Восстановлено: {await _fmt_dt(renew_at)}\n"
            
        rid = _resolve_app_id_for_doc(app_doc, app_doc["package"])
        debug_text += f"\nPackage: {app_doc['package']}\nСсылка (для перевірки): {app_doc.get('link') or db.link_from_package(app_doc['package'])}"
        debug_text += f"\nTrello app_id: {rid or '—'}\n"
        await update.message.reply_text(debug_text)
        
    except ValueError as e:
        await update.message.reply_text(f"Ошибка: {e}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка отладки: {e}")


async def cmd_test_app(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Test command to check a specific app's current status from Google Play."""
    await _register_chat_from_update(update)
    if not context.args:
        await update.message.reply_text("Использование: /test_app <ссылка или package>")
        return

    raw = " ".join(context.args)
    fetch_url = raw if "play.google.com" in raw or raw.startswith("http") else db.link_from_package(raw)
    try:
        await update.message.reply_text(f"🔍 Тестирую приложение: {raw}")

        available, meta = await fetch_app_metadata(fetch_url)
        app_doc = await db.get_app_by_link_or_package(raw)
        db_status = app_doc.get("status", "unknown") if app_doc else "not_in_db"

        link = app_doc.get("link") or fetch_url if app_doc else fetch_url
        test_text = f"📱 Тест приложения\n"
        test_text += f"Package: {app_doc.get('package', raw) if app_doc else raw}\n"
        test_text += f"Ссылка: {link}\n\n"
        
        test_text += f"🌐 Google Play статус:\n"
        test_text += f"• Доступно: {'✅ Да' if available else '❌ Нет'}\n"
        
        if available:
            test_text += f"• Название: {meta.get('name', 'Не найдено')}\n"
            test_text += f"• Разработчик: {meta.get('developer', 'Не найден')}\n"
            test_text += f"• Версия: {meta.get('version', 'Не найдена')}\n"
            test_text += f"• Скриншоты: {len(meta.get('screenshots', []))}\n"
            test_text += f"• Иконка: {'✅' if meta.get('icon') else '❌'}\n"
        else:
            test_text += f"• Причина недоступности: Не удалось получить данные\n"
        
        test_text += f"\n🗄️ База данных:\n"
        test_text += f"• Статус: {_status_emoji(db_status)} {db_status}\n"
        
        if app_doc:
            test_text += f"• Последняя проверка: {await _fmt_dt(app_doc.get('last_checked_at'))}\n"
            if app_doc.get('banned_at'):
                test_text += f"• Забанено: {await _fmt_dt(app_doc.get('banned_at'))}\n"
            if app_doc.get('renew_at'):
                test_text += f"• Восстановлено: {await _fmt_dt(app_doc.get('renew_at'))}\n"
        
        # Check for discrepancies
        if available and db_status == "unavailable":
            test_text += f"\n⚠️ ВНИМАНИЕ: Приложение доступно в Google Play, но помечено как недоступное в базе!\n"
            test_text += f"Это может указывать на ошибку в логике определения статуса."
        elif not available and db_status == "available":
            test_text += f"\n⚠️ ВНИМАНИЕ: Приложение недоступно в Google Play, но помечено как доступное в базе!\n"
            test_text += f"Возможно, приложение было заблокировано недавно."
        
        await update.message.reply_text(test_text)
        
    except ValueError as e:
        await update.message.reply_text(f"Ошибка: {e}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка тестирования: {e}")


async def cmd_fix_app(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fix command to manually reset an app's status if it was incorrectly marked as banned."""
    await _register_chat_from_update(update)
    if not context.args:
        await update.message.reply_text("Использование: /fix_app <ссылка або package>")
        return

    raw = " ".join(context.args)
    fetch_url = raw if "play.google.com" in raw or raw.startswith("http") else db.link_from_package(raw)
    try:
        available, meta = await fetch_app_metadata(fetch_url)

        if not available:
            await update.message.reply_text("❌ Приложение действительно недоступно в Google Play. "
                                          "Исправление не требуется.")
            return

        app_doc = await db.get_app_by_link_or_package(raw)
        if not app_doc:
            await update.message.reply_text("❌ Приложение не найдено в базе данных.")
            return

        package = app_doc["package"]
        db_status = app_doc.get("status", "unknown")

        if db_status == "available":
            await update.message.reply_text("✅ Приложение уже помечено как доступное в базе данных.")
            return

        apps = db.get_apps_collection()
        now = datetime.now(timezone.utc)
        update_data = {
            "status": "available",
            "banned_at": None,
            "renew_at": now,
            "last_checked_at": now,
        }
        if meta.get("name"):
            update_data["name"] = meta["name"]
        if meta.get("developer"):
            update_data["developer"] = meta["developer"]
        if meta.get("version"):
            update_data["version"] = meta["version"]
        if meta.get("icon"):
            update_data["icon"] = meta["icon"]
        if meta.get("screenshots"):
            update_data["screenshots"] = meta["screenshots"]
        if meta.get("short_desc"):
            update_data["short_desc"] = meta["short_desc"]

        await apps.update_one(
            {"package": package},
            {"$set": update_data}
        )

        try:
            app_id = _resolve_app_id_for_doc(app_doc, package)
            await sync_app_status_to_crm(
                package=package,
                developer=app_doc.get("developer") or meta.get("developer"),
                available=True,
                app_name=meta.get("name") or app_doc.get("name"),
                custom_id=app_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("CRM: помилка синхронізації для %s: %s", package, e)

        link = app_doc.get("link") or db.link_from_package(package)
        fix_text = f"🔧 Приложение исправлено!\n\n"
        fix_text += f"📱 {meta.get('name', 'N/A')}\n"
        fix_text += f"Package: {package}\n"
        fix_text += f"Ссылка: {link}\n\n"
        fix_text += f"✅ Статус изменен с '{db_status}' на 'available'\n"
        fix_text += f"🕒 Время исправления: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        
        if meta.get("developer"):
            fix_text += f"👤 Разработчик: {meta['developer']}\n"
        if meta.get("version"):
            fix_text += f"🔢 Версия: {meta['version']}\n"
        if meta.get("screenshots"):
            fix_text += f"📸 Скриншоты: {len(meta['screenshots'])}\n"
        
        await update.message.reply_text(fix_text)
        
    except ValueError as e:
        await update.message.reply_text(f"Ошибка: {e}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка исправления: {e}")


_AWAITING_ADD_USER_ID = 1
CLEANUP_DAYS = 7


async def cmd_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_chat_from_update(update)
    if not _is_env_admin(update.effective_user.id):
        return
    candidates = await db.get_cleanup_candidates(CLEANUP_DAYS)
    if not candidates:
        await update.message.reply_text(f"✅ No apps inactive for {CLEANUP_DAYS}+ days.")
        return
    # Store packages in context so "Delete All" doesn't hit callback_data size limit
    context.user_data["cleanup_packages"] = [d["package"] for d in candidates]
    text, markup = _build_cleanup_panel(candidates)
    await update.message.reply_text(text, reply_markup=markup)


def _build_cleanup_panel(candidates: list) -> tuple:
    lines = []
    buttons = []
    for doc in candidates:
        pkg = doc.get("package", "")
        name = doc.get("name") or pkg
        if doc.get("first_time_added") and doc.get("status") != "unavailable":
            reason = f"moderation {CLEANUP_DAYS}+ days"
        else:
            banned_at = doc.get("banned_at")
            if banned_at:
                if banned_at.tzinfo is None:
                    banned_at = banned_at.replace(tzinfo=timezone.utc)
                reason = f"banned {(datetime.now(timezone.utc) - banned_at).days}d ago"
            else:
                reason = f"inactive {CLEANUP_DAYS}+ days"
        lines.append(f"• {name}\n  {pkg} ({reason})")
        buttons.append([InlineKeyboardButton(f"❌ {pkg}", callback_data=f"cleanup_one:{pkg}")])
    buttons.append([InlineKeyboardButton(f"🗑 Delete All ({len(candidates)})", callback_data="cleanup_all")])
    buttons.append([InlineKeyboardButton("✖️ Cancel", callback_data="cleanup_cancel")])
    text = f"🧹 Apps inactive for {CLEANUP_DAYS}+ days ({len(candidates)}):\n\n" + "\n\n".join(lines)
    return text, InlineKeyboardMarkup(buttons)


async def on_cleanup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_env_admin(query.from_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    await query.answer()
    data = query.data

    if data == "cleanup_cancel":
        await query.message.edit_text("Cancelled.")
        return

    if data.startswith("cleanup_one:"):
        pkg = data.split(":", 1)[1]
        await db.remove_app_by_package(pkg)
        candidates = await db.get_cleanup_candidates(CLEANUP_DAYS)
        context.user_data["cleanup_packages"] = [d["package"] for d in candidates]
        if not candidates:
            await query.message.edit_text("✅ Done — no more inactive apps.")
        else:
            text, markup = _build_cleanup_panel(candidates)
            await query.message.edit_text(text, reply_markup=markup)
        return

    if data == "cleanup_all":
        packages = context.user_data.pop("cleanup_packages", [])
        if not packages:
            await query.message.edit_text("⚠️ Nothing to delete, run /cleanup again.")
            return
        count = await db.remove_apps_by_packages(packages)
        await query.message.edit_text(f"✅ Deleted {count} apps.")


def _admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add User", callback_data="admin_add")],
        [InlineKeyboardButton("👥 Manage Users", callback_data="admin_users")],
        [InlineKeyboardButton("✖️ Close", callback_data="admin_close")],
    ])


async def _render_users_panel(message, edit: bool) -> None:
    users = await db.get_allowed_users()
    text = f"👥 Allowed Users ({len(users)}):" if users else "👥 No users added yet."
    buttons = [
        [InlineKeyboardButton(f"❌ {u['user_id']}", callback_data=f"admin_remove:{u['user_id']}")]
        for u in users
    ]
    buttons.append([InlineKeyboardButton("➕ Add User", callback_data="admin_add")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_back")])
    markup = InlineKeyboardMarkup(buttons)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_env_admin(update.effective_user.id):
        return
    await update.message.reply_text("🔐 Admin Panel", reply_markup=_admin_main_keyboard())


async def on_admin_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_env_admin(query.from_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return
    await query.answer()
    data = query.data

    if data == "admin_close":
        await query.message.delete()
    elif data == "admin_back":
        await query.message.edit_text("🔐 Admin Panel", reply_markup=_admin_main_keyboard())
    elif data == "admin_users":
        await _render_users_panel(query.message, edit=True)
    elif data.startswith("admin_remove:"):
        user_id = int(data.split(":")[1])
        removed = await db.remove_allowed_user(user_id)
        await query.answer(f"✅ Removed {user_id}" if removed else "User not found.", show_alert=False)
        await _render_users_panel(query.message, edit=True)


async def on_admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not _is_env_admin(query.from_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    await query.message.edit_text(
        "➕ Send me the Telegram user ID to add.\n\n"
        "You can get it by forwarding their message to @userinfobot\n\n"
        "Send /cancel to cancel."
    )
    return _AWAITING_ADD_USER_ID


async def on_receive_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.lstrip("-").isdigit():
        await update.message.reply_text("❌ Invalid ID. Send a numeric Telegram user ID or /cancel.")
        return _AWAITING_ADD_USER_ID
    user_id = int(text)
    added = await db.add_allowed_user(user_id, added_by=update.effective_user.id)
    if added:
        await update.message.reply_text(f"✅ User {user_id} added. Use /admin to manage users.")
    else:
        await update.message.reply_text(f"⚠️ User {user_id} is already in the allowed list.")
    return ConversationHandler.END


async def on_cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def post_init(app: Application) -> None:
    await db.init_db()
    await app.bot.set_my_commands(
        [
            ("start", "Приветствие и помощь"),
            ("help", "Список команд"),
            ("add", "Добавить приложение по ссылке"),
            ("remove", "Удалить приложение по ссылке"),
            ("list", "Список приложений"),
            ("subscribe", "Подписка на уведомления"),
            ("unsubscribe", "Отписка от уведомлений"),
            ("check", "Проверить все приложения"),
            ("test_app", "Тест приложения"),
            ("fix_app", "Исправить приложение"),
            ("cleanup", "Удалить неактивные приложения"),
        ]
    )
    app.job_queue.run_repeating(
        job_check_all_apps,
        interval=config.CHECK_INTERVAL.total_seconds(),
        first=10,
        name="check_all_apps",
    )


def main() -> None:
    application = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(TypeHandler(Update, check_access), group=-1)

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("remove", cmd_remove))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("subscribe", cmd_subscribe))
    application.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    application.add_handler(CommandHandler("check", cmd_check))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("test_app", cmd_test_app))
    application.add_handler(CommandHandler("fix_app", cmd_fix_app))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("cleanup", cmd_cleanup))
    application.add_handler(CallbackQueryHandler(on_cleanup_callback, pattern=r"^cleanup_"))

    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(on_admin_add, pattern="^admin_add$")],
        states={
            _AWAITING_ADD_USER_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_receive_user_id),
                CommandHandler("cancel", on_cancel_add),
            ],
        },
        fallbacks=[CommandHandler("cancel", on_cancel_add)],
    ))

    application.add_handler(CallbackQueryHandler(on_list_nav, pattern=r"^(list:|noop)"))
    application.add_handler(CallbackQueryHandler(on_admin_nav, pattern=r"^admin_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
