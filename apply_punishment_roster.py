import os

import firebase_admin
from dotenv import load_dotenv
from firebase_admin import credentials, firestore


def normalize(value):
    return str(value or "").strip()


def init_db():
    load_dotenv()
    json_path = os.path.abspath(os.path.join(os.getcwd(), "..", "mbo-player-firebase-adminsdk-fbsvc-4ce86ceba5.json"))
    if os.path.exists(json_path):
        firebase_admin.initialize_app(credentials.Certificate(json_path))
    else:
        private_key = os.getenv("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n")
        firebase_admin.initialize_app(
            credentials.Certificate(
                {
                    "type": "service_account",
                    "project_id": os.getenv("FIREBASE_PROJECT_ID"),
                    "private_key": private_key,
                    "client_email": os.getenv("FIREBASE_CLIENT_EMAIL"),
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            )
        )
    return firestore.client()


def is_roster_removal(record):
    text = " ".join(
        normalize(record.get(key))
        for key in ["status", "penalty", "note", "releaseDateText", "releaseDate"]
    )
    return "중" in normalize(record.get("status")) and not normalize(record.get("pardon")) and any(
        keyword in text for keyword in ["영구 제명", "서버 접속 금지", "무기한"]
    )


def punishment_text(record):
    date = normalize(record.get("dateText") or record.get("date")) or "-"
    return f"{date} 처벌: {normalize(record.get('reason')) or '-'} / {normalize(record.get('penalty')) or '-'}"


def main():
    db = init_db()
    updated = 0
    records = [doc.to_dict() for doc in db.collection("punishments").stream()]
    for record in records:
        if not is_roster_removal(record):
            continue
        players = list(db.collection("players").where("name", "==", normalize(record.get("nickname"))).stream())
        for player_doc in players:
            db.collection("players").document(player_doc.id).set(
                {
                    "team": "무소속",
                    "transfer": punishment_text(record),
                    "forcedReleaseOriginalTeam": "",
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
            updated += 1
    print(f"updated {updated} roster records")


if __name__ == "__main__":
    main()