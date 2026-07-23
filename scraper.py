"""Скрейпер публичного Telegram-канала через веб-превью t.me/s/<channel>.

Складывает сырые сообщения в SQLite (таблица raw_messages).
Инкрементальный: при повторном запуске догружает только новые посты.
Для полной перезагрузки истории: python3 scraper.py --full
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CHANNEL = "FRPokerClub"
BASE_URL = f"https://t.me/s/{CHANNEL}"
DB_PATH = Path(__file__).parent / "frpoker.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS raw_messages (
            msg_id INTEGER PRIMARY KEY,
            date_utc TEXT,          -- ISO 8601 из атрибута datetime
            text TEXT               -- текст поста (переносы строк сохранены)
        )"""
    )
    conn.commit()


def parse_page(html: str) -> list[dict]:
    """Извлекает сообщения из одной страницы t.me/s."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for wrap in soup.select(".tgme_widget_message"):
        post = wrap.get("data-post", "")  # "FRPokerClub/123"
        try:
            msg_id = int(post.rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue

        time_el = wrap.select_one("time[datetime]")
        date_utc = time_el["datetime"] if time_el else None

        text_el = wrap.select_one(".tgme_widget_message_text")
        if text_el:
            # <br> -> \n, чтобы сохранить построчную структуру поста
            for br in text_el.find_all("br"):
                br.replace_with("\n")
            text = text_el.get_text()
        else:
            text = ""

        out.append({"msg_id": msg_id, "date_utc": date_utc, "text": text})
    return out


def fetch_page(before: int | None) -> list[dict]:
    url = BASE_URL if before is None else f"{BASE_URL}?before={before}"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return parse_page(resp.text)


def scrape(conn: sqlite3.Connection, full: bool) -> int:
    known_max = conn.execute("SELECT COALESCE(MAX(msg_id), 0) FROM raw_messages").fetchone()[0]
    stop_at = 0 if full else known_max

    added = 0
    before: int | None = None
    while True:
        msgs = fetch_page(before)
        if not msgs:
            break
        for m in msgs:
            cur = conn.execute(
                "INSERT OR REPLACE INTO raw_messages (msg_id, date_utc, text) VALUES (?, ?, ?)",
                (m["msg_id"], m["date_utc"], m["text"]),
            )
            added += cur.rowcount
        conn.commit()

        min_id = min(m["msg_id"] for m in msgs)
        print(f"страница до id={before or 'последние'}: {len(msgs)} постов, min_id={min_id}")
        if min_id <= stop_at + 1 or min_id <= 1:
            break
        before = min_id
        time.sleep(1)  # вежливая пауза
    return added


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="выкачать всю историю заново")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    added = scrape(conn, args.full)
    total = conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0]
    print(f"готово: обработано {added} постов, всего в базе {total}")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
