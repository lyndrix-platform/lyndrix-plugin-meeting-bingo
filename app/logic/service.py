"""Meeting Bingo — game state + logic (single source of truth).

All game state lives in this service's in-process ``state`` dict (sessions are
ephemeral; only the scoreboard is persisted to Vault). The React bundle drives
it over the HTTP router in ``app/api.py``; keeping the rules here (``check_win``,
scoring) means the UI never re-implements game logic.
"""
import os
import json
import time
import random
from uuid import uuid4
from typing import Any, Dict, List, Optional

# Plugin root = .../<plugin>/app/logic/service.py → up three levels.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
_TERMS_FILE = os.path.join(_ROOT, "terms.txt")

DEFAULT_TERMS: List[str] = [
    "Synergieeffekte", "Wir müssen das agil angehen", "Könnt ihr mich hören?",
    "Ich teile mal meinen Bildschirm", "Werfe ich mal in die Runde", "Lass uns das offline besprechen",
    "Das ist ein valider Punkt", "Da bin ich ganz bei dir", "Können wir das kurz parken?",
    "Haben wir noch genug Puffer?", "Win-Win Situation", "Wir müssen die PS auf die Straße bringen",
    "Da müssen wir nochmal tiefer reinbohren", "Ich war auf Mute", "Sorry, ich hatte Verbindungsprobleme",
    "Nehmen wir mal als Action Item mit", "Best Practice", "Low Hanging Fruits",
    "Das steht nicht auf der Agenda", "Quick Win", "KPIs", "Out of the box", "Am Ende des Tages",
    "Ich muss gleich in den nächsten Call", "Lasst uns das skalieren",
]

SARCASTIC_MESSAGES: List[str] = [
    "Knapp vorbei ist auch 2. Platz.",
    "Toll gemacht, du bist der erste Verlierer.",
    "Schön für dich. Der Keks ist aber schon weg.",
    "Immerhin hast du teilgenommen.",
    "Silbermedaille im Bullshit-Bingo. Glückwunsch?",
    "Die Goldmedaille für 'Zuspätkommen' geht an dich.",
]


def check_win(board: List[Dict[str, Any]], size: int) -> bool:
    """Row, column, or either diagonal fully marked."""
    for i in range(size):
        if all(board[i * size + j]["marked"] for j in range(size)):
            return True
    for j in range(size):
        if all(board[i * size + j]["marked"] for i in range(size)):
            return True
    if all(board[i * size + i]["marked"] for i in range(size)):
        return True
    if all(board[i * size + (size - 1 - i)]["marked"] for i in range(size)):
        return True
    return False


class BingoService:
    def __init__(self) -> None:
        self.state: Dict[str, Any] = {
            "sessions": {},
            "scoreboard_enabled": False,
            "scoreboard": {},
            "lobby_last_update": time.time(),
        }
        self._ctx = None

    def bind(self, ctx) -> None:
        self._ctx = ctx

    # ── Terms ────────────────────────────────────────────────────────────────
    def default_terms(self) -> List[str]:
        try:
            with open(_TERMS_FILE, "r", encoding="utf-8") as f:
                terms = [line.strip() for line in f if line.strip()]
            if terms:
                return terms
        except OSError:
            pass
        return list(DEFAULT_TERMS)

    def ensure_terms_file(self) -> None:
        if not os.path.exists(_TERMS_FILE):
            try:
                os.makedirs(os.path.dirname(_TERMS_FILE), exist_ok=True)
                with open(_TERMS_FILE, "w", encoding="utf-8") as f:
                    f.write("\n".join(DEFAULT_TERMS))
            except OSError:
                pass

    # ── Vault persistence (scoreboard only) ─────────────────────────────────
    # NB: get_secret/set_secret are synchronous (hvac) Vault round-trips. Callers
    # in async handlers must offload these via asyncio.to_thread (see app/api.py).
    def load_from_vault(self) -> None:
        if self._ctx is None:
            return
        # Symmetric restore: assign BOTH True and False from Vault. The old
        # `if secret == "True"` one-armed check silently kept the __init__
        # default (False) whenever the read missed, so the checkbox reset on
        # every reboot. Tolerate bool values too (Vault KV may round-trip the
        # native type); only a missing secret keeps the current state.
        raw = self._ctx.get_secret("scoreboard_enabled")
        if raw is not None:
            self.state["scoreboard_enabled"] = raw in (True, "True")
        stored = self._ctx.get_secret("bingo_scoreboard")
        if stored:
            try:
                self.state["scoreboard"] = json.loads(stored)
            except (json.JSONDecodeError, TypeError) as exc:
                self._ctx.log.error(f"Failed to parse stored scoreboard, resetting: {exc}")
                self.state["scoreboard"] = {}

    def _save_scoreboard(self) -> None:
        if self._ctx is None:
            return
        try:
            self._ctx.set_secret("bingo_scoreboard", json.dumps(self.state["scoreboard"]))
        except Exception as exc:  # noqa: BLE001 - Vault client raises varied errors; log and continue
            self._ctx.log.error(f"Failed to persist scoreboard to Vault: {exc}")

    # ── Settings ─────────────────────────────────────────────────────────────
    def get_settings(self) -> Dict[str, Any]:
        return {"scoreboard_enabled": self.state["scoreboard_enabled"]}

    def set_scoreboard_enabled(self, enabled: bool) -> Dict[str, Any]:
        self.state["scoreboard_enabled"] = bool(enabled)
        if self._ctx is not None:
            try:
                self._ctx.set_secret("scoreboard_enabled", str(bool(enabled)))
            except Exception as exc:  # noqa: BLE001 - Vault client raises varied errors; log and continue
                self._ctx.log.error(f"Failed to persist scoreboard_enabled to Vault: {exc}")
        return self.get_settings()

    # ── Scoreboard ───────────────────────────────────────────────────────────
    def scoreboard(self, limit: int = 10) -> List[Dict[str, Any]]:
        items = sorted(self.state["scoreboard"].items(), key=lambda kv: kv[1], reverse=True)
        return [{"player": p, "wins": w} for p, w in items[:limit]]

    # ── Sessions ─────────────────────────────────────────────────────────────
    def list_sessions(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": sid,
                "name": s["name"],
                "size": s["size"],
                "players": len(s["players"]),
                "winners": len(s["winners"]),
            }
            for sid, s in self.state["sessions"].items()
        ]

    def create_session(self, name: str, size: int, terms: List[str]) -> str:
        name = (name or "").strip()
        size = int(size)
        terms = [t.strip() for t in (terms or []) if t.strip()]
        if not name:
            raise ValueError("Bitte einen Namen vergeben.")
        if size < 3 or size > 5:
            raise ValueError("Feldgröße muss zwischen 3 und 5 liegen.")
        if len(terms) < size * size:
            raise ValueError(f"Du brauchst mindestens {size * size} Begriffe.")
        sid = str(uuid4())[:8]
        self.state["sessions"][sid] = {
            "name": name,
            "size": size,
            "words": terms,
            "winners": [],
            "players": {},
            "last_update": time.time(),
        }
        self.state["lobby_last_update"] = time.time()
        return sid

    def join_session(self, sid: str, nick: str) -> None:
        nick = (nick or "").strip()
        if not nick:
            raise ValueError("Nickname fehlt.")
        sess = self.state["sessions"].get(sid)
        if sess is None:
            raise KeyError(sid)
        if nick not in sess["players"]:
            sample = random.sample(sess["words"], sess["size"] * sess["size"])
            board = [{"word": w, "marked": False} for w in sample]
            sess["players"][nick] = {"board": board, "won": False}
            sess["last_update"] = time.time()
            self.state["lobby_last_update"] = time.time()

    def mark_cell(self, sid: str, nick: str, index: int) -> Dict[str, Any]:
        """Toggle a cell; on a fresh bingo, record the win + scoreboard.

        Returns {won, first_winner, sarcastic} so the UI can fire confetti/toast.
        """
        sess = self.state["sessions"].get(sid)
        if sess is None:
            raise KeyError(sid)
        player = sess["players"].get(nick)
        if player is None:
            raise KeyError(nick)

        result: Dict[str, Any] = {"won": False, "first_winner": False, "sarcastic": None}
        if player["won"]:
            result["won"] = True
            return result

        board = player["board"]
        if index < 0 or index >= len(board):
            raise ValueError("Ungültiger Index.")
        board[index]["marked"] = not board[index]["marked"]

        if check_win(board, sess["size"]):
            player["won"] = True
            result["won"] = True
            if nick not in sess["winners"]:
                sess["winners"].append(nick)
                if len(sess["winners"]) == 1:
                    result["first_winner"] = True
                    if self.state["scoreboard_enabled"]:
                        self.state["scoreboard"][nick] = self.state["scoreboard"].get(nick, 0) + 1
                        self._save_scoreboard()
                else:
                    result["sarcastic"] = random.choice(SARCASTIC_MESSAGES)

        sess["last_update"] = time.time()
        return result

    def session_view(self, sid: str, nick: str) -> Dict[str, Any]:
        sess = self.state["sessions"].get(sid)
        if sess is None:
            raise KeyError(sid)
        size = sess["size"]
        you = sess["players"].get(nick)
        others: List[Dict[str, Any]] = []
        for pn, pd in sess["players"].items():
            if pn == nick:
                continue
            marks = sum(1 for c in pd["board"] if c["marked"])
            place: Optional[int] = (
                sess["winners"].index(pn) + 1 if pd["won"] and pn in sess["winners"] else None
            )
            others.append(
                {
                    "nick": pn,
                    "marks": marks,
                    "total": size * size,
                    "won": pd["won"],
                    "place": place,
                    "board": pd["board"],
                }
            )
        return {
            "id": sid,
            "name": sess["name"],
            "size": size,
            "you": {
                "nick": nick,
                "board": you["board"] if you else None,
                "won": you["won"] if you else False,
                "joined": you is not None,
            },
            "others": others,
            "winners": sess["winners"],
            "scoreboard_enabled": self.state["scoreboard_enabled"],
        }


bingo_service = BingoService()
