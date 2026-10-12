
from flask import Flask, render_template
from flask_socketio import SocketIO
import os

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "class-champion-test")

socketio = SocketIO(app, async_mode="threading")

@app.route("/")
def home():
    return render_template("index.html")
