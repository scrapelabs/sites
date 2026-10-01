"""Fast, database-free Bearer authentication for Caddy's model API proxy."""

import hashlib
import hmac
import re
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


def _error(message, status):
    response = JsonResponse({
        'error': {
            'message': message,
            'type': 'authentication_error' if status == 401 else 'service_unavailable',
        },
    }, status=status)
    if status == 401:
        response['WWW-Authenticate'] = 'Bearer realm="Permitlify AI"'
    return response


@never_cache
@require_GET
def authorize(request):
    """Return 204 only for the dedicated key; never receive inference bodies."""
    hash_file = Path(getattr(
        settings, 'PERMITLIFY_AI_KEY_HASH_FILE',
        settings.BASE_DIR / 'open_oss_20b' / '.secrets' / 'api-key.sha256',
    ))
    try:
        expected = hash_file.read_text(encoding='ascii').strip()
    except (OSError, UnicodeError):
        return _error('AI API authentication is not configured.', 503)
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        return _error('AI API authentication is not configured.', 503)

    authorization = request.headers.get('Authorization', '')
    parts = authorization.split()
    if (len(parts) != 2 or parts[0].lower() != 'bearer'
            or len(parts[1]) > 256 or not parts[1].isascii()):
        return _error('A valid dedicated Bearer API key is required.', 401)
    actual = hashlib.sha256(parts[1].encode('ascii')).hexdigest()
    if not hmac.compare_digest(actual, expected):
        return _error('A valid dedicated Bearer API key is required.', 401)
    return HttpResponse(status=204)
