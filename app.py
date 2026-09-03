import os
import re
from flask import Flask, render_template, request

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

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


@app.route("/", methods=["GET", "POST"])
def index():
    text = ""
    items = []
    if request.method == "POST":
        text = request.form.get("shopping_list", "")
        items = parse_list(text)
    return render_template("index.html", text=text, items=items)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
