import asyncio
import logging
from datetime import datetime, timedelta, timezone
from aiogram import Bot

from config import OMSK_OFFSET, CHECK_INTERVAL_SECONDS
from db import Database
from schedule_service import (
    fetch_schedule, compare_schedules, lesson_matches_subgroup,
    extract_subgroup_label, RU_WEEKDAYS, RU_MONTHS
)

log = logging.getLogger(__name__)
_tz_omsk = timezone(timedelta(hours=OMSK_OFFSET))


def format_change_alert(change: dict) -> str:
    ls = change.get("lesson", {})
    begin = ls.get("beginLesson", "??:??")
    end = ls.get("endLesson", "??:??")
    disc = ls.get("discipline", "Пара")
    kind = ls.get("kindOfWork", "")
    aud = ls.get("auditorium", "")
    lect = ls.get("lecturer", "")
    sg = ls.get("subGroup", "")

    sg_lbl = extract_subgroup_label(sg)
    sg_tag = f" <b>[{sg_lbl}]</b>" if sg_lbl else ""
    kind_str = f" ({kind})" if kind else ""

    c_type = change.get("type")
    if c_type == "canceled":
        return f"❌ <b>ОТМЕНЕНА:</b> {begin}—{end} | {disc}{kind_str}{sg_tag}"
    elif c_type == "added":
        loc = f", ауд. {aud}" if aud else ""
        return f"✅ <b>ДОБАВЛЕНА:</b> {begin}—{end} | {disc}{kind_str}{loc}{sg_tag}"
    elif c_type == "modified":
        details = change.get("details", "")
        return f"🔄 <b>ИЗМЕНЕНИЕ:</b> {begin}—{end} | {disc}{kind_str}\n   👉 <i>{details}</i>"
    return f"ℹ️ <b>Изменение:</b> {begin}—{end} | {disc}"


async def check_group_schedule(group_id: int, group_name: str, db: Database, bot: Bot) -> list[str]:
    """Проверяет расписание для одной группы на сегодня и завтра, рассылает алерты при изменениях."""
    reports = []
    now_omsk = datetime.now(_tz_omsk)
    dates_to_check = [
        now_omsk,
        now_omsk + timedelta(days=1)
    ]

    group_users = await db.get_users_by_group(group_id)
    if not group_users:
        return []

    for dt in dates_to_check:
        date_str = dt.strftime("%Y-%m-%d")
        new_lessons = await fetch_schedule(group_id, date_str)
        old_lessons = await db.get_cached_schedule(group_id, date_str)

        # Если кэш уже был — сравниваем
        if old_lessons is not None:
            changes = compare_schedules(old_lessons, new_lessons)
            if changes:
                log.info(f"Detected {len(changes)} changes for group {group_name} on {date_str}")
                reports.append(f"Группа {group_name} ({date_str}): {len(changes)} изменений")

                weekday_name = RU_WEEKDAYS[dt.weekday()]
                month_name = RU_MONTHS[dt.month]
                date_formatted = f"{dt.day} {month_name}"

                # Рассылаем каждому пользователю этой группы с учетом подгруппы
                for u in group_users:
                    if u.get("notify_changes", 1) != 1:
                        continue

                    user_sg = u.get("subgroup", 1)
                    # Фильтруем изменения, которые касаются подгруппы пользователя
                    user_changes = [
                        c for c in changes
                        if lesson_matches_subgroup(c.get("lesson", {}), user_sg)
                    ]

                    if user_changes:
                        sg_lbl = f"{user_sg} подгруппа" if user_sg in (1, 2) else "вся группа"
                        alert_lines = [
                            f"⚠️ <b>Внимание! Изменение в расписании на {date_formatted} ({weekday_name})!</b>",
                            f"Группа: <b>{group_name}</b> | <b>{sg_lbl}</b>\n"
                        ]
                        for c in user_changes:
                            alert_lines.append(format_change_alert(c))

                        alert_text = "\n".join(alert_lines)
                        try:
                            await bot.send_message(u["user_id"], alert_text, parse_mode="HTML")
                        except Exception as e:
                            log.warning(f"Failed to send alert to user {u['user_id']}: {e}")
                        await asyncio.sleep(0.04)

        # Обновляем кэш в БД
        await db.set_cached_schedule(group_id, date_str, new_lessons)

    return reports


async def run_sync_all_groups(db: Database, bot: Bot) -> list[str]:
    """Запускает проверку расписания для всех групп в базе данных."""
    distinct_groups = await db.get_distinct_groups()
    all_reports = []
    log.info(f"Starting schedule sync for {len(distinct_groups)} groups...")
    for g in distinct_groups:
        gid = g["group_id"]
        gname = g["group_name"] or str(gid)
        try:
            reps = await check_group_schedule(gid, gname, db, bot)
            all_reports.extend(reps)
        except Exception as e:
            log.error(f"Error checking group {gid}: {e}")
        await asyncio.sleep(1)  # Защита от спама запросов к API ОмГТУ
    log.info(f"Sync complete. Found changes: {len(all_reports)}")
    return all_reports


async def schedule_checker_loop(db: Database, bot: Bot):
    """Фоновый цикл: каждый час сверяет расписание с сайтом ОмГТУ."""
    # Ждём 10 секунд после старта перед первой синхронизацией
    await asyncio.sleep(10)
    while True:
        try:
            log.info("Hourly schedule check started...")
            await run_sync_all_groups(db, bot)
            log.info(f"Hourly check finished. Sleeping for {CHECK_INTERVAL_SECONDS} seconds.")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Error in schedule checker loop: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
