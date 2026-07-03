import os

import firebase_admin
from dotenv import load_dotenv
from firebase_admin import credentials, firestore


def init_db():
    load_dotenv()
    json_path = os.path.abspath(
        os.path.join(os.getcwd(), "..", "mbo-player-firebase-adminsdk-fbsvc-4ce86ceba5.json")
    )
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


def main():
    db = init_db()
    deleted = 0

    while True:
        docs = list(db.collection("punishments").limit(450).stream())
        if not docs:
            break

        batch = db.batch()
        for snapshot in docs:
            batch.delete(snapshot.reference)
        batch.commit()
        deleted += len(docs)

    print(f"deleted {deleted} punishment records")


if __name__ == "__main__":
    main()
