#!/bin/bash
set -e
cd "$(dirname "$0")"

# Remove any stale migrations and DB so we always start clean
rm -f db.sqlite3
rm -rf core/migrations/__pycache__
find core/migrations -name "*.py" ! -name "__init__.py" -delete 2>/dev/null || true

# Generate migrations from current models
python manage.py makemigrations core --noinput 2>&1

# Apply all migrations to fresh DB
python manage.py migrate --noinput 2>&1

# Create super admin if not present
python manage.py shell -c "
from django.contrib.auth.models import User
email = 'khemiri.mohamed.ensi@gmail.com'
if not User.objects.filter(email=email).exists():
    u = User.objects.create_superuser(
        username=email, email=email, password='admin123',
        first_name='Admin', last_name='GoldenProxies'
    )
    print('Created super admin:', email)
else:
    print('Super admin already exists:', email)
" 2>&1 || true

# Restore all tables from TinyDB persistent snapshot (survives db.sqlite3 wipes).
# Source of truth between restarts; will be replaced by Postgres later.
python scripts/restore_from_tinydb.py 2>&1 || true

python manage.py runserver 0.0.0.0:${PORT:-24289}
