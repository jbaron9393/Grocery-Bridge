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

# Simple server-side token store for this personal prototype.
# Render may clear it when the service restarts/spins down, in which case just reconnect.
TOKENS = {}

CATEGORIES = {
    "produce", "herbs & spices", "pasta & grain", "condiments & oils",
    "bakery & bread", "meat", "baking", "cheese", "dairy & eggs", "other"
}

PREP_WORDS = re.compile(
    r",?\s*(cut into chunks|diced|minced|juiced|sliced|halved|beaten|crumbled|uncooked)\b.*$",
    re.IGNORECASE,
)


def parse_list(text):
    items = []
    category = "Other"
    for raw in text.splitlines():
        line = raw.strip().strip("*").strip()
        if not line:
            continue
        if line.lower() in CATEGORIES:
            category = line
            continue

        cleaned = PREP_WORDS.sub("", line).strip().rstrip(",")
        optional = bool(re.search(r"\boptional\b", line, re.I))
        cleaned = re.sub(r",?\s*optional\b", "", cleaned, flags=re.I).strip()
        items.append({
            "category": category,
            "original": line,
            "cleaned": cleaned,
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
    )


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
