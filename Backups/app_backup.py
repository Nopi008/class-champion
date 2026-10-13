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


# ---------------------------------------------------
# GRUNDEINSTELLUNGEN
# ---------------------------------------------------

BASE = Path(__file__).resolve().parent

DATABASE_PATH = os.environ.get(
    "DATABASE_PATH",
    str(BASE / "champion.sqlite3")
)

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "local-development-secret-change-me"
)

app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

socketio = SocketIO(
    app,
    async_mode="threading",
    cors_allowed_origins="*",
    max_http_buffer_size=2_000_000
)

rooms = {}
room_lock = threading.RLock()


# ---------------------------------------------------
# DATENBANK
# ---------------------------------------------------

def get_db():
    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=20
    )
    connection.row_factory = sqlite3.Row
    return connection


def create_database():
    Path(DATABASE_PATH).parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with get_db() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS quizzes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                data TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created INTEGER NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created INTEGER NOT NULL
            )
        """)


create_database()


# ---------------------------------------------------
# HILFSFUNKTIONEN
# ---------------------------------------------------

def safe_color(value, default="#8B5CF6"):
    if isinstance(value, str):
        if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            return value.upper()

    return default


def safe_int(value, minimum, maximum, default):
    try:
        value = int(value)
        return max(minimum, min(maximum, value))
    except (TypeError, ValueError):
        return default


def safe_image(value):
    if value == "":
        return True

    return (
        isinstance(value, str)
        and len(value) <= 150000
        and re.fullmatch(
            r"data:image/jpeg;base64,[A-Za-z0-9+/=]+",
            value
        ) is not None
    )


def safe_profile(raw):
    if not isinstance(raw, dict):
        raw = {}

    image = raw.get("image", "")

    return {
        "emojis": str(raw.get("emojis", "😎"))[:32] or "😎",
        "bg": safe_color(
            raw.get("bg"),
            "#32215E"
        ),
        "border": safe_color(
            raw.get("border"),
            "#8B5CF6"
        ),
        "image": image if safe_image(image) else ""
    }


# ---------------------------------------------------
# QUIZ ÜBERPRÜFEN
# ---------------------------------------------------

def validate_quiz(raw):
    if not isinstance(raw, dict):
        raise ValueError("Ungültiges Quiz.")

    title = str(
        raw.get("title", "")
    ).strip()[:80]

    if not title:
        raise ValueError(
            "Bitte einen Quiznamen eingeben."
        )

    questions = raw.get("questions")

    if (
        not isinstance(questions, list)
        or not 1 <= len(questions) <= 30
    ):
        raise ValueError(
            "Ein Quiz braucht 1 bis 30 Fragen."
        )

    checked_questions = []

    for number, question in enumerate(questions, 1):
        if not isinstance(question, dict):
            raise ValueError(
                f"Frage {number} ist ungültig."
            )

        kind = question.get("type", "choice")

        if kind not in (
            "choice",
            "yesno",
            "truefalse",
            "estimate",
            "order"
        ):
            raise ValueError(
                f"Frage {number}: unbekannter Fragetyp."
            )

        text = str(
            question.get("text", "")
        ).strip()[:350]

        if not text:
            raise ValueError(
                f"Bitte Text für Frage {number} eingeben."
            )

        image = question.get("image", "")

        if not safe_image(image):
            raise ValueError(
                f"Das Bild bei Frage {number} ist zu groß."
            )

        item = {
            "type": kind,
            "text": text,
            "duration": safe_int(
                question.get("duration"),
                5,
                120,
                15
            ),
            "points": safe_int(
                question.get("points"),
                100,
                2000,
                500
            ),
            "bonus": bool(
                question.get("bonus", True)
            ),
            "bg": safe_color(
                question.get("bg"),
                "#29204B"
            ),
            "image": image
        }

        if kind == "estimate":
            try:
                target = float(
                    question.get("target", 0)
                )

                tolerance = float(
                    question.get("tolerance", 0)
                )

                if (
                    not -1e12 <= target <= 1e12
                    or not 0 <= tolerance <= 1e12
                ):
                    raise ValueError()

            except (ValueError, TypeError):
                raise ValueError(
                    f"Frage {number}: ungültiger Zahlenwert."
                )

            item["target"] = target
            item["tolerance"] = tolerance

        else:
            if kind == "yesno":
                options = ["Ja", "Nein"]

            elif kind == "truefalse":
                options = ["Richtig", "Falsch"]

            else:
                options = question.get("options")

                if (
                    not isinstance(options, list)
                    or not 2 <= len(options) <= 6
                ):
                    raise ValueError(
                        f"Frage {number} braucht "
                        "2 bis 6 Antworten."
                    )

                options = [
                    str(option).strip()[:120]
                    for option in options
                ]

                if any(not option for option in options):
                    raise ValueError(
                        f"Frage {number}: "
                        "alle Antworten ausfüllen."
                    )

                if len(set(options)) != len(options):
                    raise ValueError(
                        f"Frage {number}: "
                        "Antworten müssen verschieden sein."
                    )

            item["options"] = options

            if kind == "order":
                item["correct"] = list(
                    range(len(options))
                )

            else:
                correct = question.get(
                    "correct",
                    [0]
                )

                if (
                    not isinstance(correct, list)
                    or not correct
                    or any(
                        not isinstance(i, int)
                        or isinstance(i, bool)
                        or i < 0
                        or i >= len(options)
                        for i in correct
                    )
                ):
                    raise ValueError(
                        f"Frage {number}: "
                        "richtige Antwort auswählen."
                    )

                item["correct"] = sorted(
                    set(correct)
                )

        checked_questions.append(item)

    cover = raw.get("cover", "")

    if not safe_image(cover):
        raise ValueError(
            "Das Quiz-Titelbild ist zu groß."
        )

    difficulty = str(
        raw.get("difficulty", "Mittel")
    )

    if difficulty not in (
        "Leicht",
        "Mittel",
        "Schwer"
    ):
        difficulty = "Mittel"

    return {
        "title": title,
        "description": str(
            raw.get("description", "")
        ).strip()[:300],
        "category": str(
            raw.get("category", "Allgemein")
        ).strip()[:50] or "Allgemein",
        "difficulty": difficulty,
        "cover": cover,
        "per_round": safe_int(
            raw.get("per_round"),
            1,
            len(checked_questions),
            len(checked_questions)
        ),
        "ranking_seconds": safe_int(
            raw.get("ranking_seconds"),
            2,
            15,
            5
        ),
        "shuffle": bool(
            raw.get("shuffle", True)
        ),
        "questions": checked_questions
    }


# ---------------------------------------------------
# EINGEBAUTE QUIZZE
# ---------------------------------------------------

def standard_quiz(title, data):
    return validate_quiz({
        "title": title,
        "per_round": len(data),
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
            for text, options, answer in data
        ]
    })


BUILTIN_QUIZZES = {
    "minecraft": standard_quiz(
        "⛏️ Minecraft",
        [
            (
                "Welcher Mob explodiert?",
                ["Creeper", "Schaf", "Kuh", "Zombie"],
                0
            ),
            (
                "Wie viele Bretter braucht eine Werkbank?",
                ["2", "4", "6", "8"],
                1
            ),
            (
                "Wo lebt der Enderdrache?",
                ["Nether", "Das Ende", "Dorf", "Höhle"],
                1
            ),
            (
                "Womit zündet man ein Netherportal an?",
                [
                    "Feuerzeug",
                    "Kompass",
                    "Angel",
                    "Schwert"
                ],
                0
            ),
            (
                "Welche Spitzhacke kann Diamanterz abbauen?",
                ["Holz", "Stein", "Eisen", "Gold"],
                2
            )
        ]
    ),

    "wissen": standard_quiz(
        "🧠 Allgemeinwissen",
        [
            (
                "Wie viele Minuten hat eine Stunde?",
                ["30", "45", "60", "90"],
                2
            ),
            (
                "Welcher Planet heißt roter Planet?",
                ["Venus", "Mars", "Saturn", "Neptun"],
                1
            ),
            (
                "Was ist die Hauptstadt von Frankreich?",
                ["Berlin", "Paris", "Rom", "Madrid"],
                1
            ),
            (
                "Wie viel ist 7 mal 8?",
                ["54", "56", "64", "48"],
                1
            ),
            (
                "Welcher Ozean ist am größten?",
                [
                    "Atlantik",
                    "Pazifik",
                    "Indischer Ozean",
                    "Arktischer Ozean"
                ],
                1
            )
        ]
    )
}


def available_quizzes():
    result = dict(BUILTIN_QUIZZES)

    with get_db() as connection:
        rows = connection.execute("""
            SELECT id, data
            FROM quizzes
            WHERE status = 'approved'
        """).fetchall()

    for row in rows:
        result[
            "user-" + str(row["id"])
        ] = json.loads(row["data"])

    return result


# ---------------------------------------------------
# WEBSITE
# ---------------------------------------------------

@app.get("/")
def homepage():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "name": "Class-Champion"
    })


@app.get("/api/quizzes")
def quiz_list():
    return jsonify([
        {
            "id": key,
            "title": quiz["title"],
            "category": quiz.get(
                "category",
                "Allgemein"
            )
        }
        for key, quiz in available_quizzes().items()
    ])


# ---------------------------------------------------
# QUIZ EINREICHEN
# ---------------------------------------------------

@app.post("/api/quizzes")
def submit_quiz():
    try:
        quiz = validate_quiz(
            request.get_json(silent=True)
        )

        with get_db() as connection:
            connection.execute("""
                INSERT INTO quizzes
                (title, data, status, created)
                VALUES (?, ?, 'trash', ?)
            """, (
                quiz["title"],
                json.dumps(
                    quiz,
                    ensure_ascii=False
                ),
                int(time.time())
            ))

        return jsonify({
            "message": (
                "Quiz eingereicht! "
                "Der Admin kann es jetzt prüfen."
            )
        }), 201

    except ValueError as error:
        return jsonify({
            "error": str(error)
        }), 400


# ---------------------------------------------------
# IDEEN EINREICHEN
# ---------------------------------------------------

@app.post("/api/ideas")
def submit_idea():
    data = request.get_json(
        silent=True
    ) or {}

    name = str(
        data.get("name", "Gast")
    ).strip()[:40] or "Gast"

    text = str(
        data.get("text", "")
    ).strip()[:2000]

    if len(text) < 5:
        return jsonify({
            "error": (
                "Bitte eine längere Idee eingeben."
            )
        }), 400

    with get_db() as connection:
        connection.execute("""
            INSERT INTO ideas
            (name, text, status, created)
            VALUES (?, ?, 'trash', ?)
        """, (
            name,
            text,
            int(time.time())
        ))

    return jsonify({
        "message": "Danke für deinen Vorschlag!"
    }), 201


# ---------------------------------------------------
# ADMIN
# ---------------------------------------------------

def is_admin():
    return (
        bool(os.environ.get("ADMIN_PASSWORD"))
        and session.get("admin") is True
    )


@app.post("/api/admin/login")
def admin_login():
    expected = os.environ.get(
        "ADMIN_PASSWORD",
        ""
    )

    data = request.get_json(
        silent=True
    ) or {}

    provided = str(
        data.get("password", "")
    )

    if (
        not expected
        or not secrets.compare_digest(
            expected,
            provided
        )
    ):
        return jsonify({
            "error": (
                "Falsches Passwort oder "
                "ADMIN_PASSWORD fehlt."
            )
        }), 403

    session["admin"] = True

    return jsonify({
        "message": "Erfolgreich angemeldet."
    })


@app.post("/api/admin/logout")
def admin_logout():
    session.clear()

    return jsonify({
        "message": "Abgemeldet."
    })


@app.get("/api/admin/items")
def admin_items():
    if not is_admin():
        return jsonify({
            "error": "Nicht angemeldet."
        }), 403

    with get_db() as connection:
        quizzes = [
            dict(row)
            for row in connection.execute("""
                SELECT id, title, data, status, created
                FROM quizzes
                ORDER BY id DESC
            """)
        ]

        ideas = [
            dict(row)
            for row in connection.execute("""
                SELECT id, name, text, status, created
                FROM ideas
                ORDER BY id DESC
            """)
        ]

    for quiz in quizzes:
        quiz["data"] = json.loads(
            quiz["data"]
        )

    return jsonify({
        "quizzes": quizzes,
        "ideas": ideas
    })


@app.post("/api/admin/<kind>/<int:item_id>")
def review_item(kind, item_id):
    if not is_admin():
        return jsonify({
            "error": "Nicht angemeldet."
        }), 403

    if kind not in ("quizzes", "ideas"):
        return jsonify({
            "error": "Ungültiger Bereich."
        }), 400

    data = request.get_json(
        silent=True
    ) or {}

    status = data.get("status")

    if status not in (
        "approved",
        "rejected"
    ):
        return jsonify({
            "error": "Ungültige Entscheidung."
        }), 400

    with get_db() as connection:
        cursor = connection.execute(
            f"UPDATE {kind} SET status=? WHERE id=?",
            (status, item_id)
        )

    if cursor.rowcount == 0:
        return jsonify({
            "error": "Eintrag nicht gefunden."
        }), 404

    return jsonify({
        "message": "Entscheidung gespeichert."
    })


# ---------------------------------------------------
# MULTIPLAYER
# ---------------------------------------------------

def generate_room_code():
    characters = (
        string.ascii_uppercase
        + string.digits
    )

    while True:
        code = "".join(
            random.choices(
                characters,
                k=5
            )
        )

        if code not in rooms:
            return code


def ranking_of(room):
    players = []

    for sid, player in room["players"].items():
        players.append({
            "id": sid,
            "name": player["name"],
            "score": player["score"],
            "host": sid == room["host"],
            **player["profile"]
        })

    return sorted(
        players,
        key=lambda player: -player["score"]
    )


def send_room_update(code):
    room = rooms.get(code)

    if room is None:
        return

    all_quizzes = available_quizzes()

    socketio.emit(
        "room_updated",
        {
            "code": code,
            "status": room["status"],
            "quiz": room["quiz"],
            "players": ranking_of(room),
            "quiz_options": [
                {
                    "id": key,
                    "title": quiz["title"]
                }
                for key, quiz in all_quizzes.items()
            ]
        },
        to=code
    )


def room_for_player(code, sid):
    room = rooms.get(code)

    if (
        room is not None
        and sid in room["players"]
    ):
        return room

    return None


@socketio.on("create_room")
def create_room(data):
    data = data if isinstance(
        data, dict
    ) else {}

    name = str(
        data.get("name", "")
    ).strip()[:20]

    if not name:
        emit("app_error", {
            "message": "Bitte Namen eingeben."
        })
        return

    with room_lock:
        code = generate_room_code()

        rooms[code] = {
            "host": request.sid,
            "players": {
                request.sid: {
                    "name": name,
                    "profile": safe_profile(
                        data.get("profile")
                    ),
                    "score": 0
                }
            },
            "status": "waiting",
            "quiz": "minecraft",
            "token": 0,
            "answers": {},
            "phase": "idle",
            "question": None,
            "started_at": 0,
            "event": threading.Event()
        }

        join_room(code)

        emit("joined", {
            "code": code
        })

        send_room_update(code)


@socketio.on("join_room")
def join_game(data):
    data = data if isinstance(
        data, dict
    ) else {}

    code = str(
        data.get("code", "")
    ).strip().upper()

    name = str(
        data.get("name", "")
    ).strip()[:20]

    with room_lock:
        room = rooms.get(code)

        if not name:
            emit("app_error", {
                "message": "Bitte Namen eingeben."
            })
            return

        if room is None:
            emit("app_error", {
                "message": "Raum nicht gefunden."
            })
            return

        if room["status"] != "waiting":
            emit("app_error", {
                "message": "Das Spiel läuft bereits."
            })
            return

        if len(room["players"]) >= 50:
            emit("app_error", {
                "message": "Der Raum ist voll."
            })
            return

        room["players"][request.sid] = {
            "name": name,
            "profile": safe_profile(
                data.get("profile")
            ),
            "score": 0
        }

        join_room(code)

        emit("joined", {
            "code": code
        })

        send_room_update(code)


@socketio.on("select_quiz")
def select_quiz(data):
    data = data if isinstance(
        data, dict
    ) else {}

    code = str(
        data.get("code", "")
    ).upper()

    quiz_id = str(
        data.get("quiz", "")
    )

    with room_lock:
        room = room_for_player(
            code,
            request.sid
        )

        if room is None:
            return

        if room["host"] != request.sid:
            return

        if room["status"] != "waiting":
            return

        if quiz_id not in available_quizzes():
            return

        room["quiz"] = quiz_id

        send_room_update(code)


@socketio.on("start_game")
def start_game(data):
    data = data if isinstance(
        data, dict
    ) else {}

    code = str(
        data.get("code", "")
    ).upper()

    with room_lock:
        room = room_for_player(
            code,
            request.sid
        )

        if room is None:
            return

        if room["host"] != request.sid:
            return

        if room["status"] != "waiting":
            return

        all_quizzes = available_quizzes()

        quiz = all_quizzes.get(
            room["quiz"]
        )

        if quiz is None:
            emit("app_error", {
                "message": "Quiz nicht gefunden."
            })
            return

        room["status"] = "playing"
        room["phase"] = "idle"
        room["token"] += 1

        for player in room["players"].values():
            player["score"] = 0

        token = room["token"]

        send_room_update(code)

    socketio.start_background_task(
        run_quiz,
        code,
        token,
        quiz
    )


# ---------------------------------------------------
# SPIEL DURCHFÜHREN
# ---------------------------------------------------

def run_quiz(code, token, quiz):
    questions = list(
        quiz["questions"]
    )

    if quiz["shuffle"]:
        random.shuffle(questions)

    questions = questions[
        :quiz["per_round"]
    ]

    for number, original in enumerate(
        questions,
        1
    ):
        with room_lock:
            room = rooms.get(code)

            if (
                room is None
                or room["token"] != token
            ):
                return

            question = dict(original)

            if question["type"] == "order":
                display_options = list(
                    question["options"]
                )

                random.shuffle(
                    display_options
                )
            else:
                display_options = list(
                    question.get("options", [])
                )

            room["question"] = question
            room["answers"] = {}
            room["phase"] = "question"
            room["started_at"] = time.monotonic()
            room["event"] = threading.Event()

            question_event = room["event"]

            payload = {
                "type": question["type"],
                "text": question["text"],
                "duration": question["duration"],
                "options": display_options,
                "number": number,
                "total": len(questions),
                "quiz_name": quiz["title"],
                "bg": question["bg"],
                "image": question["image"]
            }

        socketio.emit(
            "question",
            payload,
            to=code
        )

        # Ende nach Zeitablauf oder wenn
        # alle aktiven Spieler geantwortet haben.
        question_event.wait(
            question["duration"]
        )

        with room_lock:
            room = rooms.get(code)

            if (
                room is None
                or room["token"] != token
            ):
                return

            room["phase"] = "result"

            if question["type"] == "estimate":
                correct_text = (
                    f'{question["target"]} '
                    f'± {question["tolerance"]}'
                )

            elif question["type"] == "order":
                correct_text = " → ".join(
                    question["options"]
                )

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

        socketio.emit(
            "result",
            result,
            to=code
        )

        socketio.sleep(
            quiz["ranking_seconds"]
        )

    with room_lock:
        room = rooms.get(code)

        if (
            room is None
            or room["token"] != token
        ):
            return

        room["status"] = "finished"
        room["phase"] = "idle"

        final_ranking = ranking_of(room)

    socketio.emit(
        "finished",
        {
            "ranking": final_ranking
        },
        to=code
    )


# ---------------------------------------------------
# ANTWORTEN
# ---------------------------------------------------

@socketio.on("answer")
def player_answer(data):
    data = data if isinstance(
        data, dict
    ) else {}

    code = str(
        data.get("code", "")
    ).upper()

    with room_lock:
        room = room_for_player(
            code,
            request.sid
        )

        if room is None:
            return

        if room["phase"] != "question":
            return

        if request.sid in room["answers"]:
            return

        question = room["question"]

        elapsed = (
            time.monotonic()
            - room["started_at"]
        )

        if elapsed >= question["duration"]:
            return

        value = data.get("value")
        correct = False

        if question["type"] == "estimate":
            try:
                number = float(value)

                if not -1e15 <= number <= 1e15:
                    return

                correct = (
                    abs(
                        number - question["target"]
                    ) <= question["tolerance"]
                )

            except (TypeError, ValueError):
                return

        elif question["type"] == "order":
            if (
                not isinstance(value, list)
                or not all(
                    isinstance(x, str)
                    for x in value
                )
                or sorted(value) != sorted(
                    question["options"]
                )
            ):
                return

            correct = (
                value == question["options"]
            )

        else:
            if (
                not isinstance(value, list)
                or not value
                or any(
                    not isinstance(i, int)
                    or isinstance(i, bool)
                    or i < 0
                    or i >= len(question["options"])
                    for i in value
                )
            ):
                return

            # Keine doppelten Antwort-Indizes.
            if len(set(value)) != len(value):
                return

            correct = (
                sorted(value)
                == question["correct"]
            )

        if correct:
            points = question["points"]

            if question["bonus"]:
                remaining = max(
                    0,
                    1 - elapsed / question["duration"]
                )

                points += int(
                    points * remaining
                )
        else:
            points = 0

        room["players"][
            request.sid
        ]["score"] += points

        room["answers"][
            request.sid
        ] = value

        emit("answered", {
            "points": points,
            "correct": correct
        })

        socketio.emit(
            "answer_count",
            {
                "count": len(
                    room["answers"]
                ),
                "total": len(
                    room["players"]
                )
            },
            to=code
        )

        if (
            len(room["answers"])
            >= len(room["players"])
        ):
            room["event"].set()


# ---------------------------------------------------
# NEUE RUNDE
# ---------------------------------------------------

@socketio.on("again")
def play_again(data):
    data = data if isinstance(
        data, dict
    ) else {}

    code = str(
        data.get("code", "")
    ).upper()

    with room_lock:
        room = room_for_player(
            code,
            request.sid
        )

        if room is None:
            return

        if room["host"] != request.sid:
            return

        if room["status"] != "finished":
            return

        room["status"] = "waiting"
        room["phase"] = "idle"
        room["token"] += 1

        send_room_update(code)


# ---------------------------------------------------
# VERBINDUNG GETRENNT
# ---------------------------------------------------

@socketio.on("disconnect")
def player_disconnected():
    with room_lock:
        for code, room in list(
            rooms.items()
        ):
            if (
                request.sid
                not in room["players"]
            ):
                continue

            del room["players"][
                request.sid
            ]

            if not room["players"]:
                room["event"].set()
                del rooms[code]

            else:
                if (
                    room["host"]
                    == request.sid
                ):
                    room["host"] = next(
                        iter(room["players"])
                    )

                if (
                    room["phase"] == "question"
                    and len(room["answers"])
                    >= len(room["players"])
                ):
                    room["event"].set()

                send_room_update(code)

            break


# ---------------------------------------------------
# START
# ---------------------------------------------------

if __name__ == "__main__":
    print("")
    print("🏆 Class-Champion startet!")
    print("🌐 Öffne: http://localhost:5050")
    print("")

    socketio.run(
        app,
        host="0.0.0.0",
        port=int(
            os.environ.get("PORT", 5050)
        ),
        debug=False,
        allow_unsafe_werkzeug=True
    )