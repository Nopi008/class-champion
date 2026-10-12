
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import random
import string
import os

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", "class-champion-dev-key"
)

socketio = SocketIO(app, async_mode="threading")

rooms = {}


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
            "is_host": sid == room["host"]
        }
        for sid, player in room["players"].items()
    ]

    return {
        "code": code,
        "players": players,
        "status": room["status"]
    }


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
        "players": {sid: {"name": name}},
        "status": "waiting"
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
        emit("error_message", {
            "message": "Der Raum ist voll."
        })
        return

    sid = request.sid
    room["players"][sid] = {"name": name}
    join_room(code)

    emit("room_joined", {
        "room": get_room(code),
        "message": f"Du bist Raum {code} beigetreten."
    })

    socketio.emit(
        "room_updated",
        {"room": get_room(code)},
        to=code
    )


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

    room["status"] = "started"

    socketio.emit(
        "room_updated",
        {"room": get_room(code)},
        to=code
    )

    socketio.emit(
        "game_started",
        {"message": "Multiplayer funktioniert! Als Nächstes kommen die Quizfragen."},
        to=code
    )


@socketio.on("disconnect")
def disconnect():
    sid = request.sid

    for code, room in list(rooms.items()):
        if sid not in room["players"]:
            continue

        del room["players"][sid]

        if not room["players"]:
            del rooms[code]
            continue

        if sid == room["host"]:
            new_host = next(iter(room["players"]))
            room["host"] = new_host

        socketio.emit(
            "room_updated",
            {"room": get_room(code)},
            to=code
        )
if __name__ == "__main__":
    print("Class-Champion wird gestartet...")
    socketio.run(
        app,
        host="127.0.0.1",
        port=5050,
        debug=True,
        allow_unsafe_werkzeug=True
    )