"""Парсер постов канала: результаты турниров и финалы сезонов.

Читает raw_messages из SQLite, наполняет таблицы tournaments и results.
Все нераспознанные строки внутри блока результатов печатает в stderr,
чтобы изменение формата не прошло незамеченным.
"""

import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "frpoker.db"

# заголовок: "ПОБЕДИТЕЛИ ТУРНИРА <имя>", встречались опечатка "ПОБЕДИТЕ ТУРНИРА"
# и вариант "🏆 Победители:" без слова ТУРНИРА (пост 77)
HEADER_RE = re.compile(
    r"^[👑🏆]?\s*ПОБЕДИТЕ(?:ЛИ)?(?:\s+ТУРНИРА)?:?\s*(?P<name>.*?)\s*$", re.IGNORECASE
)
# строка результата: медаль / номер места / 🌟 / 🩸, затем имя, опционально "(роль)",
# опционально ": N ☠" (иногда ☠️ с variation selector U+FE0F), опционально "- очки"
RESULT_RE = re.compile(
    r"^(?P<place>🥇|🥈|🥉|🌟|🩸|\d{1,2})[\s.]*"
    r"(?P<name>.+?)"
    r"(?:\s*\([^)]*\))?"       # роль в скобках, напр. "(Дон Эладио🔥)" — отбрасываем
    r"(?:\s*:\s*(?P<kills>\d+)\s*☠\uFE0F?)?"
    r"(?:\s*[-–—]\s*(?P<points>\d+))?"
    r"\s*[🔥💥⚡️]*\s*$"
)
MEDAL_PLACE = {"🥇": 1, "🥈": 2, "🥉": 3}
BOUNTY_HUNTER_RE = re.compile(r"Bounty Hunter турнира\s*[-–—]\s*(?P<name>.+?)\s*:\s*(?P<kills>\d+)")
NAME_FROM_TEXT_RE = re.compile(r"Турнир\s+(?P<name>\S+)\s+завершен", re.IGNORECASE)
# пост финала сезона: "ФИНАЛ СЕЗОНА - ЗАВЕРШЕН", далее "Победители:" и строки с медалями
FINAL_HEADER_RE = re.compile(r"ФИНАЛ\s+(?:\S+\s+)?СЕЗОНА\s*[-–—]?\s*ЗАВЕРШЕН", re.IGNORECASE)
# строки, завершающие блок результатов
TERMINATOR_RE = re.compile(r"^(📊|Спасибо|Всем большое спасибо|Друзья)")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS tournaments;
        DROP TABLE IF EXISTS results;
        CREATE TABLE tournaments (
            msg_id INTEGER PRIMARY KEY,
            date_utc TEXT NOT NULL,
            name TEXT,                 -- BOUNTY, DEEPSTACK, ... или NULL у ранних постов
            is_final INTEGER NOT NULL DEFAULT 0,
            bounty_hunter TEXT,        -- сырое имя охотника за головами
            bounty_hunter_kills INTEGER
        );
        CREATE TABLE results (
            msg_id INTEGER NOT NULL REFERENCES tournaments(msg_id),
            place INTEGER NOT NULL,
            raw_name TEXT NOT NULL,    -- имя как в посте, нормализация — в агрегаторе
            kills INTEGER,             -- выбитые игроки (bounty)
            points INTEGER,            -- очки рейтинга; NULL у ранних постов и финалов
            PRIMARY KEY (msg_id, place)
        );
        """
    )


def parse_result_lines(lines: list[str], start: int) -> tuple[list[dict], list[str]]:
    """Читает строки результатов начиная с lines[start] до терминатора/конца.

    Возвращает (результаты, нераспознанные строки)."""
    results: list[dict] = []
    unparsed: list[str] = []
    place = 0
    for line in lines[start:]:
        line = line.strip()
        if not line:
            continue
        if TERMINATOR_RE.match(line) or BOUNTY_HUNTER_RE.search(line):
            break
        m = RESULT_RE.match(line)
        if not m:
            if len(results) >= 3:
                break  # список мест закончился, дальше идёт эпилог поста
            unparsed.append(line)
            continue
        p = m.group("place")
        place = MEDAL_PLACE.get(p) or (int(p) if p.isdigit() else place + 1)
        results.append(
            {
                "place": place,
                "raw_name": m.group("name").strip(),
                "kills": int(m.group("kills")) if m.group("kills") else None,
                "points": int(m.group("points")) if m.group("points") else None,
            }
        )
    return results, unparsed


def parse_tournament(msg_id: int, text: str) -> tuple[dict, list[dict], list[str]] | None:
    lines = text.split("\n")
    header_idx, name = None, None
    for i, line in enumerate(lines):
        m = HEADER_RE.match(line.strip())
        if m:
            header_idx, name = i, m.group("name").strip() or None
            break
    if header_idx is None:
        return None
    if not name:
        m = NAME_FROM_TEXT_RE.search(text)
        name = m.group("name") if m else None

    results, unparsed = parse_result_lines(lines, header_idx + 1)

    bh = BOUNTY_HUNTER_RE.search(text)
    meta = {
        "name": name,
        "is_final": 0,
        "bounty_hunter": bh.group("name").strip() if bh else None,
        "bounty_hunter_kills": int(bh.group("kills")) if bh else None,
    }
    return meta, results, unparsed


def parse_final(msg_id: int, text: str) -> tuple[dict, list[dict], list[str]] | None:
    if not FINAL_HEADER_RE.search(text):
        return None
    lines = text.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("Победители"):
            start = i + 1
            break
    if start is None:
        return None
    results, unparsed = parse_result_lines(lines, start)
    meta = {"name": "ФИНАЛ СЕЗОНА", "is_final": 1, "bounty_hunter": None, "bounty_hunter_kills": None}
    return meta, results, unparsed


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    rows = conn.execute("SELECT msg_id, date_utc, text FROM raw_messages ORDER BY msg_id").fetchall()
    n_tournaments = n_results = 0
    for msg_id, date_utc, text in rows:
        # сначала финал: его пост тоже содержит строку "Победители:"
        parsed = parse_final(msg_id, text) or parse_tournament(msg_id, text)
        if not parsed:
            continue
        meta, results, unparsed = parsed
        if not results:
            print(f"[warn] пост {msg_id}: заголовок есть, результаты не распознаны", file=sys.stderr)
            continue
        for line in unparsed:
            print(f"[warn] пост {msg_id}: нераспознанная строка: {line!r}", file=sys.stderr)
        conn.execute(
            "INSERT INTO tournaments VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, date_utc, meta["name"], meta["is_final"],
             meta["bounty_hunter"], meta["bounty_hunter_kills"]),
        )
        conn.executemany(
            "INSERT INTO results VALUES (?, ?, ?, ?, ?)",
            [(msg_id, r["place"], r["raw_name"], r["kills"], r["points"]) for r in results],
        )
        n_tournaments += 1
        n_results += len(results)

    conn.commit()
    print(f"турниров: {n_tournaments}, строк результатов: {n_results}")
    conn.close()


if __name__ == "__main__":
    main()
