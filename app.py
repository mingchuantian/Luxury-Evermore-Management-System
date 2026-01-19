import os

from luxury_app import create_app

app = create_app()


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "").strip() in ("1", "true", "True", "yes", "on")
    app.run(debug=debug, host="0.0.0.0", port=int(os.getenv("PORT", "5001")))


