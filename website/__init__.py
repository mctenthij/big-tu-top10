from flask import Flask
from flask_sqlalchemy import SQLAlchemy


app = Flask(__name__)
app.config.from_pyfile("settings.py")
app.config.from_pyfile("settings-secret.py")

db = SQLAlchemy(app)            # initialize Flask-SQLAlchemy
