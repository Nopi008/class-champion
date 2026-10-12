
from flask import Flask, render_template
from flask_socketio import SocketIO

app = Flask(__name__)
app.config["SECRET_KEY"] = "class-champion-test"
socketio = SocketIO(app)

@app.route("/")
def home():
    return render_template("index.html")

if __name__ == "__main__":
    print("Class-Champion startet!")
    print("Öffne http://127.0.0.1:5000 im Browser.")
socketio.run(app, debug=True, allow_unsafe_werkzeug=True)