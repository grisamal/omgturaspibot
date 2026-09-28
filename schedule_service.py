import re
import aiohttp
import logging
import urllib.parse
from datetime import datetime, timezone, timedelta

from config import SCHEDULE_API, SEARCH_API, OMSK_OFFSET

log = logging.getLogger(__name__)
_tz_omsk = timezone(timedelta(hours=OMSK_OFFSET))

RU_WEEKDAYS = [
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"
]

RU_MONTHS = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]

PAIR_ICONS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]


def extract_subgroup_label(sg_val) -> str | None:
    """Извлекает короткую понятную метку подгруппы: '1 п/г', '2 п/г'."""
    if not sg_val:
        return None
    s = str(sg_val).strip()
    if s.lower() in ["none", "", "0", "null"]:
        return None

    # Если указано со слешем, например "СВКо-261/1" или "ИВТ-241/2"
    if "/" in s:
        tail = s.split("/")[-1].strip()
        if tail.isdigit():
            return f"{tail} п/г"
        return f"{tail} п/г"

    # Если просто цифра "1" или "2"
    if s.isdigit():
        return f"{s} п/г"

    match = re.search(r'(\d+)', s)
    if match:
        return f"{match.group(1)} п/г"

    return s


_http_session: aiohttp.ClientSession | None = None


def get_http_session() -> aiohttp.ClientSession:
    """Возвращает переиспользуемый пул HTTP-соединений к сайту ОмГТУ."""
    global _http_session
    if _http_session is None or _http_session.closed:
        connector = aiohttp.TCPConnector(
            limit=50,
            limit_per_host=15,
            keepalive_timeout=60,
            enable_cleanup_closed=True
        )
        timeout = aiohttp.ClientTimeout(total=8, connect=3)
        _http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _http_session


async def close_http_session():
    """Корректно закрывает пул соединений при выключении бота."""
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        _http_session = None


async def fetch_schedule(group_id: int, date_str: str) -> list[dict]:
    """Получает расписание группы на конкретную дату (YYYY-MM-DD) из API ОмГТУ."""
    url = SCHEDULE_API.format(group_id=group_id, date=date_str)
    try:
        session = get_http_session()
        async with session.get(url) as r:
            if r.status == 200:
                data = await r.json()
                # Сортировка по времени начала и подгруппе
                return sorted(data, key=lambda x: (x.get("beginLesson", ""), str(x.get("subGroup", "")), x.get("discipline", "")))
            log.warning("OmGTU API returned %s for group %s on %s", r.status, group_id, date_str)
            return []
    except Exception as e:
        log.error("Error fetching schedule for group %s: %s", group_id, e)
        return []


LATIN_TO_CYRILLIC = {
    'C': 'С', 'c': 'с', 'B': 'В', 'K': 'К', 'k': 'к', 'O': 'О', 'o': 'о',
    'A': 'А', 'a': 'а', 'E': 'Е', 'e': 'е', 'P': 'Р', 'p': 'р', 'X': 'Х',
    'x': 'х', 'T': 'Т', 'M': 'М', 'H': 'Н', 'y': 'у'
}


def to_cyrillic(text: str) -> str:
    return "".join(LATIN_TO_CYRILLIC.get(ch, ch) for ch in text)


async def _do_search(term: str) -> list[dict]:
    encoded = urllib.parse.quote(term.strip())
    url = SEARCH_API.format(term=encoded)
    try:
        session = get_http_session()
        async with session.get(url) as r:
            if r.status == 200:
                data = await r.json()
                return [item for item in data if item.get("type") == "group"]
            return []
    except Exception as e:
        log.error("Error searching groups for %s: %s", term, e)
        return []


async def search_groups(term: str) -> list[dict]:
    """Поиск учебных групп ОмГТУ по названию (с автоконвертацией случайной латиницы в кириллицу)."""
    clean_term = term.strip()
    res = await _do_search(clean_term)
    if not res:
        cyr_term = to_cyrillic(clean_term)
        if cyr_term != clean_term:
            res = await _do_search(cyr_term)
    return res


def lesson_matches_subgroup(lesson: dict, user_subgroup: int) -> bool:
    """
    Проверяет, подходит ли пара для выбранной подгруппы пользователя (1 или 2).
    0 = обе подгруппы.
    """
    if user_subgroup == 0:
        return True

    sg = lesson.get("subGroup")
    if not sg or str(sg).strip().lower() in ["none", "", "0"]:
        # Общая пара для всей группы
        return True

    sg_str = str(sg).strip()
    # Пример: "СВКо-261/1", "СВКо-261/2", "1", "2"
    if sg_str.endswith(f"/{user_subgroup}") or sg_str.endswith(f" {user_subgroup}") or sg_str == str(user_subgroup):
        return True
    return False


def filter_lessons_by_subgroup(lessons: list[dict], user_subgroup: int) -> list[dict]:
    """Фильтрует список занятий под указанную подгруппу с правильной сортировкой."""
    seen = set()
    filtered = []
    # Сортируем хронологически и по подгруппе, чтобы 1 п/г всегда шла перед 2 п/г
    sorted_lessons = sorted(
        lessons,
        key=lambda x: (x.get("beginLesson", ""), str(x.get("subGroup", "")), x.get("discipline", ""))
    )
    for ls in sorted_lessons:
        if lesson_matches_subgroup(ls, user_subgroup):
            # Избегаем дублей по времени, предмету и подгруппе
            key = (ls.get("beginLesson"), ls.get("discipline"), ls.get("subGroup"), ls.get("auditorium"))
            if key not in seen:
                seen.add(key)
                filtered.append(ls)
    return filtered


def format_day_schedule(lessons: list[dict], date_obj: datetime, group_name: str, subgroup: int) -> str:
    """Форматирует расписание на один день в чистый минималистичный вид."""
    weekday_name = RU_WEEKDAYS[date_obj.weekday()]
    month_name = RU_MONTHS[date_obj.month]
    date_formatted = f"{date_obj.day} {month_name}"

    subgroup_str = f"подгр. {subgroup}" if subgroup in (1, 2) else "вся группа"
    filtered = filter_lessons_by_subgroup(lessons, subgroup)

    header = (
        f"📅 <b>{weekday_name}, {date_formatted}</b>\n"
        f"🎓 <code>{group_name}</code>  •  <b>{subgroup_str}</b>\n"
        f"─────────────────────\n"
    )

    if not filtered:
        return header + "✨ <i>Пар нет — свободный день!</i>"

    blocks = []
    for i, ls in enumerate(filtered):
        # Номер пары: используем официальный номер из ОмГТУ (если есть), либо порядковый
        num = ls.get("lessonNumberStart")
        if isinstance(num, int) and num >= 1:
            idx = num - 1
            icon = PAIR_ICONS[idx] if idx < len(PAIR_ICONS) else f"{num}️⃣"
        else:
            icon = PAIR_ICONS[i] if i < len(PAIR_ICONS) else f"{i+1}️⃣"

        begin = ls.get("beginLesson", "??:??")
        end = ls.get("endLesson", "??:??")
        disc = ls.get("discipline", "Занятие").strip()
        kind = ls.get("kindOfWork", "").strip()
        aud = ls.get("auditorium", "").strip()
        bld = ls.get("building", "").strip()
        lect = ls.get("lecturer", "").strip()
        sg = ls.get("subGroup", "")

        kind_short = ""
        if "лек" in kind.lower():
            kind_short = "Лекция"
        elif "практ" in kind.lower():
            kind_short = "Практика"
        elif "лаб" in kind.lower():
            kind_short = "Лабораторная"
        elif kind:
            kind_short = kind

        loc_str = ""
        if aud and bld:
            loc_str = f"{aud} ({bld})"
        elif aud:
            loc_str = f"ауд. {aud}"
        elif bld:
            loc_str = bld

        meta_items = []
        if loc_str:
            meta_items.append(f"📍 {loc_str}")
        if lect:
            meta_items.append(f"👤 {lect}")

        meta_line = "  •  ".join(meta_items)

        # Метка подгруппы: отображается для пар, которые делятся по подгруппам
        sg_label = extract_subgroup_label(sg)
        sg_badge = f"  •  <b>[{sg_label}]</b>" if sg_label else ""
        type_str = f"  •  <i>{kind_short}</i>" if kind_short else ""

        block = (
            f"{icon} <code>{begin} — {end}</code>{type_str}{sg_badge}\n"
            f"🔹 <b>{disc}</b>\n"
            f"{meta_line}\n"
        )
        blocks.append(block)

    unique_times = len(set(ls.get("beginLesson") for ls in filtered if ls.get("beginLesson")))
    if subgroup == 0 and unique_times < len(filtered):
        footer = f"─────────────────────\nВсего пар: <b>{unique_times}</b> ({len(filtered)} с учётом подгрупп)"
    else:
        footer = f"─────────────────────\nВсего пар: <b>{len(filtered)}</b>"

    return header + "\n".join(blocks) + footer


def compare_schedules(old_lessons: list[dict], new_lessons: list[dict]) -> list[dict]:
    """
    Сравнивает старое и новое расписание на один день.
    Возвращает список обнаруженных изменений:
    - canceled
    - added
    - modified
    """
    changes = []

    def make_key(ls):
        return (ls.get("beginLesson"), ls.get("discipline"), ls.get("subGroup"))

    old_map = {make_key(ls): ls for ls in old_lessons}
    new_map = {make_key(ls): ls for ls in new_lessons}

    # Ищем отменённые
    for k, ls in old_map.items():
        if k not in new_map:
            changes.append({
                "type": "canceled",
                "lesson": ls,
                "subgroup": ls.get("subGroup")
            })

    # Ищем добавленные
    for k, ls in new_map.items():
        if k not in old_map:
            changes.append({
                "type": "added",
                "lesson": ls,
                "subgroup": ls.get("subGroup")
            })
        else:
            # Проверяем изменения аудитории или преподавателя
            old_item = old_map[k]
            modifications = []
            if old_item.get("auditorium") != ls.get("auditorium"):
                modifications.append(f"Аудитория: {old_item.get('auditorium')} ➔ {ls.get('auditorium')}")
            if old_item.get("lecturer") != ls.get("lecturer"):
                modifications.append(f"Преподаватель: {old_item.get('lecturer')} ➔ {ls.get('lecturer')}")

            if modifications:
                changes.append({
                    "type": "modified",
                    "lesson": ls,
                    "subgroup": ls.get("subGroup"),
                    "details": "; ".join(modifications)
                })

    return changes
