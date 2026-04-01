from datetime import datetime

from peewee import (
    BooleanField,
    DateTimeField,
    FloatField,
    ForeignKeyField,
    IntegerField,
    Model,
    SqliteDatabase,
    TextField,
)

from .config import DB_PATH

db = SqliteDatabase(str(DB_PATH), pragmas={"journal_mode": "wal", "foreign_keys": 1})


class BaseModel(Model):
    class Meta:
        database = db


class Contact(BaseModel):
    id = TextField(primary_key=True)  # AppleScript GUID
    full_name = TextField()
    organization = TextField(null=True)
    email = TextField(null=True)
    has_photo = BooleanField(default=False)
    exported_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "contacts"


class LinkedinMatch(BaseModel):
    contact = ForeignKeyField(Contact, backref="matches", column_name="contact_id")
    linkedin_url = TextField(null=True)
    linkedin_name = TextField(null=True)
    headline = TextField(null=True)
    current_title = TextField(null=True)
    current_company = TextField(null=True)
    photo_url = TextField(null=True)
    photo_local = TextField(null=True)
    confidence = FloatField(default=0.0)
    # pending / approved / rejected / skipped / applied / error
    status = TextField(default="pending")
    searched_at = DateTimeField(default=datetime.utcnow)
    error = TextField(null=True)

    class Meta:
        table_name = "linkedin_matches"


class RunLog(BaseModel):
    stage = TextField()
    started_at = DateTimeField(default=datetime.utcnow)
    finished_at = DateTimeField(null=True)
    contacts_total = IntegerField(default=0)
    contacts_done = IntegerField(default=0)

    class Meta:
        table_name = "run_log"


def init_db():
    with db:
        db.create_tables([Contact, LinkedinMatch, RunLog], safe=True)
        # Migrations: add columns introduced after initial schema
        for col, definition in [
            ("current_title", "TEXT"),
            ("current_company", "TEXT"),
        ]:
            try:
                db.execute_sql(
                    f"ALTER TABLE linkedin_matches ADD COLUMN {col} {definition}"
                )
            except Exception:
                pass  # column already exists


def get_pending_matches():
    return (
        LinkedinMatch.select(LinkedinMatch, Contact)
        .join(Contact)
        .where(LinkedinMatch.status == "pending")
        .order_by(LinkedinMatch.confidence.desc())
    )


def get_approved_matches():
    return (
        LinkedinMatch.select(LinkedinMatch, Contact)
        .join(Contact)
        .where(LinkedinMatch.status == "approved")
        .order_by(LinkedinMatch.id)
    )


def summary():
    total = Contact.select().count()
    searched = LinkedinMatch.select().count()
    pending = LinkedinMatch.select().where(LinkedinMatch.status == "pending").count()
    approved = LinkedinMatch.select().where(LinkedinMatch.status == "approved").count()
    rejected = LinkedinMatch.select().where(LinkedinMatch.status == "rejected").count()
    skipped = LinkedinMatch.select().where(LinkedinMatch.status == "skipped").count()
    applied = LinkedinMatch.select().where(LinkedinMatch.status == "applied").count()
    errors = LinkedinMatch.select().where(LinkedinMatch.status == "error").count()
    return {
        "contacts": total,
        "searched": searched,
        "pending_review": pending,
        "approved": approved,
        "rejected": rejected,
        "skipped": skipped,
        "applied": applied,
        "errors": errors,
    }
