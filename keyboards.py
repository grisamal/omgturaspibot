from datetime import datetime, timedelta, timezone
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import OMSK_OFFSET

_tz_omsk = timezone(timedelta(hours=OMSK_OFFSET))

RU_SHORT_DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def group_search_results_kb(groups: list) -> InlineKeyboardMarkup:
    rows = []
    for g in groups[:8]:
        gid = g["id"]
        label = g.get("label", str(gid))
        desc = g.get("description", "")
        btn_text = f"🎓 {label} ({desc})" if desc else f"🎓 {label}"
        if len(btn_text) > 40:
            btn_text = btn_text[:37] + "..."
        rows.append([InlineKeyboardButton(
            text=btn_text,
            callback_data=f"sel_grp_{gid}_{label}"
        )])
    rows.append([InlineKeyboardButton(text="🔄 Искать снова", callback_data="onboard_group_retry")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subgroup_select_kb(prefix: str = "sg_sel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1️⃣ 1 подгруппа", callback_data=f"{prefix}_1"),
            InlineKeyboardButton(text="2️⃣ 2 подгруппа", callback_data=f"{prefix}_2"),
        ],
        [InlineKeyboardButton(text="👥 Обе подгруппы (вся группа)", callback_data=f"{prefix}_0")],
    ])


def schedule_nav_kb(current_date_str: str, is_owner: bool = False) -> InlineKeyboardMarkup:
    # Parsed current date
    dt = datetime.strptime(current_date_str, "%Y-%m-%d").replace(tzinfo=_tz_omsk)
    prev_date = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
    today_str = datetime.now(_tz_omsk).strftime("%Y-%m-%d")
    tomorrow_str = (datetime.now(_tz_omsk) + timedelta(days=1)).strftime("%Y-%m-%d")

    rows = [
        [
            InlineKeyboardButton(text="⬅️", callback_data=f"show_date_{prev_date}"),
            InlineKeyboardButton(text="📅 Сегодня", callback_data=f"show_date_{today_str}"),
            InlineKeyboardButton(text="📆 Завтра", callback_data=f"show_date_{tomorrow_str}"),
            InlineKeyboardButton(text="➡️", callback_data=f"show_date_{next_date}"),
        ],
        [
            InlineKeyboardButton(text="🗓 На неделю", callback_data=f"week_view_{current_date_str}"),
            InlineKeyboardButton(text="📋 Выбрать день", callback_data=f"picker_{current_date_str}"),
        ],
        [
            InlineKeyboardButton(text="🔄 Обновить расписание", callback_data=f"refresh_{current_date_str}"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Настройки группы/подгруппы", callback_data="settings_menu"),
        ]
    ]

    if is_owner:
        rows.append([
            InlineKeyboardButton(text="👑 Секретная Админка", callback_data="admin_menu")
        ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def day_picker_kb(base_date_str: str) -> InlineKeyboardMarkup:
    dt = datetime.strptime(base_date_str, "%Y-%m-%d").replace(tzinfo=_tz_omsk)
    # Понедельник текущей недели
    monday = dt - timedelta(days=dt.weekday())

    row1 = []
    row2 = []
    for i in range(6):  # Пн - Сб
        day_dt = monday + timedelta(days=i)
        day_str = day_dt.strftime("%Y-%m-%d")
        short_label = f"{RU_SHORT_DAYS[i]} {day_dt.strftime('%d.%m')}"
        btn = InlineKeyboardButton(text=short_label, callback_data=f"show_date_{day_str}")
        if i < 3:
            row1.append(btn)
        else:
            row2.append(btn)

    rows = [
        row1,
        row2,
        [InlineKeyboardButton(text="🔙 Назад к расписанию", callback_data=f"show_date_{base_date_str}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def week_nav_kb(monday_str: str) -> InlineKeyboardMarkup:
    m_dt = datetime.strptime(monday_str, "%Y-%m-%d").replace(tzinfo=_tz_omsk)
    prev_m = (m_dt - timedelta(days=7)).strftime("%Y-%m-%d")
    next_m = (m_dt + timedelta(days=7)).strftime("%Y-%m-%d")
    today_str = datetime.now(_tz_omsk).strftime("%Y-%m-%d")

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ Пред. неделя", callback_data=f"week_view_{prev_m}"),
            InlineKeyboardButton(text="➡️ След. неделя", callback_data=f"week_view_{next_m}"),
        ],
        [InlineKeyboardButton(text="📅 К сегодняшнему дню", callback_data=f"show_date_{today_str}")],
    ])


def settings_kb(group_name: str, subgroup: int, notify: int, current_date_str: str) -> InlineKeyboardMarkup:
    sg_label = f"{subgroup} подгруппа" if subgroup in (1, 2) else "вся группа"
    notify_icon = "✅ Включены" if notify == 1 else "❌ Выключены"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎓 Группа: {group_name}", callback_data="change_group_start")],
        [InlineKeyboardButton(text=f"👥 Подгруппа: {sg_label}", callback_data="change_subgroup_start")],
        [InlineKeyboardButton(text=f"🔔 Уведомления об изменениях: {notify_icon}", callback_data="toggle_notify")],
        [InlineKeyboardButton(text="🔙 Назад к расписанию", callback_data=f"show_date_{current_date_str}")],
    ])


def admin_menu_kb() -> InlineKeyboardMarkup:
    today_str = datetime.now(_tz_omsk).strftime("%Y-%m-%d")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Список студентов и групп", callback_data="adm_usr_0")],
        [InlineKeyboardButton(text="📊 Статистика бота", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📢 Рассылка всем пользователям", callback_data="admin_broadcast_start")],
        [InlineKeyboardButton(text="🔄 Принудительная сверка с сайтом", callback_data="admin_sync_now")],
        [InlineKeyboardButton(text="🌐 Обновить VPN-подписку (Xray)", callback_data="admin_update_vpn")],
        [InlineKeyboardButton(text="💾 Скачать бэкап базы (.db)", callback_data="admin_backup_db")],
        [InlineKeyboardButton(text="🔙 Назад к расписанию", callback_data=f"show_date_{today_str}")],
    ])


def admin_users_list_kb(users: list, page: int = 0, per_page: int = 10) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(users) + per_page - 1) // per_page)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_usr_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if (page + 1) * per_page < len(users):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_usr_{page + 1}"))

    rows = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="📥 Скачать полный список (.txt)", callback_data="admin_export_users")])
    rows.append([InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb(target: str = "schedule") -> InlineKeyboardMarkup:
    today_str = datetime.now(_tz_omsk).strftime("%Y-%m-%d")
    cb = f"show_date_{today_str}" if target == "schedule" else target
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=cb)],
    ])
