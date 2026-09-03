import os
import re
import secrets
import time
from urllib.parse import urlencode

import requests
from flask import Flask, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

KROGER_CLIENT_ID = os.environ.get("KROGER_CLIENT_ID", "")
KROGER_CLIENT_SECRET = os.environ.get("KROGER_CLIENT_SECRET", "")
KROGER_REDIRECT_URI = os.environ.get(
    "KROGER_REDIRECT_URI",
    "https://grocery-bridge.onrender.com/callback",
)
KROGER_API = "https://api.kroger.com/v1"
KROGER_AUTHORIZE = f"{KROGER_API}/connect/oauth2/authorize"
KROGER_TOKEN = f"{KROGER_API}/connect/oauth2/token"
KROGER_SCOPES = "product.compact cart.basic:write profile.compact"

# Central Park King Soopers, 10406 E Martin Luther King Jr Blvd, Denver CO 80238
# Kroger store URL identifies it as division 620 / store 00123.
DEFAULT_LOCATION_ID = "62000123"
DEFAULT_STORE_NAME = "King Soopers – Central Park"
DEFAULT_STORE_ADDRESS = "10406 E Martin Luther King Jr Blvd, Denver, CO 80238"

# Simple server-side token store for this personal prototype.
# Render may clear it when the service restarts/spins down, in which case just reconnect.
TOKENS = {}

CATEGORIES = {
    "produce", "herbs & spices", "pasta & grain", "condiments & oils",
    "bakery & bread", "meat", "baking", "cheese", "dairy & eggs", "other"
}

PREP_AFTER_COMMA = re.compile(
    r",\s*(cut into chunks|diced|minced|juiced|sliced|halved|beaten|crumbled|chopped|peeled|divided|rinsed|drained).*?$",
    re.IGNORECASE,
)

LEADING_AMOUNT = re.compile(
    r"^\s*(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?|[¼½¾⅓⅔⅛⅜⅝⅞])\s*"
    r"(?:cups?|tbsp|tablespoons?|tsp|teaspoons?|lbs?|pounds?|oz|ounces?|pints?|quarts?|gallons?|"
    r"cloves?|cans?|packages?|pkgs?|bags?|bunch(?:es)?|heads?|slices?|pieces?)?\s*",
    re.IGNORECASE,
)


def search_term_for(line):
    term = PREP_AFTER_COMMA.sub("", line).strip()
    term = re.sub(r",?\s*optional\b", "", term, flags=re.I).strip()
    term = LEADING_AMOUNT.sub("", term).strip()
    term = re.sub(r"^(?:large|medium|small)\s+", "", term, flags=re.I)
    term = re.sub(r"\b(?:uncooked|beaten|crumbled|dried)\b\s*", "", term, flags=re.I)
    term = re.sub(r"\s+", " ", term).strip(" ,.-")

    # A few recipe-language phrases search much better with a grocery-style name.
    replacements = {
        "garlic cloves": "garlic",
        "short-grain or jasmine rice": "jasmine rice",
        "tin foil": "aluminum foil",
    }
    lower = term.lower()
    if lower in replacements:
        term = replacements[lower]
    return term


def parse_list(text):
    items = []
    category = "Other"
    for raw in text.splitlines():
        line = raw.strip().strip("*").strip()
        if not line:
            continue
        if line.lower() in CATEGORIES:
            category = line.title()
            continue

        optional = bool(re.search(r"\boptional\b", line, re.I))
        search_term = search_term_for(line)
        if not search_term:
            continue
        items.append({
            "category": category,
            "original": line,
            "cleaned": search_term,
            "optional": optional,
        })
    return items


def token_for_session():
    sid = session.get("sid")
    if not sid:
        return None
    token = TOKENS.get(sid)
    if not token:
        return None
    if token.get("expires_at", 0) <= time.time() + 30:
        return None
    return token


def kroger_headers():
    token = token_for_session()
    if not token:
        return None
    return {"Authorization": f"Bearer {token['access_token']}", "Accept": "application/json"}


def product_choices(term, limit=4):
    headers = kroger_headers()
    if not headers:
        return []
    response = requests.get(
        f"{KROGER_API}/products",
        headers=headers,
        params={
            "filter.term": term,
            "filter.locationId": DEFAULT_LOCATION_ID,
            "filter.limit": limit,
        },
        timeout=20,
    )
    response.raise_for_status()
    choices = []
    for product in response.json().get("data", []):
        item = (product.get("items") or [{}])[0]
        price = item.get("price") or {}
        inventory = item.get("inventory") or {}
        image_url = None
        for image in product.get("images") or []:
            if image.get("perspective") == "front" or image.get("featured"):
                sizes = image.get("sizes") or []
                preferred = next((s for s in sizes if s.get("size") in ("medium", "small")), None)
                if preferred:
                    image_url = preferred.get("url")
                    break
        choices.append({
            "upc": product.get("upc") or product.get("productId"),
            "description": product.get("description", "Product"),
            "brand": product.get("brand", ""),
            "size": item.get("size", ""),
            "price": price.get("promo") or price.get("regular"),
            "stock": inventory.get("stockLevel", ""),
            "image": image_url,
        })
    return choices


@app.route("/", methods=["GET", "POST"])
def index():
    text = ""
    items = []
    if request.method == "POST":
        text = request.form.get("shopping_list", "")
        items = parse_list(text)
    return render_template(
        "index.html",
        text=text,
        items=items,
        connected=token_for_session() is not None,
        connected_profile=session.get("kroger_profile_id"),
        oauth_error=session.pop("oauth_error", None),
        cart_message=session.pop("cart_message", None),
        store_name=DEFAULT_STORE_NAME,
        store_address=DEFAULT_STORE_ADDRESS,
    )


@app.post("/match")
def match_products():
    if not token_for_session():
        session["oauth_error"] = "Please connect King Soopers before matching products."
        return redirect(url_for("index"))

    text = request.form.get("shopping_list", "")
    items = parse_list(text)
    matched = []
    for item in items:
        try:
            choices = product_choices(item["cleaned"])
        except requests.RequestException as exc:
            choices = []
            item["error"] = str(exc)
        item["choices"] = choices
        matched.append(item)

    return render_template(
        "match.html",
        items=matched,
        text=text,
        store_name=DEFAULT_STORE_NAME,
        store_address=DEFAULT_STORE_ADDRESS,
    )


@app.post("/add-to-cart")
def add_to_cart():
    if not token_for_session():
        session["oauth_error"] = "Your King Soopers connection expired. Please reconnect."
        return redirect(url_for("index"))

    upcs = request.form.getlist("selected_upc")
    items = []
    for upc in upcs:
        if upc:
            items.append({"upc": upc, "quantity": 1, "modality": "PICKUP"})

    if not items:
        session["cart_message"] = "No products were selected."
        return redirect(url_for("index"))

    try:
        response = requests.put(
            f"{KROGER_API}/cart/add",
            headers={**kroger_headers(), "Content-Type": "application/json"},
            json={"items": items},
            timeout=20,
        )
        response.raise_for_status()
        session["cart_message"] = f"✅ Added {len(items)} selected product{'s' if len(items) != 1 else ''} to your King Soopers cart."
    except requests.RequestException as exc:
        detail = ""
        if getattr(exc, "response", None) is not None:
            detail = f" ({exc.response.status_code}: {exc.response.text[:300]})"
        session["cart_message"] = f"Could not add products to cart{detail}"

    return redirect(url_for("index"))


@app.get("/connect")
def connect():
    if not KROGER_CLIENT_ID or not KROGER_CLIENT_SECRET:
        session["oauth_error"] = "Kroger credentials are missing in Render environment variables."
        return redirect(url_for("index"))

    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    params = {
        "scope": KROGER_SCOPES,
        "response_type": "code",
        "client_id": KROGER_CLIENT_ID,
        "redirect_uri": KROGER_REDIRECT_URI,
        "state": state,
    }
    return redirect(f"{KROGER_AUTHORIZE}?{urlencode(params)}")


@app.get("/callback")
def callback():
    error = request.args.get("error")
    if error:
        session["oauth_error"] = request.args.get("error_description", error)
        return redirect(url_for("index"))

    state = request.args.get("state")
    expected_state = session.pop("oauth_state", None)
    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        session["oauth_error"] = "Kroger sign-in failed: invalid OAuth state."
        return redirect(url_for("index"))

    code = request.args.get("code")
    if not code:
        session["oauth_error"] = "Kroger did not return an authorization code."
        return redirect(url_for("index"))

    try:
        response = requests.post(
            KROGER_TOKEN,
            auth=(KROGER_CLIENT_ID, KROGER_CLIENT_SECRET),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": KROGER_REDIRECT_URI,
            },
            timeout=20,
        )
        response.raise_for_status()
        token = response.json()
        token["expires_at"] = time.time() + int(token.get("expires_in", 1800))

        sid = session.get("sid") or secrets.token_urlsafe(24)
        session["sid"] = sid
        TOKENS[sid] = token

        profile = requests.get(
            f"{KROGER_API}/identity/profile",
            headers={"Authorization": f"Bearer {token['access_token']}"},
            timeout=20,
        )
        if profile.ok:
            session["kroger_profile_id"] = profile.json().get("data", {}).get("id")
    except requests.RequestException as exc:
        detail = ""
        if getattr(exc, "response", None) is not None:
            detail = f" ({exc.response.status_code}: {exc.response.text[:300]})"
        session["oauth_error"] = f"Could not connect to Kroger{detail}"

    return redirect(url_for("index"))


@app.get("/disconnect")
def disconnect():
    sid = session.pop("sid", None)
    if sid:
        TOKENS.pop(sid, None)
    session.pop("kroger_profile_id", None)
    return redirect(url_for("index"))


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
