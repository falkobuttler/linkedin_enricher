from pathlib import Path

DATA_DIR = Path.home() / ".linkedin_enricher"
DB_PATH = DATA_DIR / "state.db"
PHOTOS_DIR = DATA_DIR / "photos"

# Rate limiting: conservative to avoid LinkedIn blocking
RATE_LIMIT_RPM = 8
BATCH_SIZE = 20
BATCH_PAUSE_SECONDS = 60
MIN_CONFIDENCE_DEFAULT = 0.40

# Image processing
PHOTO_SIZE_PX = 512

# Review server
FLASK_PORT = 5000

# Ensure directories exist
DATA_DIR.mkdir(exist_ok=True)
PHOTOS_DIR.mkdir(exist_ok=True)
