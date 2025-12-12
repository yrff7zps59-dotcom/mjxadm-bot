import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest

# ===========================================================
#                        CONFIGURATION
# ===========================================================

BOT_TOKEN = "8437034788:AAGo7r2mWueww-CMEtrVICqATt9YxLQnwnQ"
BASE_URL = "https://admin.majestic-files.net/api"
MONITOR_INTERVAL = 10
AUTO_REFRESH_INTERVAL = 15
ADMINS_PER_PAGE = 10

# ===========================================================
#                      INITIALIZATION
# ===========================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()


# ===========================================================
#                       DATA MODELS
# ===========================================================

@dataclass
class UserSession:
    session_id: str
    server_id: str
    login: str
    admin_level: int = 0
    rights: list = field(default_factory=list)
    notifications: bool = True
    auto_refresh: bool = True
    tracked_admin: str = ""


@dataclass
class MonitorState:
    online_admins: set = field(default_factory=set)
    reports_stats: dict = field(default_factory=dict)
    admin_reports: dict = field(default_factory=dict)


@dataclass
class LiveMessage:
    chat_id: int
    message_id: int
    view_type: str
    page: int = 0
    level_filter: int = 0
    admin_login: str = ""


user_sessions: dict[int, UserSession] = {}
monitor_states: dict[int, MonitorState] = {}
monitor_tasks: dict[int, asyncio.Task] = {}
live_messages: dict[int, LiveMessage] = {}
refresh_tasks: dict[int, asyncio.Task] = {}


class AuthStates(StatesGroup):
    waiting_server = State()
    waiting_login = State()
    waiting_password = State()
    waiting_2fa = State()


# ===========================================================
#                        UTILITIES
# ===========================================================

def format_time(seconds: int) -> str:
    """Format time from seconds to human readable"""
    if seconds == 0:
        return "0m"
    
    minutes = seconds // 60
    hours, mins = divmod(minutes, 60)
    
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    return f"{mins}m"


def get_level_emoji(level: int) -> str:
    return {4: "[4]", 3: "[3]", 2: "[2]", 1: "[1]"}.get(level, "[?]")


def get_level_name(level: int) -> str:
    return f"Level {level}"


def get_timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


async def api_get(session: UserSession, endpoint: str) -> dict:
    cookies = {"sessionId": session.session_id, "serverId": session.server_id}
    async with aiohttp.ClientSession(cookies=cookies) as http:
        async with http.get(f"{BASE_URL}{endpoint}") as resp:
            return await resp.json()


# ===========================================================
#                        KEYBOARDS
# ===========================================================

def kb_servers():
    buttons = []
    for i in range(1, 17, 4):
        row = [InlineKeyboardButton(text=f"RU{j}", callback_data=f"server:RU{j}") 
               for j in range(i, min(i + 4, 17))]
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Summary", callback_data="view:summary"),
            InlineKeyboardButton(text="Online", callback_data="view:online")
        ],
        [
            InlineKeyboardButton(text="Reports", callback_data="view:reports"),
            InlineKeyboardButton(text="Servers", callback_data="view:servers")
        ],
        [
            InlineKeyboardButton(text="All Admins", callback_data="view:admins:0:0"),
            InlineKeyboardButton(text="Settings", callback_data="settings")
        ]
    ])


def kb_view(view_type: str, auto_refresh: bool = True):
    refresh_icon = "||" if auto_refresh else ">"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Refresh", callback_data=f"refresh:{view_type}"),
            InlineKeyboardButton(text=f"{refresh_icon} Auto", callback_data=f"toggle_auto:{view_type}"),
        ],
        [InlineKeyboardButton(text="< Menu", callback_data="menu")]
    ])


def kb_admins_select(admins: list, page: int, total_pages: int, level_filter: int, auto_refresh: bool):
    buttons = []
    
    for i in range(0, len(admins), 2):
        row = []
        for admin in admins[i:i+2]:
            is_online = "* " if admin.get("online", 0) > 0 else ""
            row.append(InlineKeyboardButton(
                text=f"{is_online}{admin['login'][:12]}",
                callback_data=f"admin:{admin['login']}"
            ))
        buttons.append(row)
    
    level_row = []
    for lvl in [0, 4, 3, 2, 1]:
        text = "All" if lvl == 0 else f"L{lvl}"
        if lvl == level_filter:
            text = f"[{text}]"
        level_row.append(InlineKeyboardButton(text=text, callback_data=f"filter:{lvl}:{page}"))
    buttons.append(level_row)
    
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="<", callback_data=f"page:{page-1}:{level_filter}"))
    nav_row.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text=">", callback_data=f"page:{page+1}:{level_filter}"))
    buttons.append(nav_row)
    
    refresh_icon = "||" if auto_refresh else ">"
    buttons.append([
        InlineKeyboardButton(text="Refresh", callback_data=f"refresh:admins:{page}:{level_filter}"),
        InlineKeyboardButton(text=f"{refresh_icon} Auto", callback_data=f"toggle_auto:admins"),
    ])
    buttons.append([InlineKeyboardButton(text="< Menu", callback_data="menu")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_admin_profile(admin_login: str, is_tracked: bool, auto_refresh: bool):
    track_text = "Untrack" if is_tracked else "Track"
    refresh_icon = "||" if auto_refresh else ">"
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=track_text, callback_data=f"track:{admin_login}")],
        [
            InlineKeyboardButton(text="Refresh", callback_data=f"refresh:profile:{admin_login}"),
            InlineKeyboardButton(text=f"{refresh_icon} Auto", callback_data=f"toggle_auto:profile:{admin_login}"),
        ],
        [InlineKeyboardButton(text="< Back", callback_data="view:admins:0:0")],
        [InlineKeyboardButton(text="< Menu", callback_data="menu")]
    ])


def kb_settings(session: UserSession):
    notif = "Notifications: ON" if session.notifications else "Notifications: OFF"
    auto = "Auto-refresh: ON" if session.auto_refresh else "Auto-refresh: OFF"
    
    buttons = [
        [InlineKeyboardButton(text=notif, callback_data="toggle_notif")],
        [InlineKeyboardButton(text=auto, callback_data="toggle_global_auto")],
    ]
    
    if session.tracked_admin:
        buttons.append([InlineKeyboardButton(
            text=f"Untrack {session.tracked_admin}", 
            callback_data="untrack"
        )])
    
    buttons.append([InlineKeyboardButton(text="Logout", callback_data="logout")])
    buttons.append([InlineKeyboardButton(text="< Menu", callback_data="menu")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_guest():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Login", callback_data="login")]
    ])


# ===========================================================
#                   CONTENT GENERATORS
# ===========================================================

async def generate_summary(session: UserSession) -> str:
    stats = await api_get(session, "/admin/reports/statistics")
    admins_data = await api_get(session, "/admin/admins")
    servers_data = await api_get(session, "/meta/servers")
    
    r = stats.get("result", {})
    admins = admins_data.get("result", [])
    servers = servers_data.get("result", {}).get("servers", [])
    
    online_admins = [a for a in admins if a.get("online", 0) > 0]
    total_reports = sum(
        a.get("reports", {}).get("default", 0) + a.get("reports", {}).get("moderation", 0)
        for a in admins
    )
    
    ru_servers = [s for s in servers if s["id"].startswith("ru")]
    total_players = sum(s.get("players", 0) for s in ru_servers)
    
    my_server = next((s for s in servers if s["id"].lower() == session.server_id.lower()), None)
    my_server_info = ""
    if my_server:
        my_players = my_server.get("players", 0)
        my_queue = my_server.get("queuedPlayers", 0)
        my_status = "ON" if my_server.get("status") else "OFF"
        queue_str = f" (+{my_queue})" if my_queue > 0 else ""
        my_server_info = f"\nYour server ({my_server['name']}): {my_status} {my_players}{queue_str}"
    
    return (
        f"<b>Summary</b>\n"
        f"{'='*20}\n\n"
        f"<b>Reports:</b>\n"
        f"  Moderation: <b>{r.get('moderation', 0)}</b>\n"
        f"  In progress: <b>{r.get('progress', 0)}</b>\n"
        f"  Unresolved: <b>{r.get('unresolved', 0)}</b>\n"
        f"  At admins: <b>{total_reports}</b>\n\n"
        f"<b>Admins:</b> {len(online_admins)}/{len(admins)} online\n"
        f"<b>Players:</b> {total_players}{my_server_info}\n\n"
        f"<i>Updated: {get_timestamp()}</i>"
    )


async def generate_online(session: UserSession) -> str:
    data = await api_get(session, "/admin/admins")
    admins = data.get("result", [])
    online = [a for a in admins if a.get("online", 0) > 0]
    
    by_level = {}
    for admin in online:
        lvl = admin.get("admin", 0)
        by_level.setdefault(lvl, []).append(admin)
    
    text = (
        f"<b>Admins Online</b>\n"
        f"{'='*20}\n"
        f"Online: <b>{len(online)}</b> / {len(admins)}\n\n"
    )
    
    if online:
        for lvl in sorted(by_level.keys(), reverse=True):
            text += f"<b>{get_level_name(lvl)}</b>\n"
            # Sort by dayOnline (today's online time)
            for admin in sorted(by_level[lvl], key=lambda x: x.get("dayOnline", 0), reverse=True):
                # dayOnline = online time today (in seconds)
                time_str = format_time(admin.get("dayOnline", 0))
                reports = admin.get("reports", {})
                rep = reports.get("default", 0) + reports.get("moderation", 0)
                rep_str = f" [R:{rep}]" if rep > 0 else ""
                text += f"  * {admin['login']} <code>({time_str})</code>{rep_str}\n"
            text += "\n"
    else:
        text += "No one online\n"
    
    text += f"<i>Updated: {get_timestamp()}</i>"
    return text


async def generate_reports(session: UserSession) -> str:
    stats = await api_get(session, "/admin/reports/statistics")
    admins_data = await api_get(session, "/admin/admins")
    
    r = stats.get("result", {})
    admins = admins_data.get("result", [])
    
    admin_reports = []
    for admin in admins:
        rep = admin.get("reports", {})
        count = rep.get("default", 0) + rep.get("moderation", 0)
        if count > 0:
            is_online = admin.get("online", 0) > 0
            admin_reports.append((admin["login"], count, is_online, admin.get("admin", 0)))
    
    admin_reports.sort(key=lambda x: x[1], reverse=True)
    total = sum(x[1] for x in admin_reports)
    
    text = (
        f"<b>Reports</b>\n"
        f"{'='*20}\n\n"
        f"<b>Statistics:</b>\n"
        f"  Moderation: <b>{r.get('moderation', 0)}</b>\n"
        f"  In progress: <b>{r.get('progress', 0)}</b>\n"
        f"  Unresolved: <b>{r.get('unresolved', 0)}</b>\n\n"
    )
    
    if admin_reports:
        text += f"<b>At admins</b> ({total}):\n"
        for login, count, is_online, lvl in admin_reports[:12]:
            status = "*" if is_online else " "
            text += f"  {status} {get_level_emoji(lvl)} {login}: <b>{count}</b>\n"
        if len(admin_reports) > 12:
            text += f"  <i>... and {len(admin_reports) - 12} more</i>\n"
    
    text += f"\n<i>Updated: {get_timestamp()}</i>"
    return text


async def generate_servers(session: UserSession) -> str:
    data = await api_get(session, "/meta/servers")
    servers = data.get("result", {}).get("servers", [])
    
    ru_servers = sorted(
        [s for s in servers if s["id"].startswith("ru")],
        key=lambda x: int(x["id"][2:]) if x["id"][2:].isdigit() else 99
    )
    
    total = sum(s.get("players", 0) for s in ru_servers)
    queue = sum(s.get("queuedPlayers", 0) for s in ru_servers)
    
    text = (
        f"<b>Servers</b>\n"
        f"{'='*20}\n"
        f"Total: <b>{total}</b>"
    )
    if queue > 0:
        text += f" (+{queue} in queue)"
    text += "\n\n"
    
    for s in ru_servers:
        status = "+" if s.get("status") else "-"
        tech = " [TECH]" if s.get("techWorks") else ""
        players = s.get("players", 0)
        q = s.get("queuedPlayers", 0)
        q_str = f" <i>(+{q})</i>" if q > 0 else ""
        text += f"{status} <b>{s['name']}</b>: {players}{q_str}{tech}\n"
    
    text += f"\n<i>Updated: {get_timestamp()}</i>"
    return text


async def generate_admins_with_buttons(session: UserSession, page: int = 0, level_filter: int = 0):
    data = await api_get(session, "/admin/admins")
    admins = data.get("result", [])
    
    if level_filter > 0:
        admins = [a for a in admins if a.get("admin", 0) == level_filter]
    
    admins = sorted(admins, key=lambda x: x.get("weekOnline", 0), reverse=True)
    
    total_pages = max(1, (len(admins) + ADMINS_PER_PAGE - 1) // ADMINS_PER_PAGE)
    page = min(page, total_pages - 1)
    
    start = page * ADMINS_PER_PAGE
    end = start + ADMINS_PER_PAGE
    page_admins = admins[start:end]
    
    filter_text = f"Level {level_filter}" if level_filter > 0 else "All levels"
    
    tracked_info = ""
    if session.tracked_admin:
        tracked = next((a for a in data.get("result", []) if a["login"] == session.tracked_admin), None)
        if tracked:
            is_on = "*" if tracked.get("online", 0) > 0 else " "
            rep = tracked.get("reports", {}).get("default", 0) + tracked.get("reports", {}).get("moderation", 0)
            tracked_info = f"\nTracking: {is_on} <b>{session.tracked_admin}</b> (R:{rep})\n"
    
    text = (
        f"<b>Admins</b> ({len(admins)})\n"
        f"{'='*20}\n"
        f"Filter: <b>{filter_text}</b>{tracked_info}\n"
        f"<i>Select admin for details:</i>\n\n"
        f"<i>Updated: {get_timestamp()}</i>"
    )
    
    kb = kb_admins_select(page_admins, page, total_pages, level_filter, session.auto_refresh)
    return text, kb, total_pages


async def generate_admin_profile(session: UserSession, admin_login: str) -> tuple[str, dict]:
    data = await api_get(session, "/admin/admins")
    admins = data.get("result", [])
    
    admin = next((a for a in admins if a["login"] == admin_login), None)
    if not admin:
        return f"Admin <b>{admin_login}</b> not found", {}
    
    is_online = "ONLINE" if admin.get("online", 0) > 0 else "OFFLINE"
    
    reports = admin.get("reports", {})
    rep_default = reports.get("default", 0)
    rep_mod = reports.get("moderation", 0)
    rep_total = rep_default + rep_mod
    
    other = admin.get("otherAccountsOnline", {})
    
    text = (
        f"<b>{admin['login']}</b>\n"
        f"{'='*20}\n\n"
        f"{get_level_name(admin.get('admin', 0))}\n"
        f"Status: {is_online}"
    )
    
    if admin.get("online", 0) > 0:
        text += f" ({format_time(admin.get('online', 0))})"
    
    text += (
        f"\n\n<b>Online time:</b>\n"
        f"  Today: <b>{format_time(admin.get('dayOnline', 0))}</b>\n"
        f"  Week: <b>{format_time(admin.get('weekOnline', 0))}</b>\n"
        f"  Month: <b>{format_time(admin.get('monthOnline', 0))}</b>\n\n"
        f"<b>Reports:</b> {rep_total}\n"
        f"  Default: {rep_default}\n"
        f"  Moderation: {rep_mod}\n"
    )
    
    if any(other.values()):
        text += (
            f"\n<b>Other accounts:</b>\n"
            f"  Week: {format_time(other.get('weekOnline', 0))}\n"
            f"  Month: {format_time(other.get('monthOnline', 0))}\n"
        )
    
    text += f"\n<i>Updated: {get_timestamp()}</i>"
    return text, admin


# ===========================================================
#                    AUTO-REFRESH SYSTEM
# ===========================================================

async def auto_refresh_loop(user_id: int):
    while user_id in user_sessions and user_id in live_messages:
        session = user_sessions[user_id]
        if not session.auto_refresh:
            await asyncio.sleep(AUTO_REFRESH_INTERVAL)
            continue
        
        live = live_messages.get(user_id)
        if not live:
            break
            
        try:
            if live.view_type == "summary":
                text = await generate_summary(session)
                kb = kb_view("summary", session.auto_refresh)
            elif live.view_type == "online":
                text = await generate_online(session)
                kb = kb_view("online", session.auto_refresh)
            elif live.view_type == "reports":
                text = await generate_reports(session)
                kb = kb_view("reports", session.auto_refresh)
            elif live.view_type == "servers":
                text = await generate_servers(session)
                kb = kb_view("servers", session.auto_refresh)
            elif live.view_type == "admins":
                text, kb, _ = await generate_admins_with_buttons(session, live.page, live.level_filter)
            elif live.view_type == "admin_profile" and live.admin_login:
                text, admin = await generate_admin_profile(session, live.admin_login)
                is_tracked = session.tracked_admin == live.admin_login
                kb = kb_admin_profile(live.admin_login, is_tracked, session.auto_refresh)
            else:
                break
            
            await bot.edit_message_text(
                text=text,
                chat_id=live.chat_id,
                message_id=live.message_id,
                parse_mode="HTML",
                reply_markup=kb
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                if user_id in live_messages:
                    del live_messages[user_id]
                break
        except Exception:
            pass
        
        await asyncio.sleep(AUTO_REFRESH_INTERVAL)


def start_auto_refresh(user_id: int):
    if user_id in refresh_tasks:
        refresh_tasks[user_id].cancel()
    refresh_tasks[user_id] = asyncio.create_task(auto_refresh_loop(user_id))


def stop_auto_refresh(user_id: int):
    if user_id in refresh_tasks:
        refresh_tasks[user_id].cancel()
        del refresh_tasks[user_id]
    if user_id in live_messages:
        del live_messages[user_id]


# ===========================================================
#                    MONITORING SYSTEM
# ===========================================================

async def monitor_loop(user_id: int):
    while user_id in user_sessions:
        session = user_sessions[user_id]
        if not session.notifications:
            await asyncio.sleep(MONITOR_INTERVAL)
            continue
            
        try:
            admins_data = await api_get(session, "/admin/admins")
            stats_data = await api_get(session, "/admin/reports/statistics")
            
            if not admins_data.get("status") or not stats_data.get("status"):
                await asyncio.sleep(MONITOR_INTERVAL)
                continue
            
            admins = admins_data["result"]
            stats = stats_data["result"]
            
            if user_id not in monitor_states:
                monitor_states[user_id] = MonitorState()
                monitor_states[user_id].online_admins = {a["login"] for a in admins if a.get("online", 0) > 0}
                monitor_states[user_id].reports_stats = stats.copy()
                monitor_states[user_id].admin_reports = {
                    a["login"]: a.get("reports", {}).get("default", 0) + a.get("reports", {}).get("moderation", 0)
                    for a in admins
                }
                await asyncio.sleep(MONITOR_INTERVAL)
                continue
            
            state = monitor_states[user_id]
            notifications = []
            
            current_online = {a["login"] for a in admins if a.get("online", 0) > 0}
            joined = current_online - state.online_admins
            left = state.online_admins - current_online
            
            for login in joined:
                admin = next((a for a in admins if a["login"] == login), None)
                if admin:
                    lvl = admin.get("admin", 0)
                    notifications.append(f"+ <b>{login}</b> joined ({get_level_name(lvl)})")
            
            for login in left:
                notifications.append(f"- <b>{login}</b> left")
            
            state.online_admins = current_online
            
            if stats != state.reports_stats:
                old, new = state.reports_stats, stats
                changes = []
                
                for key, name in [("moderation", "Moderation"), ("progress", "In progress"), ("unresolved", "Unresolved")]:
                    if new.get(key, 0) != old.get(key, 0):
                        diff = new[key] - old.get(key, 0)
                        sign = "+" if diff > 0 else ""
                        changes.append(f"{name}: {old.get(key, 0)} -> {new[key]} ({sign}{diff})")
                
                if changes:
                    notifications.append("<b>Stats changed:</b>\n" + "\n".join(f"  {c}" for c in changes))
                
                state.reports_stats = stats.copy()
            
            tracked = session.tracked_admin
            for admin in admins:
                login = admin["login"]
                new_count = admin.get("reports", {}).get("default", 0) + admin.get("reports", {}).get("moderation", 0)
                old_count = state.admin_reports.get(login, 0)
                
                if new_count != old_count:
                    diff = new_count - old_count
                    if tracked and login != tracked:
                        state.admin_reports[login] = new_count
                        continue
                    
                    if diff > 0:
                        notifications.append(f"<b>{login}</b> +{diff} report (total: {new_count})")
                    else:
                        notifications.append(f"<b>{login}</b> closed {abs(diff)} (left: {new_count})")
                
                state.admin_reports[login] = new_count
            
            if tracked:
                tracked_admin = next((a for a in admins if a["login"] == tracked), None)
                if tracked_admin:
                    is_online = tracked_admin.get("online", 0) > 0
                    was_online = tracked in state.online_admins
                    
                    if is_online and not was_online:
                        notifications.insert(0, f"<b>Tracked admin {tracked} joined!</b>")
                    elif not is_online and was_online:
                        notifications.insert(0, f"<b>Tracked admin {tracked} left!</b>")
            
            if notifications:
                text = "<b>Notifications</b>\n\n" + "\n\n".join(notifications)
                try:
                    await bot.send_message(user_id, text, parse_mode="HTML")
                except:
                    pass
                    
        except:
            pass
        
        await asyncio.sleep(MONITOR_INTERVAL)


def start_monitor(user_id: int):
    if user_id in monitor_tasks:
        monitor_tasks[user_id].cancel()
    monitor_tasks[user_id] = asyncio.create_task(monitor_loop(user_id))


def stop_monitor(user_id: int):
    if user_id in monitor_tasks:
        monitor_tasks[user_id].cancel()
        del monitor_tasks[user_id]
    if user_id in monitor_states:
        del monitor_states[user_id]


# ===========================================================
#                      BOT COMMANDS
# ===========================================================

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    stop_auto_refresh(user_id)
    
    if user_id in user_sessions:
        session = user_sessions[user_id]
        await message.answer(
            f"<b>MAJESTIC MONITOR</b>\n\n"
            f"Account: <b>{session.login}</b>\n"
            f"Server: <code>{session.server_id}</code>\n"
            f"{get_level_name(session.admin_level)}\n\n"
            f"Select action:",
            parse_mode="HTML",
            reply_markup=kb_main()
        )
    else:
        await message.answer(
            f"<b>MAJESTIC MONITOR</b>\n\n"
            f"Bot for monitoring admin panel\n"
            f"majestic-files.net\n\n"
            f"Features:\n"
            f"  - Real-time notifications\n"
            f"  - Auto-refresh statistics\n"
            f"  - Admin list with pagination",
            parse_mode="HTML",
            reply_markup=kb_guest()
        )


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    user_id = message.from_user.id
    stop_auto_refresh(user_id)
    
    if user_id not in user_sessions:
        return await message.answer("Please login first", reply_markup=kb_guest())
    
    session = user_sessions[user_id]
    await message.answer(
        f"<b>{session.login}</b> | {session.server_id}\n\nSelect action:",
        parse_mode="HTML",
        reply_markup=kb_main()
    )


# ===========================================================
#                    CALLBACK HANDLERS
# ===========================================================

@router.callback_query(F.data == "login")
async def cb_login(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "<b>Authorization</b>\n\nSelect server:",
        parse_mode="HTML",
        reply_markup=kb_servers()
    )
    await state.set_state(AuthStates.waiting_server)


@router.callback_query(F.data.startswith("server:"))
async def cb_server(callback: CallbackQuery, state: FSMContext):
    server_id = callback.data.split(":")[1]
    await state.update_data(server_id=server_id)
    await callback.message.edit_text(
        f"<b>Authorization</b>\n\nServer: <code>{server_id}</code>\n\nEnter login:",
        parse_mode="HTML"
    )
    await state.set_state(AuthStates.waiting_login)


@router.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Cancelled", reply_markup=kb_guest())


@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    stop_auto_refresh(user_id)
    
    if user_id not in user_sessions:
        return await callback.message.edit_text("Session expired", reply_markup=kb_guest())
    
    session = user_sessions[user_id]
    await callback.message.edit_text(
        f"<b>{session.login}</b> | {session.server_id}\n\nSelect action:",
        parse_mode="HTML",
        reply_markup=kb_main()
    )


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "settings")
async def cb_settings(callback: CallbackQuery):
    user_id = callback.from_user.id
    stop_auto_refresh(user_id)
    
    if user_id not in user_sessions:
        return await callback.message.edit_text("Session expired", reply_markup=kb_guest())
    
    session = user_sessions[user_id]
    tracked_info = f"\nTracking: <b>{session.tracked_admin}</b>" if session.tracked_admin else ""
    
    await callback.message.edit_text(
        f"<b>Settings</b>\n\n"
        f"Account: <b>{session.login}</b>\n"
        f"Server: <code>{session.server_id}</code>\n"
        f"{get_level_name(session.admin_level)}{tracked_info}",
        parse_mode="HTML",
        reply_markup=kb_settings(session)
    )


@router.callback_query(F.data == "untrack")
async def cb_untrack(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_sessions:
        return await callback.answer("Session expired")
    
    session = user_sessions[user_id]
    old_tracked = session.tracked_admin
    session.tracked_admin = ""
    
    await callback.answer(f"Untracked {old_tracked}")
    
    await callback.message.edit_text(
        f"<b>Settings</b>\n\n"
        f"Account: <b>{session.login}</b>\n"
        f"Server: <code>{session.server_id}</code>\n"
        f"{get_level_name(session.admin_level)}",
        parse_mode="HTML",
        reply_markup=kb_settings(session)
    )


@router.callback_query(F.data == "toggle_notif")
async def cb_toggle_notif(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_sessions:
        return await callback.answer("Session expired")
    
    session = user_sessions[user_id]
    session.notifications = not session.notifications
    
    await callback.answer(f"Notifications {'ON' if session.notifications else 'OFF'}")
    await callback.message.edit_reply_markup(reply_markup=kb_settings(session))


@router.callback_query(F.data == "toggle_global_auto")
async def cb_toggle_global_auto(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_sessions:
        return await callback.answer("Session expired")
    
    session = user_sessions[user_id]
    session.auto_refresh = not session.auto_refresh
    
    await callback.answer(f"Auto-refresh {'ON' if session.auto_refresh else 'OFF'}")
    await callback.message.edit_reply_markup(reply_markup=kb_settings(session))


@router.callback_query(F.data == "logout")
async def cb_logout(callback: CallbackQuery):
    user_id = callback.from_user.id
    stop_monitor(user_id)
    stop_auto_refresh(user_id)
    if user_id in user_sessions:
        del user_sessions[user_id]
    
    await callback.message.edit_text("Logged out", reply_markup=kb_guest())


# ===========================================================
#                    VIEW HANDLERS
# ===========================================================

@router.callback_query(F.data.startswith("view:"))
async def cb_view(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_sessions:
        return await callback.message.edit_text("Session expired", reply_markup=kb_guest())
    
    session = user_sessions[user_id]
    parts = callback.data.split(":")
    view_type = parts[1]
    
    await callback.answer("Loading...")
    
    try:
        if view_type == "summary":
            text = await generate_summary(session)
            kb = kb_view("summary", session.auto_refresh)
            live_messages[user_id] = LiveMessage(callback.message.chat.id, callback.message.message_id, "summary")
            
        elif view_type == "online":
            text = await generate_online(session)
            kb = kb_view("online", session.auto_refresh)
            live_messages[user_id] = LiveMessage(callback.message.chat.id, callback.message.message_id, "online")
            
        elif view_type == "reports":
            text = await generate_reports(session)
            kb = kb_view("reports", session.auto_refresh)
            live_messages[user_id] = LiveMessage(callback.message.chat.id, callback.message.message_id, "reports")
            
        elif view_type == "servers":
            text = await generate_servers(session)
            kb = kb_view("servers", session.auto_refresh)
            live_messages[user_id] = LiveMessage(callback.message.chat.id, callback.message.message_id, "servers")
            
        elif view_type == "admins":
            page = int(parts[2]) if len(parts) > 2 else 0
            level_filter = int(parts[3]) if len(parts) > 3 else 0
            text, kb, _ = await generate_admins_with_buttons(session, page, level_filter)
            live_messages[user_id] = LiveMessage(
                callback.message.chat.id, callback.message.message_id, "admins", page, level_filter
            )
        else:
            return
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        start_auto_refresh(user_id)
        
    except Exception as e:
        await callback.answer(f"Error: {e}", show_alert=True)


@router.callback_query(F.data.startswith("admin:"))
async def cb_admin_profile(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_sessions:
        return await callback.answer("Session expired")
    
    session = user_sessions[user_id]
    admin_login = callback.data.split(":", 1)[1]
    
    await callback.answer("Loading...")
    
    try:
        text, admin = await generate_admin_profile(session, admin_login)
        is_tracked = session.tracked_admin == admin_login
        kb = kb_admin_profile(admin_login, is_tracked, session.auto_refresh)
        
        live_messages[user_id] = LiveMessage(
            callback.message.chat.id, callback.message.message_id, "admin_profile", 
            admin_login=admin_login
        )
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        start_auto_refresh(user_id)
        
    except Exception as e:
        await callback.answer(f"Error: {e}", show_alert=True)


@router.callback_query(F.data.startswith("track:"))
async def cb_track_admin(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_sessions:
        return await callback.answer("Session expired")
    
    session = user_sessions[user_id]
    admin_login = callback.data.split(":", 1)[1]
    
    if session.tracked_admin == admin_login:
        session.tracked_admin = ""
        await callback.answer(f"Untracked {admin_login}")
    else:
        session.tracked_admin = admin_login
        await callback.answer(f"Now tracking {admin_login}")
    
    is_tracked = session.tracked_admin == admin_login
    kb = kb_admin_profile(admin_login, is_tracked, session.auto_refresh)
    await callback.message.edit_reply_markup(reply_markup=kb)


@router.callback_query(F.data.startswith("refresh:"))
async def cb_refresh(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_sessions:
        return await callback.answer("Session expired")
    
    session = user_sessions[user_id]
    parts = callback.data.split(":")
    view_type = parts[1]
    
    try:
        if view_type == "admins":
            page = int(parts[2]) if len(parts) > 2 else 0
            level_filter = int(parts[3]) if len(parts) > 3 else 0
            text, kb, _ = await generate_admins_with_buttons(session, page, level_filter)
            live_messages[user_id] = LiveMessage(
                callback.message.chat.id, callback.message.message_id, "admins", page, level_filter
            )
        elif view_type == "profile":
            admin_login = parts[2] if len(parts) > 2 else ""
            text, _ = await generate_admin_profile(session, admin_login)
            is_tracked = session.tracked_admin == admin_login
            kb = kb_admin_profile(admin_login, is_tracked, session.auto_refresh)
            live_messages[user_id] = LiveMessage(
                callback.message.chat.id, callback.message.message_id, "admin_profile", admin_login=admin_login
            )
        else:
            if view_type == "summary":
                text = await generate_summary(session)
            elif view_type == "online":
                text = await generate_online(session)
            elif view_type == "reports":
                text = await generate_reports(session)
            elif view_type == "servers":
                text = await generate_servers(session)
            else:
                return await callback.answer()
            kb = kb_view(view_type, session.auto_refresh)
            live_messages[user_id] = LiveMessage(callback.message.chat.id, callback.message.message_id, view_type)
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await callback.answer("Refreshed")
        
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await callback.answer("No changes")
        else:
            await callback.answer("Error")
    except Exception as e:
        await callback.answer(f"Error: {e}", show_alert=True)


@router.callback_query(F.data.startswith("toggle_auto:"))
async def cb_toggle_auto(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_sessions:
        return await callback.answer("Session expired")
    
    session = user_sessions[user_id]
    session.auto_refresh = not session.auto_refresh
    
    parts = callback.data.split(":")
    view_type = parts[1]
    
    if session.auto_refresh:
        start_auto_refresh(user_id)
        await callback.answer("Auto-refresh ON")
    else:
        stop_auto_refresh(user_id)
        await callback.answer("Auto-refresh OFF")
    
    live = live_messages.get(user_id)
    
    if view_type == "admins" and live:
        text, kb, _ = await generate_admins_with_buttons(session, live.page, live.level_filter)
    elif view_type == "profile" and len(parts) > 2:
        admin_login = parts[2]
        is_tracked = session.tracked_admin == admin_login
        kb = kb_admin_profile(admin_login, is_tracked, session.auto_refresh)
    else:
        kb = kb_view(view_type, session.auto_refresh)
    
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except:
        pass


@router.callback_query(F.data.startswith("page:"))
async def cb_page(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_sessions:
        return await callback.answer("Session expired")
    
    session = user_sessions[user_id]
    parts = callback.data.split(":")
    page = int(parts[1])
    level_filter = int(parts[2])
    
    try:
        text, kb, _ = await generate_admins_with_buttons(session, page, level_filter)
        
        live_messages[user_id] = LiveMessage(
            callback.message.chat.id, callback.message.message_id, "admins", page, level_filter
        )
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await callback.answer()
        
    except Exception as e:
        await callback.answer(f"Error: {e}", show_alert=True)


@router.callback_query(F.data.startswith("filter:"))
async def cb_filter(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_sessions:
        return await callback.answer("Session expired")
    
    session = user_sessions[user_id]
    parts = callback.data.split(":")
    level_filter = int(parts[1])
    
    try:
        text, kb, _ = await generate_admins_with_buttons(session, 0, level_filter)
        
        live_messages[user_id] = LiveMessage(
            callback.message.chat.id, callback.message.message_id, "admins", 0, level_filter
        )
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await callback.answer()
        
    except Exception as e:
        await callback.answer(f"Error: {e}", show_alert=True)


# ===========================================================
#                    AUTHORIZATION
# ===========================================================

@router.message(AuthStates.waiting_login)
async def process_login(message: Message, state: FSMContext):
    await state.update_data(login=message.text)
    await message.answer("Enter password:")
    await state.set_state(AuthStates.waiting_password)


@router.message(AuthStates.waiting_password)
async def process_password(message: Message, state: FSMContext):
    await state.update_data(password=message.text)
    try:
        await message.delete()
    except:
        pass
    await message.answer("Enter 2FA code:")
    await state.set_state(AuthStates.waiting_2fa)


@router.message(AuthStates.waiting_2fa)
async def process_2fa(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    
    status_msg = await message.answer("Authorizing...")
    
    auth_data = {
        "login": data["login"],
        "password": data["password"],
        "serverId": data["server_id"],
        "code": message.text
    }
    
    async with aiohttp.ClientSession() as http_session:
        try:
            async with http_session.post(f"{BASE_URL}/auth/login", json=auth_data) as resp:
                login_resp = await resp.json()
            
            if login_resp.get("status") and login_resp.get("result", {}).get("sessionId"):
                result = login_resp["result"]
                session_id = result["sessionId"]
                server_id = result["serverId"]
                
                cookies = {"sessionId": session_id, "serverId": server_id}
                async with http_session.get(f"{BASE_URL}/admin/users/me", cookies=cookies) as me_resp:
                    me_data = await me_resp.json()
                
                user_info = me_data.get("result", {})
                
                user_sessions[message.from_user.id] = UserSession(
                    session_id=session_id,
                    server_id=server_id,
                    login=result["account"]["login"],
                    admin_level=user_info.get("adminLevel", 0),
                    rights=user_info.get("rights", [])
                )
                
                start_monitor(message.from_user.id)
                
                await status_msg.edit_text(
                    f"<b>Authorization successful!</b>\n\n"
                    f"Account: <b>{result['account']['login']}</b>\n"
                    f"Server: <code>{server_id}</code>\n"
                    f"{get_level_name(user_info.get('adminLevel', 0))}\n"
                    f"Rights: {len(user_info.get('rights', []))}\n\n"
                    f"Notifications: ON\n"
                    f"Auto-refresh: ON",
                    parse_mode="HTML",
                    reply_markup=kb_main()
                )
            else:
                error = login_resp.get("result", "Unknown error")
                await status_msg.edit_text(
                    f"<b>Authorization failed</b>\n\n{error}",
                    parse_mode="HTML",
                    reply_markup=kb_guest()
                )
        except Exception as e:
            await status_msg.edit_text(
                f"<b>Error</b>\n\n{e}",
                parse_mode="HTML",
                reply_markup=kb_guest()
            )


# ===========================================================
#                         STARTUP
# ===========================================================

async def set_commands():
    commands = [
        BotCommand(command="start", description="Main menu"),
        BotCommand(command="menu", description="Open menu"),
    ]
    await bot.set_my_commands(commands)


async def shutdown():
    print("\nShutting down...")
    
    for user_id in list(monitor_tasks.keys()):
        stop_monitor(user_id)
    print("  - Monitoring stopped")
    
    for user_id in list(refresh_tasks.keys()):
        stop_auto_refresh(user_id)
    print("  - Auto-refresh stopped")
    
    await bot.session.close()
    print("  - Bot session closed")
    
    print("Bot stopped!")


async def main():
    await set_commands()
    dp.include_router(router)
    
    print("=" * 30)
    print("  MAJESTIC MONITOR")
    print("=" * 30)
    print("Bot started!")
    print("Press Ctrl+C to stop\n")
    
    try:
        await dp.start_polling(bot)
    finally:
        await shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
