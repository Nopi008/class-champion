
import os
import random
import string
import time
import re

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", "class-champion-dev-key"
)

socketio = SocketIO(app, async_mode="threading")

rooms = {}

QUESTION_TIME = 15
RESULT_TIME = 5

QUIZZES = {
    "minecraft": {
        "name": "⛏️ Minecraft-Wissen",
        "questions": [
            (
                "Welcher Mob explodiert?",
                ["Creeper", "Zombie", "Schaf", "Kuh"],
                0
            ),
            (
                "Welche Dimension erreicht man mit einem Endportal?",
                ["Nether", "Ende", "Oberwelt", "Höhle"],
                1
            ),
            (
                "Wie viele Bretter braucht eine Werkbank?",
                ["2", "4", "6", "8"],
                1
            ),
            (
                "Was braucht man zum Entzünden eines Netherportals?",
                ["Feuerzeug", "Angel", "Kompass", "Fackel"],
                0
            ),
            (
                "Welche Spitzhacke baut Diamanterz erfolgreich ab?",
                ["Holz", "Stein", "Eisen", "Gold"],
                2
            ),
            (
                "Welcher Mob lässt Lohenruten fallen?",
                ["Ghast", "Lohe", "Zombie", "Piglin"],
                1
            ),
            (
                "Wie viele Rahmenblöcke hat ein Endportal?",
                ["8", "10", "12", "16"],
                2
            ),
            (
                "Welcher Trank ermöglicht Unterwasseratmung?",
                ["Stärke", "Nachtsicht", "Wasseratmung", "Schnelligkeit"],
                2
            ),
            (
                "Welcher Block wird für einen Verzauberungstisch benötigt?",
                ["Obsidian", "Kupfer", "Sand", "Erde"],
                0
            ),
            (
                "Welcher Bauteil kann Redstone-Signale verzögern?",
                ["Truhe", "Repeater", "Ofen", "Leiter"],
                1
            ),
        ]
    },

    "wissen": {
        "name": "🧠 Allgemeinwissen",
        "questions": [
            (
                "Wie viele Minuten hat eine Stunde?",
                ["30", "45", "60", "90"],
                2
            ),
            (
                "Welcher Planet ist der rote Planet?",
                ["Venus", "Mars", "Saturn", "Neptun"],
                1
            ),
            (
                "Was ist die Hauptstadt Frankreichs?",
                ["Rom", "Paris", "Berlin", "Madrid"],
                1
            ),
            (
                "Wie viele Seiten hat ein Sechseck?",
                ["5", "6", "7", "8"],
                1
            ),
            (
                "Wie viel ist 7 × 8?",
                ["54", "56", "64", "48"],
                1
            ),
            (
                "Welches Tier ist ein Säugetier?",
                ["Hai", "Delfin", "Forelle", "Krake"],
                1
            ),
            (
                "Wie viele Tage hat eine Woche?",
                ["5", "6", "7", "8"],
                2
            ),
            (
                "Welches Gas benötigen Pflanzen für Fotosynthese?",
                ["Helium", "Kohlendioxid", "Neon", "Wasserstoff"],
                1
            ),
            (
                "Was ist gefrorenes Wasser?",
                ["Nebel", "Dampf", "Eis", "Salz"],
                2
            ),
            (
                "Welcher Ozean ist der größte?",
                ["Atlantik", "Pazifik", "Indischer Ozean", "Arktischer Ozean"],
                1
            ),
        ]
    }
}


def clean_profile(data):
    if not isinstance(data, dict):
        data = {}

    color = str(data.get("color", "#8B5CF6")).upper()

    if not re.fullmatch(r"#[0-9A-F]{6}", color):
        color = "#8B5CF6"

    emojis = str(data.get("emojis", "😎")).strip()[:24]

    if not emojis:
        emojis = "😎"

    image = data.get("image", "")

    if not isinstance(image, str):
        image = ""

    if len(image) > 180000:
        image = ""

    if image and not image.startswith("data:image/jpeg;base64,"):
        image = ""

    return {
        "color": color,
        "emojis": emojis,
        "image": image
    }


def get_players(room):
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


def room_update(code):
    room = rooms.get(code)

    if not room:
        return

    socketio.emit(
        "room_updated",
        {
            "code": code,
            "status": room["status"],
            "quiz": room["quiz"],
            "players": get_players(room)
        },
        to=code
    )


def valid_room(code):
    room = rooms.get(code)

    if room and request.sid in room["players"]:
        return room

    return None


def generate_code():
    while True:
        code = "".join(
            random.choices(
                string.ascii_uppercase + string.digits,
                k=5
            )
        )

        if code not in rooms:
            return code


@app.route("/")
def home():
    return render_template("index.html")


@socketio.on("create_room")
def create_room(data):
    data = data or {}

    sid = request.sid
    code = generate_code()

    name = str(data.get("name", "Champion")).strip()[:20]

    if not name:
        name = "Champion"

    rooms[code] = {
        "host": sid,
        "players": {
            sid: {
                "name": name,
                "profile": clean_profile(data.get("profile")),
                "score": 0
            }
        },
        "status": "waiting",
        "quiz": "minecraft",
        "token": 0,
        "index": -1,
        "answers": {},
        "started_at": 0,
        "phase": "idle"
    }

    join_room(code)

    emit("joined", {"code": code})

    room_update(code)


@socketio.on("join_room")
def join_game(data):
    data = data or {}

    code = str(data.get("code", "")).upper().strip()
    room = rooms.get(code)

    if not room or room["status"] != "waiting":
        emit(
            "app_error",
            {"message": "Raum nicht gefunden oder das Quiz läuft bereits."}
        )
        return

    if len(room["players"]) >= 50:
        emit("app_error", {"message": "Dieser Raum ist voll."})
        return

    name = str(data.get("name", "Champion")).strip()[:20]

    if not name:
        name = "Champion"

    sid = request.sid

    room["players"][sid] = {
        "name": name,
        "profile": clean_profile(data.get("profile")),
        "score": 0
    }

    join_room(code)

    emit("joined", {"code": code})

    room_update(code)


@socketio.on("select_quiz")
def select_quiz(data):
    data = data or {}

    code = str(data.get("code", "")).upper()
    room = valid_room(code)

    if not room:
        return

    if request.sid != room["host"]:
        return

    if room["status"] != "waiting":
        return

    quiz = data.get("quiz")

    if quiz not in QUIZZES:
        return

    room["quiz"] = quiz

    room_update(code)


@socketio.on("start_game")
def start_game(data):
    code = str((data or {}).get("code", "")).upper()

    room = valid_room(code)

    if not room:
        return

    if request.sid != room["host"]:
        return

    if room["status"] != "waiting":
        return

    room["status"] = "playing"
    room["token"] += 1

    for player in room["players"].values():
        player["score"] = 0

    room_update(code)

    socketio.start_background_task(
        run_quiz,
        code,
        room["token"]
    )


def run_quiz(code, token):
    room = rooms.get(code)

    if not room:
        return

    questions = list(QUIZZES[room["quiz"]]["questions"])
    random.shuffle(questions)

    for index, (question, options, correct) in enumerate(questions):
        room = rooms.get(code)

        if not room or room["token"] != token:
            return

        room["index"] = index
        room["answers"] = {}
        room["started_at"] = time.time()
        room["phase"] = "question"
        room["current_questions"] = questions

        socketio.emit(
            "question",
            {
                "text": question,
                "options": options,
                "number": index + 1,
                "total": len(questions),
                "duration": QUESTION_TIME,
                "quiz_name": QUIZZES[room["quiz"]]["name"]
            },
            to=code
        )

        socketio.sleep(QUESTION_TIME)

        room = rooms.get(code)

        if not room or room["token"] != token:
            return

        room["phase"] = "result"

        socketio.emit(
            "result",
            {
                "correct": correct,
                "correct_text": options[correct],
                "ranking": get_players(room)
            },
            to=code
        )

        socketio.sleep(RESULT_TIME)

    room = rooms.get(code)

    if not room or room["token"] != token:
        return

    room["status"] = "finished"

    socketio.emit(
        "finished",
        {"ranking": get_players(room)},
        to=code
    )

    room_update(code)


@socketio.on("answer")
def answer(data):
    data = data or {}

    code = str(data.get("code", "")).upper()
    room = valid_room(code)

    if not room:
        return

    if room["status"] != "playing":
        return

    if room["phase"] != "question":
        return

    if request.sid in room["answers"]:
        return

    try:
        value = int(data.get("value"))
    except (TypeError, ValueError):
        return

    if value not in range(4):
        return

    elapsed = time.time() - room["started_at"]

    if elapsed >= QUESTION_TIME:
        return

    questions = room.get("current_questions")

    if not questions:
        return

    correct = questions[room["index"]][2]

    if value == correct:
        points = 500 + int(
            max(0, QUESTION_TIME - elapsed) * 25
        )
    else:
        points = 0

    room["answers"][request.sid] = value

    room["players"][request.sid]["score"] += points

    emit("answered", {"points": points})

    socketio.emit(
        "answer_count",
        {
            "count": len(room["answers"]),
            "total": len(room["players"])
        },
        to=code
    )

    room_update(code)


@socketio.on("again")
def play_again(data):
    code = str((data or {}).get("code", "")).upper()
    room = valid_room(code)

    if not room:
        return

    if request.sid != room["host"]:
        return

    if room["status"] != "finished":
        return

    room["status"] = "waiting"
    room["phase"] = "idle"
    room["token"] += 1

    room_update(code)


@socketio.on("disconnect")
def disconnect():
    for code, room in list(rooms.items()):
        if request.sid not in room["players"]:
            continue

        del room["players"][request.sid]

        if not room["players"]:
            del rooms[code]
        else:
            if room["host"] == request.sid:
                room["host"] = next(iter(room["players"]))

            room_update(code)

        break


if __name__ == "__main__":
    print("Class-Champion läuft auf Port 5050!")

    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5050)),
        debug=False,
        allow_unsafe_werkzeug=True
    )
