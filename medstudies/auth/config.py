import os

SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY: str = os.environ.get("SUPABASE_ANON_KEY", "")
MEDSTUDIES_BASE_URL: str = os.environ.get("MEDSTUDIES_BASE_URL", "http://localhost:8000")

# Cookie names
COOKIE_ACCESS = "ms_access"
COOKIE_REFRESH = "ms_refresh"

# Cookie settings
COOKIE_HTTPONLY = True
COOKIE_SECURE = MEDSTUDIES_BASE_URL.startswith("https")
COOKIE_SAMESITE = "lax"
ACCESS_MAX_AGE = 3600        # 1 hour
REFRESH_MAX_AGE = 30 * 86400  # 30 days
