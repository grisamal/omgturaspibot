import asyncio
import logging
from datetime import datetime, timedelta, timezone
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from config import OWNER_ID, OMSK_OFFSET, DB_PATH
from db import Database
from schedule_service import (
    fetch_schedule, search_groups, format_day_schedule,
    filter_lessons_by_subgroup, extract_subgroup_label,
    RU_WEEKDAYS, RU_MONTHS
)
import keyboards as kb

log = logging.getLogger(__name__)
router = Router()
_tz_omsk = timezone(timedelta(hours=OMSK_OFFSET))


class Form(StatesGroup):
    waiting_for_group = State()
    waiting_for_broadcast = State()


def get_now_omsk() -> datetime:
    return datetime.now(_tz_omsk)


# ================= COMMAND SHORTCUTS =================

@router.message(Command("today"))
async def cmd_today(message: Message, db: Database):
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу через команду /start")
        return
    today_str = get_now_omsk().strftime("%Y-%m-%d")
    await render_schedule_screen(message, user, db, today_str, is_edit=False)


@router.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message, db: Database):
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу через команду /start")
        return
    tom_str = (get_now_omsk() + timedelta(days=1)).strftime("%Y-%m-%d")
    await render_schedule_screen(message, user, db, tom_str, is_edit=False)


@router.message(Command("week"))
async def cmd_week(message: Message, db: Database):
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу через команду /start")
        return
    today_str = get_now_omsk().strftime("%Y-%m-%d")
    await render_week_screen(message, user, db, today_str, is_edit=False)


@router.message(Command("settings"))
async def cmd_settings(message: Message, db: Database):
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу через команду /start")
        return
    today_str = get_now_omsk().strftime("%Y-%m-%d")
    text = (
        "⚙️ <b>Настройки профиля:</b>\n\n"
        f"• Группа: <b>{user.get('group_name')}</b>\n"
        f"• Подгруппа: <b>{user.get('subgroup')}</b>\n"
        f"• Оповещения об изменениях: <b>{'Включены ✅' if user.get('notify_changes', 1) else 'Выключены ❌'}</b>\n\n"
        "Нажимай на кнопки ниже для изменения:"
    )
    await message.answer(
        text,
        reply_markup=kb.settings_kb(user.get("group_name", "—"), user.get("subgroup", 1), user.get("notify_changes", 1), today_str),
        parse_mode="HTML"
    )


# ================= ONBOARDING / START =================

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: Database):
    await state.clear()
    uid = message.from_user.id
    uname = message.from_user.username or ""
    fname = message.from_user.first_name or ""

    user = await db.get_or_create_user(uid, uname, fname)

    # Если группа ещё не выбрана — запускаем онбординг
    if not user.get("group_id"):
        await state.set_state(Form.waiting_for_group)
        text = (
            "👋 <b>Добро пожаловать в бот расписания ОмГТУ!</b>\n\n"
            "Я помогу тебе быстро смотреть расписание пар с разделением по подгруппам, "
            "листать дни в 1 клик и мгновенно узнавать о заменах и отменах с сайта.\n\n"
            "🔍 <b>Шаг 1 из 2:</b> Введи название своей группы (например: <code>СВКо-261</code>, <code>ИВТ-231</code>, <code>Э-211</code>):"
        )
        await message.answer(text, parse_mode="HTML")
        return

    # Если группа уже есть — показываем сегодняшнее расписание
    await show_schedule_message(message, user, db, edit=False)


@router.message(Form.waiting_for_group)
@router.message(F.text, ~F.text.startswith("/"))
async def process_group_search(message: Message, state: FSMContext, db: Database):
    current_state = await state.get_state()
    if current_state == Form.waiting_for_broadcast.state:
        await process_broadcast(message, state, db)
        return

    term = message.text.strip()
    uid = message.from_user.id
    uname = message.from_user.username or ""
    fname = message.from_user.first_name or ""
    await db.get_or_create_user(uid, uname, fname)

    status_msg = await message.answer(f"🔍 Ищу группу «{term}» в базе ОмГТУ...", parse_mode="HTML")
    groups = await search_groups(term)
    if not groups:
        await status_msg.edit_text(
            f"❌ Группа «<b>{term}</b>» не найдена на сайте ОмГТУ.\n\n"
            f"Попробуй ввести только шифр (например: <code>СВКо</code>, <code>ИВТ</code> или <code>ФИТ</code>):",
            parse_mode="HTML"
        )
        return

    await state.clear()
    await status_msg.edit_text(
        f"🔍 Найдено групп: <b>{len(groups)}</b>. Выбери свою из списка:",
        reply_markup=kb.group_search_results_kb(groups),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "onboard_group_retry")
async def cb_group_retry(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_for_group)
    text = "🔍 Введи название своей группы (например: <code>СВКо-261</code>):"
    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("sel_grp_"))
async def cb_sel_grp(callback: CallbackQuery, db: Database):
    parts = callback.data.split("_")
    gid = int(parts[2])
    label = "_".join(parts[3:])

    uid = callback.from_user.id
    await db.set_user_group(uid, gid, label)

    text = (
        f"✅ Выбрана группа: <b>{label}</b>!\n\n"
        f"👥 <b>Шаг 2 из 2:</b> Выбери свою подгруппу (чтобы видеть только свои практические и лабораторные):"
    )
    await callback.message.edit_text(text, reply_markup=kb.subgroup_select_kb("onboard_sg"), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("onboard_sg_"))
async def cb_onboard_sg(callback: CallbackQuery, db: Database):
    sg = int(callback.data.split("_")[2])
    uid = callback.from_user.id
    await db.set_user_subgroup(uid, sg)

    user = await db.get_user(uid)
    sg_label = f"{sg} подгруппа" if sg in (1, 2) else "вся группа"

    # Уведомляем владельца о новом студенте и его группе
    if uid != OWNER_ID:
        u_tag = f"@{user.get('username')}" if user.get("username") else f"ID: {uid}"
        fn = user.get("first_name", "")
        try:
            await callback.bot.send_message(
                OWNER_ID,
                f"👤 <b>Новый студент в боте!</b>\n"
                f"• Юзер: <b>{u_tag}</b> ({fn})\n"
                f"• Группа: <code>{user.get('group_name')}</code> ({sg_label})\n"
                f"• ID: <code>{uid}</code>",
                parse_mode="HTML"
            )
        except Exception as e:
            log.warning(f"Could not notify owner: {e}")

    await callback.answer(f"Подгруппа сохранена: {sg_label}!")
    # Отображаем расписание на сегодня
    today_str = get_now_omsk().strftime("%Y-%m-%d")
    await render_schedule_screen(callback.message, user, db, today_str, is_edit=True)


# ================= SCHEDULE DISPLAY & NAVIGATION =================

async def show_schedule_message(msg: Message, user: dict, db: Database, edit: bool = False):
    today_str = get_now_omsk().strftime("%Y-%m-%d")
    await render_schedule_screen(msg, user, db, today_str, is_edit=edit)


async def render_schedule_screen(msg: Message, user: dict, db: Database, date_str: str, is_edit: bool = True):
    gid = user["group_id"]
    gname = user.get("group_name") or "Группа"
    sg = user.get("subgroup", 1)
    uid = user["user_id"]

    # 1. Сначала проверяем кэш БД
    lessons = await db.get_cached_schedule(gid, date_str)
    if lessons is None:
        # 2. Если нет в кэше — запрашиваем API ОмГТУ
        lessons = await fetch_schedule(gid, date_str)
        await db.set_cached_schedule(gid, date_str, lessons)

    date_obj = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_tz_omsk)
    text = format_day_schedule(lessons, date_obj, gname, sg)
    is_owner = (uid == OWNER_ID)
    markup = kb.schedule_nav_kb(date_str, is_owner=is_owner)

    if is_edit:
        try:
            await msg.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    else:
        await msg.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("show_date_"))
async def cb_show_date(callback: CallbackQuery, db: Database):
    date_str = callback.data.replace("show_date_", "")
    user = await db.get_user(callback.from_user.id)
    if not user or not user.get("group_id"):
        await callback.answer("Сначала выбери группу через /start", show_alert=True)
        return
    await render_schedule_screen(callback.message, user, db, date_str, is_edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("refresh_"))
async def cb_refresh(callback: CallbackQuery, db: Database):
    date_str = callback.data.replace("refresh_", "")
    user = await db.get_user(callback.from_user.id)
    if not user or not user.get("group_id"):
        return

    # Принудительно запрашиваем свежее с сайта ОмГТУ
    fresh_lessons = await fetch_schedule(user["group_id"], date_str)
    await db.set_cached_schedule(user["group_id"], date_str, fresh_lessons)

    await render_schedule_screen(callback.message, user, db, date_str, is_edit=True)
    await callback.answer("✅ Расписание обновлено с сайта ОмГТУ!")


# ================= DAY PICKER =================

@router.callback_query(F.data.startswith("picker_"))
async def cb_picker(callback: CallbackQuery):
    base_date_str = callback.data.replace("picker_", "")
    text = "📋 <b>Выбери интересующий день недели:</b>"
    await callback.message.edit_text(text, reply_markup=kb.day_picker_kb(base_date_str), parse_mode="HTML")
    await callback.answer()


# ================= WEEK VIEW =================

@router.callback_query(F.data.startswith("week_view_"))
async def cb_week_view(callback: CallbackQuery, db: Database):
    date_str = callback.data.replace("week_view_", "")
    user = await db.get_user(callback.from_user.id)
    if not user or not user.get("group_id"):
        return
    await render_week_screen(callback.message, user, db, date_str, is_edit=True)
    await callback.answer()


async def render_week_screen(msg: Message, user: dict, db: Database, date_str: str, is_edit: bool = True):
    gid = user["group_id"]
    gname = user.get("group_name", "Группа")
    sg = user.get("subgroup", 1)

    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_tz_omsk)
    # Находим понедельник этой недели
    monday = dt - timedelta(days=dt.weekday())
    monday_str = monday.strftime("%Y-%m-%d")

    subgroup_str = f"подгр. {sg}" if sg in (1, 2) else "вся группа"
    lines = [
        f"🗓 <b>Расписание на неделю ({monday.strftime('%d.%m')} — {(monday + timedelta(days=5)).strftime('%d.%m')})</b>",
        f"🎓 <code>{gname}</code>  •  <b>{subgroup_str}</b>\n"
    ]

    for i in range(6):  # Пн - Сб
        day_dt = monday + timedelta(days=i)
        d_str = day_dt.strftime("%Y-%m-%d")
        w_name = RU_WEEKDAYS[i]

        lessons = await db.get_cached_schedule(gid, d_str)
        if lessons is None:
            lessons = await fetch_schedule(gid, d_str)
            await db.set_cached_schedule(gid, d_str, lessons)

        filtered = filter_lessons_by_subgroup(lessons, sg)
        if filtered:
            lines.append(f"📌 <b>{w_name} ({day_dt.strftime('%d.%m')}):</b>")
            for ls in filtered:
                b = ls.get("beginLesson", "")
                e = ls.get("endLesson", "")
                d = ls.get("discipline", "")
                a = ls.get("auditorium", "")
                aud_str = f" [{a}]" if a else ""
                sg_val = ls.get("subGroup", "")
                sg_lbl = extract_subgroup_label(sg_val)
                sg_str = f" <b>[{sg_lbl}]</b>" if sg_lbl else ""
                lines.append(f"• <code>{b}-{e}</code> {d}{aud_str}{sg_str}")
            lines.append("")
        else:
            lines.append(f"⚪️ <b>{w_name} ({day_dt.strftime('%d.%m')}):</b> Пар нет\n")

    text = "\n".join(lines)
    markup = kb.week_nav_kb(monday_str)
    if is_edit:
        try:
            await msg.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    else:
        await msg.answer(text, reply_markup=markup, parse_mode="HTML")


# ================= SETTINGS =================

@router.callback_query(F.data == "settings_menu")
async def cb_settings_menu(callback: CallbackQuery, db: Database):
    user = await db.get_user(callback.from_user.id)
    if not user:
        return
    today_str = get_now_omsk().strftime("%Y-%m-%d")
    text = (
        "⚙️ <b>Настройки профиля:</b>\n\n"
        f"• Группа: <b>{user.get('group_name')}</b>\n"
        f"• Подгруппа: <b>{user.get('subgroup')}</b>\n"
        f"• Оповещения об изменениях: <b>{'Включены ✅' if user.get('notify_changes', 1) else 'Выключены ❌'}</b>\n\n"
        "Нажимай на кнопки ниже для изменения:"
    )
    await callback.message.edit_text(
        text,
        reply_markup=kb.settings_kb(user.get("group_name", "—"), user.get("subgroup", 1), user.get("notify_changes", 1), today_str),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "change_group_start")
async def cb_change_group_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_for_group)
    text = "🔍 Введи название новой группы ОмГТУ (например: <code>СВКо-261</code>):"
    await callback.message.edit_text(text, reply_markup=kb.cancel_kb("settings_menu"), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "change_subgroup_start")
async def cb_change_subgroup_start(callback: CallbackQuery):
    text = "👥 <b>Выбери свою подгруппу:</b>"
    await callback.message.edit_text(text, reply_markup=kb.subgroup_select_kb("settings_sg"), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("settings_sg_"))
async def cb_settings_sg(callback: CallbackQuery, db: Database):
    sg = int(callback.data.split("_")[2])
    uid = callback.from_user.id
    await db.set_user_subgroup(uid, sg)
    user = await db.get_user(uid)
    if uid != OWNER_ID and user:
        u_tag = f"@{user.get('username')}" if user.get("username") else f"ID: {uid}"
        fn = user.get("first_name", "")
        try:
            await callback.bot.send_message(
                OWNER_ID,
                f"⚙️ <b>Студент сменил подгруппу!</b>\n"
                f"• Юзер: <b>{u_tag}</b> ({fn})\n"
                f"• Группа: <code>{user.get('group_name')}</code> (подгруппа {sg})\n"
                f"• ID: <code>{uid}</code>",
                parse_mode="HTML"
            )
        except Exception as e:
            log.warning(f"Could not notify owner: {e}")
    await callback.answer("Подгруппа успешно изменена!")
    await cb_settings_menu(callback, db)


@router.callback_query(F.data == "toggle_notify")
async def cb_toggle_notify(callback: CallbackQuery, db: Database):
    uid = callback.from_user.id
    user = await db.get_user(uid)
    new_val = 0 if user.get("notify_changes", 1) == 1 else 1
    await db.set_user_notify(uid, new_val)
    status_text = "включены" if new_val == 1 else "выключены"
    await callback.answer(f"Уведомления {status_text}!")
    await cb_settings_menu(callback, db)


# ================= SECRET ADMIN PANEL =================

def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    text = (
        "👑 <b>Секретная Панель Администратора</b>\n\n"
        "Эта панель доступна <b>только тебе</b>. Ни один пользователь бота о ней не знает.\n\n"
        "Выбери действие:"
    )
    await message.answer(text, reply_markup=kb.admin_menu_kb(), parse_mode="HTML")


@router.callback_query(F.data == "admin_menu")
async def cb_admin_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    text = (
        "👑 <b>Секретная Панель Администратора</b>\n\n"
        "Выбери нужное действие:"
    )
    await callback.message.edit_text(text, reply_markup=kb.admin_menu_kb(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery, db: Database):
    if not is_admin(callback.from_user.id):
        return
    stats = await db.get_stats()
    top_str = "\n".join([f"• <b>{g['group_name']}</b>: {g['user_count']} чел." for g in stats["top_groups"]])
    if not top_str:
        top_str = "Пока нет данных"

    text = (
        "📊 <b>Статистика бота:</b>\n\n"
        f"👥 Всего пользователей: <b>{stats['total_users']}</b>\n"
        f"⚡️ Активных за 24 часа: <b>{stats['active_today']}</b>\n"
        f"🎓 Отслеживаемых групп ОмГТУ: <b>{stats['total_groups']}</b>\n\n"
        f"<b>Топ популярных групп:</b>\n{top_str}"
    )
    await callback.message.edit_text(text, reply_markup=kb.admin_menu_kb(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_usr_"))
async def cb_admin_users(callback: CallbackQuery, db: Database):
    if not is_admin(callback.from_user.id):
        return
    page = int(callback.data.split("_")[2])
    per_page = 10
    users = await db.get_all_users()

    if not users:
        await callback.message.edit_text("👥 Пользователей пока нет.", reply_markup=kb.admin_menu_kb())
        await callback.answer()
        return

    total_pages = max(1, (len(users) + per_page - 1) // per_page)
    offset = page * per_page
    page_users = users[offset:offset + per_page]

    lines = [
        f"👥 <b>Список студентов и групп</b> (всего: {len(users)})",
        f"Страница {page + 1}/{total_pages}\n"
    ]

    for i, u in enumerate(page_users, start=offset + 1):
        uname = f"@{u['username']}" if u.get('username') else f"ID: {u['user_id']}"
        fname = f" ({u['first_name']})" if u.get('first_name') else ""
        gname = u.get('group_name') or "Без группы"
        sg = f" (п/г {u['subgroup']})" if u.get('subgroup') in (1, 2) else ""
        lines.append(f"{i}. <b>{uname}</b>{fname} ➔ <code>{gname}</code>{sg}")

    text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=kb.admin_users_list_kb(users, page=page, per_page=per_page), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_export_users")
async def cb_admin_export_users(callback: CallbackQuery, db: Database):
    if not is_admin(callback.from_user.id):
        return
    users = await db.get_all_users()
    lines = [
        "СПИСОК СТУДЕНТОВ И ГРУПП ОМГТУ",
        "Сформировано: " + datetime.now(_tz_omsk).strftime("%Y-%m-%d %H:%M:%S"),
        "Всего пользователей: " + str(len(users)),
        "=" * 55,
        ""
    ]
    for i, u in enumerate(users, 1):
        uname = f"@{u['username']}" if u.get('username') else "нет_юзернейма"
        fname = u.get('first_name') or ""
        gname = u.get('group_name') or "Не выбрана"
        sg = f"подгруппа {u['subgroup']}" if u.get('subgroup') in (1, 2) else "вся группа"
        uid = u['user_id']
        created = u.get('created_at', '')
        lines.append(f"{i}. {uname} ({fname}) — Группа: {gname} ({sg}) | ID: {uid} | Регистрация: {created}")

    content = "\n".join(lines).encode("utf-8")
    doc = BufferedInputFile(content, filename="students_list.txt")
    await callback.message.answer_document(doc, caption="📋 Полный список студентов и привязанных групп (.txt)")
    await callback.answer("Файл со списком отправлен!")


@router.callback_query(F.data == "admin_sync_now")
async def cb_admin_sync_now(callback: CallbackQuery, db: Database):
    if not is_admin(callback.from_user.id):
        return
    from notifier import run_sync_all_groups
    await callback.answer("Запускаю сверку расписания со всеми группами...")
    status_msg = await callback.message.edit_text("⏳ <b>Сверяю расписание с сайтом ОмГТУ...</b>", parse_mode="HTML")

    reports = await run_sync_all_groups(db, callback.bot)
    rep_text = "\n".join(reports) if reports else "Все расписания соответствуют сайту, изменений нет."

    text = (
        f"✅ <b>Сверка завершена!</b>\n\n"
        f"<b>Результаты:</b>\n{rep_text}"
    )
    await status_msg.edit_text(text, reply_markup=kb.admin_menu_kb(), parse_mode="HTML")


@router.callback_query(F.data == "admin_update_vpn")
async def cb_admin_update_vpn(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer("⏳ Обновляю VPN...")
    status_msg = await callback.message.edit_text(
        "⏳ <b>Обновляю VPN-подписку (Xray)...</b>\n\n"
        "Скачиваю свежие ноды и тестирую соединение с Telegram...",
        parse_mode="HTML"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "/root/tgbot/venv/bin/python3", "/usr/local/bin/update_vpn_sub.py",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        out = stdout.decode("utf-8", errors="ignore")
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        summary = lines[-1] if lines else "Скрипт выполнен."

        if proc.returncode == 0:
            res_text = (
                f"✅ <b>VPN-подписка успешно обновлена!</b>\n\n"
                f"ℹ️ <i>{summary}</i>"
            )
        else:
            res_text = (
                f"⚠️ <b>Ошибка при обновлении VPN:</b>\n\n"
                f"<code>{summary}</code>\n\n"
                f"Прокси вернулся на предыдущую рабочую конфигурацию."
            )
    except Exception as e:
        res_text = f"❌ Ошибка запуска скрипта VPN: {e}"

    await status_msg.edit_text(res_text, reply_markup=kb.admin_menu_kb(), parse_mode="HTML")


@router.callback_query(F.data == "admin_backup_db")
async def cb_admin_backup_db(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    try:
        with open(DB_PATH, "rb") as f:
            bytes_data = f.read()
        doc = BufferedInputFile(bytes_data, filename="schedule_bot.db")
        await callback.message.answer_document(doc, caption="💾 Бэкап базы данных пользователей и расписания.")
        await callback.answer("Бэкап отправлен!")
    except Exception as e:
        await callback.answer(f"Ошибка бэкапа: {e}", show_alert=True)


@router.callback_query(F.data == "admin_broadcast_start")
async def cb_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(Form.waiting_for_broadcast)
    text = (
        "📢 <b>Рассылка сообщения всем пользователям</b>\n\n"
        "Отправь текст объявления (поддерживается HTML-разметка):"
    )
    await callback.message.edit_text(text, reply_markup=kb.cancel_kb("admin_menu"), parse_mode="HTML")
    await callback.answer()


@router.message(Form.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext, db: Database):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    b_text = message.text or message.html_text or ""
    users = await db.get_all_users()
    status_msg = await message.answer(f"⏳ Отправка сообщения {len(users)} пользователям...", parse_mode="HTML")

    success = 0
    fail = 0
    for u in users:
        uid = u["user_id"]
        try:
            await message.bot.send_message(uid, b_text, parse_mode="HTML")
            success += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await message.bot.send_message(uid, b_text, parse_mode="HTML")
                success += 1
            except Exception:
                fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.04)

    await status_msg.edit_text(
        f"📢 <b>Рассылка завершена!</b>\n\n"
        f"✅ Доставлено: <b>{success}</b>\n"
        f"❌ Ошибок (заблокировали): <b>{fail}</b>",
        reply_markup=kb.admin_menu_kb(),
        parse_mode="HTML"
    )
