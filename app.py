
import json
import os
import random
import re
import secrets
import sqlite3
import string
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, session
from flask_socketio import SocketIO, emit, join_room

BASE = Path(__file__).resolve().parent
DATABASE = os.environ.get("DATABASE_PATH", str(BASE / "champion.sqlite3"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", "change-this-secret-for-production"
)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get(
    "COOKIE_SECURE", "0"
) == "1"

socketio = SocketIO(
    app,
    async_mode="threading",
    max_http_buffer_size=2_000_000
)

rooms = {}
room_lock = threading.RLock()


# ---------------- DATENBANK ----------------

def get_db():
    db = sqlite3.connect(DATABASE, timeout=20)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    Path(DATABASE).parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS quizzes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                data TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created INTEGER NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created INTEGER NOT NULL
            )
        """)
        columns = [
            row["name"]
            for row in db.execute("PRAGMA table_info(ideas)")
        ]
        if "completed" not in columns:
            db.execute("""
                ALTER TABLE ideas
                ADD COLUMN completed INTEGER NOT NULL DEFAULT 0
            """)


init_db()


# ---------------- HILFSFUNKTIONEN ----------------

def number(value, low, high, default):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def color(value, fallback="#8B5CF6"):
    if isinstance(value, str) and re.fullmatch(
        r"#[0-9a-fA-F]{6}", value
    ):
        return value
    return fallback


def image_ok(value):
    return value == "" or (
        isinstance(value, str)
        and len(value) <= 150000
        and re.fullmatch(
            r"data:image/jpeg;base64,[A-Za-z0-9+/=]+",
            value
        ) is not None
    )


def profile(raw):
    raw = raw if isinstance(raw, dict) else {}
    img = raw.get("image", "")
    return {
        "emojis": str(raw.get("emojis", "😎"))[:32] or "😎",
        "bg": color(raw.get("bg"), "#32215E"),
        "border": color(raw.get("border")),
        "image": img if image_ok(img) else ""
    }


def validate_quiz(raw):
    if not isinstance(raw, dict):
        raise ValueError("Ungültige Quizdaten.")

    title = str(raw.get("title", "")).strip()[:80]
    if not title:
        raise ValueError("Bitte einen Quiznamen eingeben.")

    questions = raw.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 30:
        raise ValueError("Ein Quiz braucht 1 bis 30 Fragen.")

    checked = []

    for index, q in enumerate(questions, 1):
        if not isinstance(q, dict):
            raise ValueError(f"Frage {index} ist ungültig.")

        kind = q.get("type", "choice")
        if kind not in ("choice", "yesno", "truefalse", "estimate", "order"):
            raise ValueError(f"Frage {index}: unbekannter Typ.")

        text = str(q.get("text", "")).strip()[:350]
        if not text:
            raise ValueError(f"Frage {index}: Text fehlt.")

        img = q.get("image", "")
        if not image_ok(img):
            raise ValueError(f"Frage {index}: Bild zu groß.")

        item = {
            "type": kind,
            "text": text,
            "duration": number(q.get("duration"), 5, 120, 15),
            "points": number(q.get("points"), 100, 2000, 500),
            "bonus": bool(q.get("bonus", True)),
            "bg": color(q.get("bg"), "#29204B"),
            "image": img
        }

        if kind == "estimate":
            try:
                target = float(q.get("target", 0))
                tolerance = float(q.get("tolerance", 0))
                if not -1e12 <= target <= 1e12:
                    raise ValueError()
                if not 0 <= tolerance <= 1e12:
                    raise ValueError()
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"Frage {index}: ungültige Schätzung.")

            item["target"] = target
            item["tolerance"] = tolerance

        else:
            if kind == "yesno":
                options = ["Ja", "Nein"]
            elif kind == "truefalse":
                options = ["Richtig", "Falsch"]
            else:
                options = q.get("options")
                if not isinstance(options, list) or not 2 <= len(options) <= 6:
                    raise ValueError(
                        f"Frage {index}: 2 bis 6 Antworten nötig."
                    )
                options = [str(v).strip()[:120] for v in options]
                if any(not v for v in options) or len(set(options)) != len(options):
                    raise ValueError(
                        f"Frage {index}: Antworten ausfüllen und "
                        "unterschiedlich halten."
                    )

            item["options"] = options

            if kind == "order":
                item["correct"] = list(range(len(options)))
            else:
                correct = q.get("correct", [0])
                if (
                    not isinstance(correct, list)
                    or not correct
                    or any(
                        type(i) is not int or i < 0 or i >= len(options)
                        for i in correct
                    )
                ):
                    raise ValueError(
                        f"Frage {index}: richtige Antwort markieren."
                    )
                item["correct"] = sorted(set(correct))

        checked.append(item)

    cover = raw.get("cover", "")
    if not image_ok(cover):
        raise ValueError("Titelbild ist zu groß.")

    difficulty = str(raw.get("difficulty", "Mittel"))
    if difficulty not in ("Leicht", "Mittel", "Schwer"):
        difficulty = "Mittel"

    return {
        "title": title,
        "description": str(raw.get("description", "")).strip()[:300],
        "category": str(raw.get("category", "Allgemein")).strip()[:50]
                    or "Allgemein",
        "difficulty": difficulty,
        "cover": cover,
        "per_round": number(
            raw.get("per_round"), 1, len(checked), len(checked)
        ),
        "ranking_seconds": number(
            raw.get("ranking_seconds"), 2, 15, 5
        ),
        "shuffle": bool(raw.get("shuffle", True)),
        "questions": checked
    }


# ---------------- STANDARD-QUIZZE ----------------

def standard_quiz(title, entries):
    return validate_quiz({
        "title": title,
        "per_round": len(entries),
        "questions": [
            {
                "type": "choice",
                "text": text,
                "options": options,
                "correct": [answer],
                "duration": 15,
                "points": 500,
                "bonus": True
            }
            for text, options, answer in entries
        ]
    })


BUILTIN = {
    "minecraft": standard_quiz("⛏️ Minecraft", [
        ("Welcher Mob explodiert?",
         ["Creeper", "Schaf", "Kuh", "Zombie"], 0),
        ("Wie viele Bretter braucht eine Werkbank?",
         ["2", "4", "6", "8"], 1),
        ("Wo lebt der Enderdrache?",
         ["Nether", "Das Ende", "Dorf", "Höhle"], 1),
        ("Womit zündet man ein Netherportal an?",
         ["Feuerzeug", "Kompass", "Angel", "Schwert"], 0),
        ("Welche Spitzhacke kann Diamanterz abbauen?",
         ["Holz", "Stein", "Eisen", "Gold"], 2)
    ]),
    "wissen": standard_quiz("🧠 Allgemeinwissen", [
        ("Wie viele Minuten hat eine Stunde?",
         ["30", "45", "60", "90"], 2),
        ("Welcher Planet heißt roter Planet?",
         ["Venus", "Mars", "Saturn", "Neptun"], 1),
        ("Was ist die Hauptstadt von Frankreich?",
         ["Berlin", "Paris", "Rom", "Madrid"], 1),
        ("Wie viel ist 7 mal 8?",
         ["54", "56", "64", "48"], 1),
        ("Welcher Ozean ist am größten?",
         ["Atlantik", "Pazifik", "Indischer Ozean", "Arktischer Ozean"], 1)
    ])
}


def available_quizzes():
    result = dict(BUILTIN)
    with get_db() as db:
        rows = db.execute("""
            SELECT id, data FROM quizzes
            WHERE status = 'approved'
        """).fetchall()

    for row in rows:
        result["user-" + str(row["id"])] = json.loads(row["data"])
    return result


# ---------------- WEBSITE UND EINREICHUNGEN ----------------

@app.get("/")
def homepage():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "name": "Class-Champion"})


@app.get("/api/quizzes")
def quiz_list():
    return jsonify([
        {"id": key, "title": value["title"]}
        for key, value in available_quizzes().items()
    ])


@app.post("/api/quizzes")
def submit_quiz():
    try:
        quiz = validate_quiz(request.get_json(silent=True))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    with get_db() as db:
        db.execute("""
            INSERT INTO quizzes (title, data, status, created)
            VALUES (?, ?, 'pending', ?)
        """, (
            quiz["title"],
            json.dumps(quiz, ensure_ascii=False),
            int(time.time())
        ))

    return jsonify({
        "message": "📥 Quiz eingereicht! Der Admin prüft es."
    }), 201


@app.post("/api/ideas")
def submit_idea():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "Gast")).strip()[:40] or "Gast"
    text = str(data.get("text", "")).strip()[:2000]

    if len(text) < 5:
        return jsonify({"error": "Bitte eine längere Idee eingeben."}), 400

    with get_db() as db:
        db.execute("""
            INSERT INTO ideas (name, text, status, created, completed)
            VALUES (?, ?, 'pending', ?, 0)
        """, (name, text, int(time.time())))

    return jsonify({
        "message": "💡 Danke! Deine Idee wartet auf Freigabe."
    }), 201


# ---------------- ADMIN-ANMELDUNG ----------------

def is_admin():
    return (
        bool(os.environ.get("ADMIN_PASSWORD"))
        and session.get("admin") is True
    )


def admin_error():
    return jsonify({"error": "Bitte als Admin anmelden."}), 403


@app.post("/api/admin/login")
def admin_login():
    expected = os.environ.get("ADMIN_PASSWORD", "")
    data = request.get_json(silent=True) or {}
    supplied = str(data.get("password", ""))

    if not expected or not secrets.compare_digest(expected, supplied):
        return jsonify({
            "error": "Falsches Passwort oder ADMIN_PASSWORD fehlt."
        }), 403

    session.clear()
    session["admin"] = True
    return jsonify({"message": "Angemeldet."})


@app.post("/api/admin/logout")
def admin_logout():
    session.clear()
    return jsonify({"message": "Abgemeldet."})


# ---------------- ADMIN-DATEN ----------------

@app.get("/api/admin/items")
def admin_items():
    if not is_admin():
        return admin_error()

    with get_db() as db:
        quizzes = [
            dict(row)
            for row in db.execute("""
                SELECT id, title, data, status, created
                FROM quizzes ORDER BY id DESC
            """)
        ]
        ideas = [
            dict(row)
            for row in db.execute("""
                SELECT id, name, text, status, created, completed
                FROM ideas ORDER BY id DESC
            """)
        ]

    for quiz in quizzes:
        quiz["data"] = json.loads(quiz["data"])

    return jsonify({"quizzes": quizzes, "ideas": ideas})


@app.get("/api/admin/dashboard")
def admin_dashboard():
    if not is_admin():
        return admin_error()

    with get_db() as db:
        quiz_rows = db.execute("""
            SELECT status, COUNT(*) AS amount
            FROM quizzes GROUP BY status
        """).fetchall()
        idea_rows = db.execute("""
            SELECT status, COUNT(*) AS amount
            FROM ideas GROUP BY status
        """).fetchall()
        done = db.execute("""
            SELECT COUNT(*) AS amount FROM ideas
            WHERE status='approved' AND completed=1
        """).fetchone()["amount"]

    qc = {row["status"]: row["amount"] for row in quiz_rows}
    ic = {row["status"]: row["amount"] for row in idea_rows}

    with room_lock:
        room_count = len(rooms)
        players = sum(len(r["players"]) for r in rooms.values())

    return jsonify({
        "rooms": room_count,
        "players": players,
        "pending_quizzes": qc.get("pending", 0) + qc.get("trash", 0),
        "approved_quizzes": qc.get("approved", 0),
        "rejected_quizzes": qc.get("rejected", 0),
        "pending_ideas": ic.get("pending", 0) + ic.get("trash", 0),
        "approved_ideas": ic.get("approved", 0),
        "rejected_ideas": ic.get("rejected", 0),
        "completed_ideas": done
    })


@app.post("/api/admin/<kind>/<int:item_id>")
def review_item(kind, item_id):
    if not is_admin():
        return admin_error()

    if kind not in ("quizzes", "ideas"):
        return jsonify({"error": "Ungültiger Bereich."}), 400

    data = request.get_json(silent=True) or {}
    status = data.get("status")

    if status not in ("pending", "approved", "rejected"):
        return jsonify({"error": "Ungültiger Status."}), 400

    with get_db() as db:
        cursor = db.execute(
            f"UPDATE {kind} SET status=? WHERE id=?",
            (status, item_id)
        )

    if cursor.rowcount == 0:
        return jsonify({"error": "Eintrag nicht gefunden."}), 404

    if kind == "quizzes":
        with room_lock:
            valid = available_quizzes()
            for code, room in rooms.items():
                if room["status"] == "waiting":
                    if room["quiz"] not in valid:
                        room["quiz"] = "minecraft"
                    send_room_update(code)

    return jsonify({"message": "✅ Änderung gespeichert."})


@app.post("/api/admin/ideas/<int:idea_id>/completed")
def complete_idea(idea_id):
    if not is_admin():
        return admin_error()

    data = request.get_json(silent=True) or {}
    checked = data.get("completed")

    if not isinstance(checked, bool):
        return jsonify({"error": "Ungültiger Wert."}), 400

    with get_db() as db:
        cursor = db.execute("""
            UPDATE ideas SET completed=?
            WHERE id=? AND status='approved'
        """, (int(checked), idea_id))

    if cursor.rowcount == 0:
        return jsonify({"error": "Idee nicht gefunden."}), 404

    return jsonify({"message": "Checkliste gespeichert."})


@app.delete("/api/admin/<kind>/<int:item_id>")
def delete_item(kind, item_id):
    if not is_admin():
        return admin_error()

    if kind not in ("quizzes", "ideas"):
        return jsonify({"error": "Ungültiger Bereich."}), 400

    with get_db() as db:
        cursor = db.execute(
            f"DELETE FROM {kind} WHERE id=? AND status='rejected'",
            (item_id,)
        )

    if cursor.rowcount == 0:
        return jsonify({"error": "Abgelehnter Eintrag fehlt."}), 404

    return jsonify({"message": "🗑️ Endgültig gelöscht."})


# ---------------- MULTIPLAYER-HILFSFUNKTIONEN ----------------

def new_code():
    letters = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(letters) for _ in range(5))
        if code not in rooms:
            return code


def ranking_of(room):
    result = [
        {
            "id": sid,
            "name": p["name"],
            "score": p["score"],
            "host": sid == room["host"],
            **p["profile"]
        }
        for sid, p in room["players"].items()
    ]
    return sorted(result, key=lambda p: -p["score"])


def send_room_update(code):
    room = rooms.get(code)
    if room is None:
        return

    quizzes = available_quizzes()

    socketio.emit("room_updated", {
        "code": code,
        "status": room["status"],
        "quiz": room["quiz"],
        "players": ranking_of(room),
        "quiz_options": [
            {"id": key, "title": q["title"]}
            for key, q in quizzes.items()
        ]
    }, to=code)


def get_player_room(code, sid):
    room = rooms.get(code)
    return room if room and sid in room["players"] else None


# ---------------- ADMIN: ALLE RÄUME ----------------

@app.get("/api/admin/rooms")
def admin_rooms():
    if not is_admin():
        return admin_error()

    with room_lock:
        result = [
            {
                "code": code,
                "quiz": room["quiz"],
                "status": room["status"],
                "phase": room["phase"],
                "player_count": len(room["players"]),
                "players": ranking_of(room)
            }
            for code, room in rooms.items()
        ]

    return jsonify({"rooms": result})


@app.post("/api/admin/rooms/<code>/close")
def admin_close_room(code):
    if not is_admin():
        return admin_error()

    code = code.upper()

    with room_lock:
        room = rooms.pop(code, None)
        if room is None:
            return jsonify({"error": "Raum nicht gefunden."}), 404

        room["token"] += 1
        room["event"].set()
        players = list(room["players"])

    socketio.emit(
        "room_closed",
        {"message": "Der Admin hat diesen Raum geschlossen."},
        to=code
    )
    for sid in players:
        socketio.server.disconnect(sid, namespace="/")

    return jsonify({"message": "Raum geschlossen."})


@app.post("/api/admin/rooms/<code>/kick")
def admin_kick_player(code):
    if not is_admin():
        return admin_error()

    code = code.upper()
    data = request.get_json(silent=True) or {}
    sid = str(data.get("sid", ""))

    with room_lock:
        room = rooms.get(code)

        if room is None or sid not in room["players"]:
            return jsonify({"error": "Spieler nicht gefunden."}), 404

        name = room["players"][sid]["name"]
        del room["players"][sid]

        if not room["players"]:
            room["token"] += 1
            room["event"].set()
            del rooms[code]
        else:
            if room["host"] == sid:
                room["host"] = next(iter(room["players"]))

            if (
                room["phase"] == "question"
                and len(room["answers"]) >= len(room["players"])
            ):
                room["event"].set()

            send_room_update(code)

    socketio.emit(
        "admin_kicked",
        {"message": "Du wurdest vom Admin entfernt."},
        to=sid
    )
    socketio.server.disconnect(sid, namespace="/")

    return jsonify({"message": f"{name} wurde entfernt."})


# ---------------- RÄUME ERSTELLEN / BEITRETEN ----------------

@socketio.on("create_room")
def create_room(data):
    data = data if isinstance(data, dict) else {}
    name = str(data.get("name", "")).strip()[:20]

    if not name:
        emit("app_error", {"message": "Bitte Namen eingeben."})
        return

    with room_lock:
        code = new_code()
        rooms[code] = {
            "host": request.sid,
            "players": {
                request.sid: {
                    "name": name,
                    "profile": profile(data.get("profile")),
                    "score": 0
                }
            },
            "status": "waiting",
            "quiz": "minecraft",
            "phase": "idle",
            "token": 0,
            "answers": {},
            "question": None,
            "started_at": 0,
            "event": threading.Event()
        }
        join_room(code)
        emit("joined", {"code": code})
        send_room_update(code)


@socketio.on("join_room")
def join_game(data):
    data = data if isinstance(data, dict) else {}
    code = str(data.get("code", "")).strip().upper()
    name = str(data.get("name", "")).strip()[:20]

    with room_lock:
        room = rooms.get(code)

        if not name:
            emit("app_error", {"message": "Bitte Namen eingeben."})
            return
        if room is None:
            emit("app_error", {"message": "Raum nicht gefunden."})
            return
        if room["status"] != "waiting":
            emit("app_error", {"message": "Das Spiel läuft bereits."})
            return
        if len(room["players"]) >= 50:
            emit("app_error", {"message": "Raum ist voll."})
            return

        room["players"][request.sid] = {
            "name": name,
            "profile": profile(data.get("profile")),
            "score": 0
        }
        join_room(code)
        emit("joined", {"code": code})
        send_room_update(code)


@socketio.on("select_quiz")
def select_quiz(data):
    data = data if isinstance(data, dict) else {}
    code = str(data.get("code", "")).upper()
    quiz_id = str(data.get("quiz", ""))

    with room_lock:
        room = get_player_room(code, request.sid)
        if (
            not room
            or room["host"] != request.sid
            or room["status"] != "waiting"
            or quiz_id not in available_quizzes()
        ):
            return

        room["quiz"] = quiz_id
        send_room_update(code)


@socketio.on("start_game")
def start_game(data):
    data = data if isinstance(data, dict) else {}
    code = str(data.get("code", "")).upper()

    with room_lock:
        room = get_player_room(code, request.sid)
        if not room or room["host"] != request.sid:
            return
        if room["status"] != "waiting":
            return

        quiz = available_quizzes().get(room["quiz"])
        if not quiz:
            emit("app_error", {"message": "Quiz nicht gefunden."})
            return

        room["status"] = "playing"
        room["phase"] = "idle"
        room["token"] += 1
        room["answers"] = {}

        for player in room["players"].values():
            player["score"] = 0

        token = room["token"]
        send_room_update(code)

    socketio.start_background_task(run_quiz, code, token, quiz)


# ---------------- QUIZ ABLAUF ----------------

def run_quiz(code, token, quiz):
    questions = list(quiz["questions"])
    if quiz.get("shuffle", True):
        random.shuffle(questions)
    questions = questions[:quiz["per_round"]]

    for index, original in enumerate(questions, 1):
        with room_lock:
            room = rooms.get(code)
            if not room or room["token"] != token:
                return

            question = dict(original)
            options = list(question.get("options", []))

            if question["type"] == "order":
                random.shuffle(options)

            room["question"] = question
            room["answers"] = {}
            room["phase"] = "question"
            room["started_at"] = time.monotonic()
            room["event"] = threading.Event()
            event = room["event"]

            payload = {
                "type": question["type"],
                "text": question["text"],
                "duration": question["duration"],
                "options": options,
                "number": index,
                "total": len(questions),
                "quiz_name": quiz["title"],
                "bg": question["bg"],
                "image": question["image"]
            }

        socketio.emit("question", payload, to=code)

        event.wait(question["duration"])

        with room_lock:
            room = rooms.get(code)
            if not room or room["token"] != token:
                return

            room["phase"] = "result"

            if question["type"] == "estimate":
                correct_text = (
                    f'{question["target"]} ± {question["tolerance"]}'
                )
            elif question["type"] == "order":
                correct_text = " → ".join(question["options"])
            else:
                correct_text = ", ".join(
                    question["options"][i]
                    for i in question["correct"]
                )

            result = {
                "ranking": ranking_of(room),
                "correct_text": correct_text,
                "seconds": quiz["ranking_seconds"]
            }

        socketio.emit("result", result, to=code)
        socketio.sleep(quiz["ranking_seconds"])

    with room_lock:
        room = rooms.get(code)
        if not room or room["token"] != token:
            return
        room["status"] = "finished"
        room["phase"] = "idle"
        results = ranking_of(room)

    socketio.emit("finished", {"ranking": results}, to=code)


# ---------------- ANTWORTEN ----------------

@socketio.on("answer")
def player_answer(data):
    data = data if isinstance(data, dict) else {}
    code = str(data.get("code", "")).upper()

    with room_lock:
        room = get_player_room(code, request.sid)
        if not room or room["phase"] != "question":
            return
        if request.sid in room["answers"]:
            return

        question = room["question"]
        elapsed = time.monotonic() - room["started_at"]
        if elapsed >= question["duration"]:
            return

        value = data.get("value")
        correct = False

        if question["type"] == "estimate":
            try:
                guess = float(value)
                if not -1e15 <= guess <= 1e15:
                    return
                correct = abs(
                    guess - question["target"]
                ) <= question["tolerance"]
            except (TypeError, ValueError, OverflowError):
                return

        elif question["type"] == "order":
            if (
                not isinstance(value, list)
                or not all(isinstance(v, str) for v in value)
                or sorted(value) != sorted(question["options"])
            ):
                return
            correct = value == question["options"]

        else:
            if (
                not isinstance(value, list)
                or not value
                or any(
                    type(v) is not int
                    or v < 0
                    or v >= len(question["options"])
                    for v in value
                )
                or len(set(value)) != len(value)
            ):
                return

            correct = sorted(value) == question["correct"]

        points = 0
        if correct:
            points = question["points"]
            if question["bonus"]:
                remaining = max(
                    0, 1 - elapsed / question["duration"]
                )
                points += int(points * remaining)

        room["players"][request.sid]["score"] += points
        room["answers"][request.sid] = value

        emit("answered", {
            "points": points,
            "correct": correct
        })

        socketio.emit("answer_count", {
            "count": len(room["answers"]),
            "total": len(room["players"])
        }, to=code)

        if len(room["answers"]) >= len(room["players"]):
            room["event"].set()


# ---------------- NEUE RUNDE / DISCONNECT ----------------

@socketio.on("again")
def again(data):
    data = data if isinstance(data, dict) else {}
    code = str(data.get("code", "")).upper()

    with room_lock:
        room = get_player_room(code, request.sid)
        if not room or room["host"] != request.sid:
            return
        if room["status"] != "finished":
            return

        room["status"] = "waiting"
        room["phase"] = "idle"
        room["token"] += 1
        send_room_update(code)


@socketio.on("disconnect")
def disconnected():
    with room_lock:
        for code, room in list(rooms.items()):
            if request.sid not in room["players"]:
                continue

            del room["players"][request.sid]

            if not room["players"]:
                room["token"] += 1
                room["event"].set()
                del rooms[code]
            else:
                if room["host"] == request.sid:
                    room["host"] = next(iter(room["players"]))

                if (
                    room["phase"] == "question"
                    and len(room["answers"]) >= len(room["players"])
                ):
                    room["event"].set()

                send_room_update(code)
            break


# ---------------- START ----------------

if __name__ == "__main__":
    print()
    print("🏆 Class-Champion startet!")
    print("🌐 Öffne: http://localhost:5050")
    print()

    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5050)),
        debug=False,
        allow_unsafe_werkzeug=True
    )
