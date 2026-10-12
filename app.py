
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import random
import string
import os
import time

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", "class-champion-dev-key"
)

socketio = SocketIO(app, async_mode="threading")

rooms = {}

QUESTION_TIME = 15
RESULT_TIME = 5

QUESTIONS = [
    {
        "question": "Welcher Gegenstand entzündet ein Netherportal?",
        "options": [
            {"value": "A", "label": "Feuerzeug"},
            {"value": "B", "label": "Redstone-Fackel"},
            {"value": "C", "label": "Angel"},
            {"value": "D", "label": "Kompass"},
        ],
        "answer": "A",
        "explanation": "Mit einem Feuerzeug kannst du den Innenraum des Netherportals entzünden."
    },
    {
        "question": "Welche Spitzhacke brauchst du mindestens, um Diamanterz abzubauen und Diamanten zu erhalten?",
        "options": [
            {"value": "A", "label": "Holzspitzhacke"},
            {"value": "B", "label": "Steinspitzhacke"},
            {"value": "C", "label": "Eisenspitzhacke"},
            {"value": "D", "label": "Goldspitzhacke"},
        ],
        "answer": "C",
        "explanation": "Eine Eisenspitzhacke oder eine bessere Spitzhacke kann Diamanterz abbauen."
    },
    {
        "question": "Welcher Mob explodiert in der Nähe eines Spielers?",
        "options": [
            {"value": "A", "label": "Zombie"},
            {"value": "B", "label": "Creeper"},
            {"value": "C", "label": "Skelett"},
            {"value": "D", "label": "Kuh"},
        ],
        "answer": "B",
        "explanation": "Der Creeper nähert sich seinem Ziel und explodiert, wenn er nah genug ist."
    },
    {
        "question": "Wie viele Enderaugen passen in den Rahmen eines Endportals?",
        "options": [
            {"value": "A", "label": "8"},
            {"value": "B", "label": "10"},
            {"value": "C", "label": "12"},
            {"value": "D", "label": "16"},
        ],
        "answer": "C",
        "explanation": "Der Endportalrahmen besteht aus zwölf Blöcken, die jeweils ein Enderauge aufnehmen können."
    },
    {
        "question": "Welcher dieser Gegenstände wird für einen Verzauberungstisch benötigt?",
        "options": [
            {"value": "A", "label": "Obsidian"},
            {"value": "B", "label": "Kupferblock"},
            {"value": "C", "label": "Smaragdblock"},
            {"value": "D", "label": "Netheritbarren"},
        ],
        "answer": "A",
        "explanation": "Für einen Verzauberungstisch brauchst du unter anderem vier Obsidianblöcke."
    },
    {
        "question": "Welcher Gegenstand hilft dir, unter Wasser dauerhaft zu atmen, solange sein Effekt aktiv ist?",
        "options": [
            {"value": "A", "label": "Trank der Nachtsicht"},
            {"value": "B", "label": "Trank der Wasseratmung"},
            {"value": "C", "label": "Trank der Stärke"},
            {"value": "D", "label": "Trank der Langsamkeit"},
        ],
        "answer": "B",
        "explanation": "Der Effekt Wasseratmung verhindert, dass dir unter Wasser die Luft ausgeht."
    },
    {
        "question": "Welcher Mob lässt normalerweise Lohenruten fallen?",
        "options": [
            {"value": "A", "label": "Ghast"},
            {"value": "B", "label": "Piglin"},
            {"value": "C", "label": "Witherskelett"},
            {"value": "D", "label": "Lohe"},
        ],
        "answer": "D",
        "explanation": "Lohen können Lohenruten fallen lassen, die du unter anderem für Lohenstaub benötigst."
    },
    {
        "question": "Wie viele Holzbretter brauchst du für eine Werkbank?",
        "options": [
            {"value": "A", "label": "2"},
            {"value": "B", "label": "4"},
            {"value": "C", "label": "6"},
            {"value": "D", "label": "8"},
        ],
        "answer": "B",
        "explanation": "Vier Holzbretter ergeben eine Werkbank."
    },
    {
        "question": "Welcher Redstone-Bauteil kann ein Signal verstärken und verzögern?",
        "options": [
            {"value": "A", "label": "Redstone-Repeater"},
            {"value": "B", "label": "Truhe"},
            {"value": "C", "label": "Ofen"},
            {"value": "D", "label": "Leiter"},
        ],
        "answer": "A",
        "explanation": "Ein Redstone-Repeater erneuert das Signal und kann eine einstellbare Verzögerung hinzufügen."
    },
    {
        "question": "Welche Dimension erreichst du durch ein aktiviertes Endportal?",
        "options": [
            {"value": "A", "label": "Nether"},
            {"value": "B", "label": "Oberwelt"},
            {"value": "C", "label": "Das Ende"},
            {"value": "D", "label": "Eine Höhlendimension"},
        ],
        "answer": "C",
        "explanation": "Ein aktiviertes Endportal führt dich in die Dimension Das Ende."
    },
]


def make_code():
    while True:
        code = "".join(
            random.choices(string.ascii_uppercase + string.digits, k=5)
        )
        if code not in rooms:
            return code


def get_room(code):
    room = rooms.get(code)
    if not room:
        return None

    players = [
        {
            "name": player["name"],
            "is_host": sid == room["host"],
            "score": player.get("score", 0)
        }
        for sid, player in room["players"].items()
    ]

    players.sort(key=lambda player: player["score"], reverse=True)

    return {
        "code": code,
        "players": players,
        "status": room["status"]
    }


def send_room_update(code):
    room_data = get_room(code)
    if room_data:
        socketio.emit("room_updated", {"room": room_data}, to=code)


@app.route("/")
def home():
    return render_template("index.html")


@socketio.on("create_room")
def create_room(data):
    name = str((data or {}).get("name", "")).strip()[:20]

    if not name:
        emit("error_message", {"message": "Gib einen Spielernamen ein."})
        return

    code = make_code()
    sid = request.sid

    rooms[code] = {
        "host": sid,
        "players": {
            sid: {"name": name, "score": 0}
        },
        "status": "waiting",
        "question_index": -1,
        "answers": {},
        "question_started_at": 0,
        "game_id": 0
    }

    join_room(code)

    emit("room_joined", {
        "room": get_room(code),
        "message": "Raum erstellt! Teile den Code mit deinen Mitschülern."
    })


@socketio.on("join_room")
def join_game(data):
    data = data or {}
    name = str(data.get("name", "")).strip()[:20]
    code = str(data.get("code", "")).strip().upper()

    if not name or not code:
        emit("error_message", {
            "message": "Gib deinen Namen und den Raumcode ein."
        })
        return

    room = rooms.get(code)

    if not room:
        emit("error_message", {
            "message": "Raum nicht gefunden. Prüfe den Code."
        })
        return

    if room["status"] != "waiting":
        emit("error_message", {
            "message": "Das Spiel hat bereits begonnen."
        })
        return

    if len(room["players"]) >= 50:
        emit("error_message", {"message": "Der Raum ist voll."})
        return

    sid = request.sid
    room["players"][sid] = {"name": name, "score": 0}
    join_room(code)

    emit("room_joined", {
        "room": get_room(code),
        "message": f"Du bist Raum {code} beigetreten."
    })

    send_room_update(code)


@socketio.on("start_game")
def start_game(data):
    code = str((data or {}).get("code", "")).strip().upper()
    room = rooms.get(code)

    if not room:
        emit("error_message", {"message": "Raum nicht gefunden."})
        return

    if request.sid != room["host"]:
        emit("error_message", {
            "message": "Nur der Spielleiter kann starten."
        })
        return

    if room["status"] != "waiting":
        emit("error_message", {
            "message": "Dieses Spiel wurde bereits gestartet."
        })
        return

    if not room["players"]:
        emit("error_message", {"message": "Es sind keine Spieler im Raum."})
        return

    room["status"] = "started"
    room["question_index"] = -1
    room["answers"] = {}
    room["game_id"] += 1
    game_id = room["game_id"]

    send_room_update(code)
    socketio.start_background_task(run_quiz, code, game_id)


def run_quiz(code, game_id):
    question_order = list(range(len(QUESTIONS)))
    random.shuffle(question_order)

    for index, question_number in enumerate(question_order):
        room = rooms.get(code)

        if not room or room["game_id"] != game_id:
            return

        room["question_index"] = index
        room["answers"] = {}
        room["question_started_at"] = time.time()

        question = QUESTIONS[question_number]

        socketio.emit("quiz_question", {
            "question": question["question"],
            "options": question["options"],
            "number": index + 1,
            "total": len(question_order),
            "duration": QUESTION_TIME
        }, to=code)

        socketio.sleep(QUESTION_TIME)

        room = rooms.get(code)

        if not room or room["game_id"] != game_id:
            return

        # Calculate scores on the server so clients cannot choose their own points.
        for sid, submitted in room["answers"].items():
            if sid not in room["players"]:
                continue

            if submitted["value"] == question["answer"]:
                elapsed = submitted["elapsed"]
                speed_bonus = max(
                    0, int(500 * (QUESTION_TIME - elapsed) / QUESTION_TIME)
                )
                room["players"][sid]["score"] += 500 + speed_bonus

        leaderboard = get_room(code)["players"]

        socketio.emit("quiz_result", {
            "correct_value": question["answer"],
            "explanation": question["explanation"],
            "leaderboard": leaderboard
        }, to=code)

        send_room_update(code)
        socketio.sleep(RESULT_TIME)

    room = rooms.get(code)

    if not room or room["game_id"] != game_id:
        return

    room["status"] = "finished"
    leaderboard = get_room(code)["players"]

    socketio.emit("game_finished", {
        "leaderboard": leaderboard,
        "message": "Quiz beendet! Hier ist die finale Rangliste."
    }, to=code)

    send_room_update(code)


@socketio.on("submit_answer")
def submit_answer(data):
    data = data or {}
    code = str(data.get("code", "")).strip().upper()
    value = str(data.get("value", "")).strip().upper()

    room = rooms.get(code)

    if not room or request.sid not in room["players"]:
        emit("error_message", {"message": "Du bist nicht in diesem Raum."})
        return

    if room["status"] != "started":
        emit("error_message", {"message": "Es läuft gerade keine Frage."})
        return

    index = room["question_index"]

    if index < 0 or index >= len(QUESTIONS):
        emit("error_message", {"message": "Es gibt gerade keine aktive Frage."})
        return

    if time.time() - room["question_started_at"] >= QUESTION_TIME:
        emit("answer_received", {"message": "Die Zeit ist abgelaufen."})
        return

    if request.sid in room["answers"]:
        emit("answer_received", {"message": "Du hast bereits geantwortet."})
        return

    question_number = getattr(room, "unused", None)
    # Find the current question from the server-side order stored below.
    # The actual correct answer is only sent after the timer ends.
    # Options are validated against the current question by value.
    if value not in ("A", "B", "C", "D"):
        emit("error_message", {"message": "Ungültige Antwort."})
        return

    room["answers"][request.sid] = {
        "value": value,
        "elapsed": max(0, time.time() - room["question_started_at"])
    }

    emit("answer_received", {"message": "Antwort gespeichert!"})


@socketio.on("disconnect")
def disconnect():
    sid = request.sid

    for code, room in list(rooms.items()):
        if sid not in room["players"]:
            continue

        del room["players"][sid]
        room["answers"].pop(sid, None)

        if not room["players"]:
            del rooms[code]
            continue

        if sid == room["host"]:
            room["host"] = next(iter(room["players"]))

        send_room_update(code)


if __name__ == "__main__":
    print("Class-Champion wird gestartet...")
    socketio.run(
        app,
        host="127.0.0.1",
        port=5050,
        debug=True,
        allow_unsafe_werkzeug=True
    )
