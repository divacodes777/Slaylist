import os
import requests
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, flash, abort
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///wardrobe.db"
# Local use only. When you deploy, set a real SECRET_KEY environment variable.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in first."

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}
TRACKING = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid"}


# ---------- database ----------

class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(30), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), default="wishlist")  # "owned" or "wishlist"
    category = db.Column(db.String(50))
    brand = db.Column(db.String(100))
    price = db.Column(db.Float)
    link = db.Column(db.Text)
    image_url = db.Column(db.Text)


wardrobe_items = db.Table(
    "wardrobe_items",
    db.Column("wardrobe_id", db.Integer, db.ForeignKey("wardrobe.id"), primary_key=True),
    db.Column("item_id", db.Integer, db.ForeignKey("item.id"), primary_key=True),
)


class Wardrobe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    items = db.relationship("Item", secondary=wardrobe_items, backref="wardrobes")


with app.app_context():
    db.create_all()


# ---------- helpers for reading product links ----------

def clean_link(url):
    """Drop tracking junk (utm_*, fbclid...) so the saved link stays short."""
    p = urlparse(url)
    keep = [
        (k, v) for k, v in parse_qsl(p.query)
        if not k.startswith("utm_") and k not in TRACKING
    ]
    return urlunparse(p._replace(query=urlencode(keep), fragment=""))


def shopify_info(url):
    """Shopify stores can share product data as JSON. Returns None if not available."""
    p = urlparse(url)
    if "/products/" not in p.path:
        return None
    handle = p.path.split("/products/")[1].strip("/").split("/")[0]
    try:
        r = requests.get(
            f"{p.scheme}://{p.netloc}/products/{handle}.json", headers=HEADERS, timeout=10
        )
        product = r.json()["product"]
    except (requests.RequestException, ValueError, KeyError):
        return None

    price = None
    try:
        price = float(product["variants"][0]["price"])
    except (KeyError, IndexError, ValueError, TypeError):
        pass
    images = product.get("images") or []
    return {
        "name": product.get("title"),
        "image_url": images[0].get("src") if images else None,
        "brand": product.get("vendor"),
        "price": price,
    }


def fetch_product_info(url):
    """Read a product page and pull out its title, image, brand and price."""
    info = {"name": None, "image_url": None, "brand": None, "price": None}
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
    except requests.RequestException:
        soup = None

    if soup:
        def meta(*names):
            for n in names:
                tag = soup.find("meta", property=n) or soup.find("meta", attrs={"name": n})
                if tag and tag.get("content"):
                    return tag["content"].strip()
            return None

        info["name"] = meta("og:title", "twitter:title")
        if not info["name"] and soup.title and soup.title.string:
            info["name"] = soup.title.string.strip()
        info["image_url"] = meta("og:image", "twitter:image")
        info["brand"] = meta("og:site_name")

        price = meta("product:price:amount", "og:price:amount")
        if price:
            try:
                info["price"] = float(price.replace(",", ""))
            except ValueError:
                pass

    # backup plan for Shopify stores
    if not info["image_url"] or not info["name"]:
        shop = shopify_info(url)
        if shop:
            for key, value in shop.items():
                info[key] = info[key] or value
    return info


def my_wardrobe_or_404(wardrobe_id):
    """Fetch a wardrobe, but only if it belongs to the logged-in user."""
    w = db.get_or_404(Wardrobe, wardrobe_id)
    if w.user_id != current_user.id:
        abort(404)
    return w
# ---------- my wardrobes (login required) ----------

@app.route("/wardrobes")
@login_required
def home():
    items = Item.query.filter_by(user_id=current_user.id).order_by(Item.id.desc()).all()
    wardrobes = Wardrobe.query.filter_by(user_id=current_user.id).order_by(Wardrobe.id.desc()).all()
    return render_template("index.html", items=items, wardrobes=wardrobes)


@app.route("/add", methods=["POST"])
@login_required
def add():
    link = clean_link(request.form.get("link", "").strip())
    typed_name = request.form.get("name", "").strip()
    wardrobe_id = request.form.get("wardrobe_id", type=int)
    if link:
        info = fetch_product_info(link)
        item = Item(
            user_id=current_user.id,
            name=typed_name or info["name"] or "Untitled piece",
            status="wishlist",
            brand=info["brand"],
            price=info["price"],
            link=link,
            image_url=info["image_url"],
        )
        if wardrobe_id:
            w = db.session.get(Wardrobe, wardrobe_id)
            if w and w.user_id == current_user.id:
                item.wardrobes.append(w)
        db.session.add(item)
        db.session.commit()
    if wardrobe_id:
        return redirect(url_for("wardrobe", wardrobe_id=wardrobe_id))
    return redirect(url_for("home"))


@app.route("/wardrobe/new", methods=["POST"])
@login_required
def new_wardrobe():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("home"))
    w = Wardrobe(user_id=current_user.id, name=name)
    db.session.add(w)
    db.session.commit()
    return redirect(url_for("wardrobe", wardrobe_id=w.id))


@app.route("/wardrobe/<int:wardrobe_id>")
@login_required
def wardrobe(wardrobe_id):
    w = my_wardrobe_or_404(wardrobe_id)
    in_ids = {i.id for i in w.items}
    mine = Item.query.filter_by(user_id=current_user.id).order_by(Item.id.desc()).all()
    available = [i for i in mine if i.id not in in_ids]
    return render_template("wardrobe.html", wardrobe=w, available=available)


@app.route("/wardrobe/<int:wardrobe_id>/add", methods=["POST"])
@login_required
def add_pieces(wardrobe_id):
    w = my_wardrobe_or_404(wardrobe_id)
    for item_id in request.form.getlist("item_ids", type=int):
        item = db.session.get(Item, item_id)
        if item and item.user_id == current_user.id and item not in w.items:
            w.items.append(item)
    db.session.commit()
    return redirect(url_for("wardrobe", wardrobe_id=w.id))


@app.route("/wardrobe/<int:wardrobe_id>/remove/<int:item_id>", methods=["POST"])
@login_required
def remove_piece(wardrobe_id, item_id):
    w = my_wardrobe_or_404(wardrobe_id)
    item = db.session.get(Item, item_id)
    if item in w.items:
        w.items.remove(item)
        db.session.commit()
    return redirect(url_for("wardrobe", wardrobe_id=w.id))


# ---------- welcome and discover ----------

SAMPLE_OUTFITS = [
    {"title": "Cozy winter brunch", "by": "@mira.wears", "tag": "winter", "color": "#CFE3E8", "sticker": "sparkle", "ratio": "4 / 5",
     "pieces": ["Cream knit sweater", "Wide-leg jeans", "Chunky loafers", "Mini shoulder bag"]},
    {"title": "Beach day off-duty", "by": "@sunnyfits", "tag": "summer", "color": "#FFE7A8", "sticker": "star", "ratio": "1 / 1",
     "pieces": ["Linen shirt", "Striped shorts", "Straw tote", "Sandals"]},
    {"title": "Girls' night out", "by": "@glam.lena", "tag": "party", "color": "#F8C8DC", "sticker": "heart", "ratio": "3 / 4",
     "pieces": ["Satin slip dress", "Strappy heels", "Mini clutch", "Gold hoops"]},
    {"title": "Monday meeting", "by": "@desk.to.dinner", "tag": "work", "color": "#BFC3FF", "sticker": "flower", "ratio": "4 / 6",
     "pieces": ["Tailored blazer", "Straight trousers", "Pointed flats", "Structured tote"]},
    {"title": "Airport comfy", "by": "@jet.set.jo", "tag": "travel", "color": "#CDEBD3", "sticker": "cherry", "ratio": "1 / 1",
     "pieces": ["Oversized hoodie", "Joggers", "White sneakers", "Crossbody bag"]},
    {"title": "Coffee run casual", "by": "@mira.wears", "tag": "casual", "color": "#FFD9C7", "sticker": "sparkle", "ratio": "3 / 4",
     "pieces": ["Graphic tee", "Denim jacket", "Baggy jeans", "Canvas sneakers"]},
    {"title": "Layered for snow", "by": "@frost.fits", "tag": "winter", "color": "#CFE3E8", "sticker": "star", "ratio": "4 / 6",
     "pieces": ["Faux fur coat", "Turtleneck", "Wool trousers", "Ankle boots"]},
    {"title": "Garden party", "by": "@sunnyfits", "tag": "party", "color": "#F8C8DC", "sticker": "flower", "ratio": "4 / 5",
     "pieces": ["Floral midi dress", "Cardigan", "Block heels", "Pearl earrings"]},
]


@app.route("/")
def welcome():
    return render_template("welcome.html")


@app.route("/discover")
def discover():
    tags = sorted({o["tag"] for o in SAMPLE_OUTFITS})
    return render_template("discover.html", outfits=SAMPLE_OUTFITS, tags=tags)


# ---------- accounts ----------

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        error = None
        if not 3 <= len(username) <= 30:
            error = "Username must be 3 to 30 characters."
        elif "@" not in email:
            error = "Please enter a valid email."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif User.query.filter((User.username == username) | (User.email == email)).first():
            error = "That username or email is already taken."
        if error:
            flash(error)
        else:
            user = User(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
            )
            db.session.add(user)
            db.session.commit()
            login_user(user)
            return redirect(url_for("home"))
    return render_template("auth.html", mode="signup")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for("home"))
        flash("Wrong email or password.")
    return render_template("auth.html", mode="login")


@app.route("/logout", methods=["POST"])
def logout():
    logout_user()
    return redirect(url_for("welcome"))


if __name__ == "__main__":
    app.run(debug=True)