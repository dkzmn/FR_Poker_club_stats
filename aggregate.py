"""Агрегатор: сезонные лидерборды, достижения игроков -> docs/data.json."""

import json
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = ROOT / "frpoker.db"
OUT_PATH = ROOT / "docs" / "data.json"

SEASONS = json.loads((ROOT / "seasons.json").read_text())
ALIASES_RAW = json.loads((ROOT / "aliases.json").read_text())


def norm_key(name: str) -> str:
    """Ключ для сопоставления имён: без регистра, пробелов, ё->е."""
    s = unicodedata.normalize("NFKC", name).casefold().replace("ё", "е")
    return "".join(s.split())


ALIASES = {norm_key(k): v for k, v in ALIASES_RAW.items() if not k.startswith("_")}


class PlayerRegistry:
    """Сопоставляет сырые имена каноническим; display-имя — самое частое написание."""

    def __init__(self) -> None:
        self.spellings: dict[str, defaultdict[str, int]] = {}

    def canon_key(self, raw: str) -> str:
        raw = raw.strip()
        if norm_key(raw) in ALIASES:
            raw = ALIASES[norm_key(raw)]
        key = norm_key(raw)
        self.spellings.setdefault(key, defaultdict(int))[raw] += 1
        return key

    def display(self, key: str) -> str:
        return max(self.spellings[key].items(), key=lambda kv: kv[1])[0]


def season_for(date_str: str) -> dict | None:
    d = date_str[:10]
    matched = None
    for s in SEASONS:
        if d >= s["start"]:
            matched = s
    if matched and d > matched["end"]:
        print(f"[warn] дата {d} позже конца сезона {matched['id']} — обновите seasons.json", file=sys.stderr)
    return matched


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    reg = PlayerRegistry()

    tournaments = []
    for msg_id, date_utc, name, is_final, bh, bh_kills in conn.execute(
        "SELECT msg_id, date_utc, name, is_final, bounty_hunter, bounty_hunter_kills "
        "FROM tournaments ORDER BY date_utc"
    ):
        results = [
            {
                "place": place,
                "player_key": reg.canon_key(raw_name),
                "kills": kills,
                "points": points,
            }
            for place, raw_name, kills, points in conn.execute(
                "SELECT place, raw_name, kills, points FROM results WHERE msg_id=? ORDER BY place",
                (msg_id,),
            )
        ]
        season = season_for(date_utc)
        tournaments.append(
            {
                "msg_id": msg_id,
                "date": date_utc[:10],
                "name": name,
                "is_final": bool(is_final),
                "season_id": season["id"] if season else None,
                "bounty_hunter_key": reg.canon_key(bh) if bh else None,
                "bounty_hunter_kills": bh_kills,
                "results": results,
            }
        )

    # --- сезонные агрегаты ---
    seasons_out = []
    for s in SEASONS:
        st = [t for t in tournaments if t["season_id"] == s["id"]]
        regular = [t for t in st if not t["is_final"]]
        finals = [t for t in st if t["is_final"]]

        stats: dict[str, dict] = defaultdict(
            lambda: {"points": 0, "games": 0, "wins": 0, "seconds": 0, "thirds": 0,
                     "podiums": 0, "kills": 0, "bh_titles": 0}
        )
        for t in regular:
            for r in t["results"]:
                p = stats[r["player_key"]]
                p["games"] += 1
                p["points"] += r["points"] or 0
                p["kills"] += r["kills"] or 0
                p["wins"] += r["place"] == 1
                p["seconds"] += r["place"] == 2
                p["thirds"] += r["place"] == 3
                p["podiums"] += r["place"] <= 3
            if t["bounty_hunter_key"]:
                stats[t["bounty_hunter_key"]]["bh_titles"] += 1

        leaderboard = sorted(
            ({"player_key": k, **v} for k, v in stats.items()),
            key=lambda p: (-p["points"], -p["wins"], -p["podiums"], -p["games"]),
        )
        champion = None
        if finals:
            winner = next((r for r in finals[0]["results"] if r["place"] == 1), None)
            champion = winner["player_key"] if winner else None

        seasons_out.append(
            {
                **s,
                "tournaments_count": len(regular),
                "leaderboard": leaderboard,
                "champion_key": champion,
                "final_msg_id": finals[0]["msg_id"] if finals else None,
            }
        )

    # --- карьерные достижения ---
    players: dict[str, dict] = defaultdict(
        lambda: {
            "games": 0, "wins": 0, "seconds": 0, "thirds": 0, "fourths": 0, "podiums": 0,
            "points_total": 0, "max_points": 0, "kills": 0, "bh_titles": 0,
            "deep_wins": 0, "joker_wins": 0, "mafia_wins": 0, "bounty_wins": 0,
            "champion_seasons": [],
            "best_season_rank": None, "first_seen": None, "last_seen": None,
        }
    )
    for t in tournaments:
        tname = (t["name"] or "").upper()
        for r in t["results"]:
            p = players[r["player_key"]]
            if not t["is_final"]:
                p["games"] += 1
                pts = r["points"] or 0
                p["points_total"] += pts
                if pts > p["max_points"]:
                    p["max_points"] = pts
                p["kills"] += r["kills"] or 0
                p["wins"] += r["place"] == 1
                p["seconds"] += r["place"] == 2
                p["thirds"] += r["place"] == 3
                p["fourths"] += r["place"] == 4
                p["podiums"] += r["place"] <= 3
                if r["place"] == 1:
                    if "DEEP" in tname:
                        p["deep_wins"] += 1
                    if "JOK" in tname:
                        p["joker_wins"] += 1
                    if "MAFIA" in tname:
                        p["mafia_wins"] += 1
                    if "BOUNTY" in tname:
                        p["bounty_wins"] += 1
            p["first_seen"] = min(p["first_seen"] or t["date"], t["date"])
            p["last_seen"] = max(p["last_seen"] or t["date"], t["date"])
        if t["bounty_hunter_key"]:
            players[t["bounty_hunter_key"]]["bh_titles"] += 1

    for s in seasons_out:
        for rank, row in enumerate(s["leaderboard"], 1):
            p = players[row["player_key"]]
            if p["best_season_rank"] is None or rank < p["best_season_rank"]:
                p["best_season_rank"] = rank
        if s["champion_key"]:
            players[s["champion_key"]]["champion_seasons"].append(s["id"])

    def badges(p: dict) -> list[str]:
        out = []
        for sid in p["champion_seasons"]:
            out.append(f"🏆 Чемпион сезона {sid}")
        if p["best_season_rank"] == 1:
            out.append("📈 №1 сезона")
        elif p["best_season_rank"] is not None and p["best_season_rank"] <= 3:
            out.append("📈 Топ-3 сезона")
        if p["wins"] >= 5:
            out.append(f"👑 {p['wins']} побед")
        elif p["wins"] >= 1:
            out.append(f"🥇 Побед: {p['wins']}")
        if p["podiums"] >= 10:
            out.append("🏅 Подиумный монстр (10+)")
        if p["seconds"] >= 5:
            out.append(f"🥈 Серебряный запас ({p['seconds']})")
        if p["thirds"] >= 5:
            out.append(f"🥉 Бронзовый фонд ({p['thirds']})")
        if p["fourths"] >= 5:
            out.append(f"🫧 Бабл-бой ({p['fourths']}× 4-е)")
        if p["bh_titles"] >= 1:
            out.append(f"💀 Bounty Hunter ×{p['bh_titles']}")
        if p["kills"] >= 100:
            out.append("☠️ Палач (100+ киллов)")
        elif p["kills"] >= 50:
            out.append("☠️ Жнец (50+ киллов)")
        elif p["kills"] >= 25:
            out.append("☠️ Охотник (25+ киллов)")
        if p["max_points"] >= 3500:
            out.append("💎 Большой улов (3500+ за турнир)")
        if p["deep_wins"] >= 1:
            out.append("💥 Deepstack-победитель")
        if p["joker_wins"] >= 1:
            out.append("🃏 Joker-победитель")
        if p["mafia_wins"] >= 1:
            out.append("🕵️ Mafia-победитель")
        if p["bounty_wins"] >= 1:
            out.append("🥊 Bounty-победитель")
        if p["games"] >= 10 and p["wins"] / p["games"] >= 0.3:
            out.append("📊 Стабильность (30%+ побед)")
        if p["games"] >= 25:
            out.append("🎖 Ветеран клуба (25+ турниров)")
        elif p["games"] >= 10:
            out.append("🎯 Частый гость (10+ турниров)")
        return out

    for p in players.values():
        p["badges"] = badges(p)

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channel": "FRPokerClub",
        "player_names": {k: reg.display(k) for k in reg.spellings},
        "seasons": seasons_out,
        "tournaments": tournaments,
        "players": dict(players),
    }
    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(
        f"сезонов: {len(seasons_out)}, турниров: {len(tournaments)}, "
        f"игроков: {len(players)} -> {OUT_PATH.relative_to(ROOT)}"
    )


if __name__ == "__main__":
    main()
