from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///wardrobe.db"
db = SQLAlchemy(app)


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), default="wishlist")  # "owned" or "wishlist"
    category = db.Column(db.String(50))
    brand = db.Column(db.String(100))
    price = db.Column(db.Float)
    link = db.Column(db.Text)
    image_url = db.Column(db.Text)


with app.app_context():
    db.create_all()


@app.route("/")
def home():
    items = Item.query.all()
    return render_template("index.html", items=items)


if __name__ == "__main__":
    app.run(debug=True)