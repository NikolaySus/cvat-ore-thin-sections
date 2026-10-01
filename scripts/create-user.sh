#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/compose.sh exec -T cvat_server python manage.py shell -c \
"from django.contrib.auth import get_user_model; U=get_user_model(); u,created=U.objects.get_or_create(username='ore', defaults={'email':'ore@example.local', 'is_staff':True, 'is_superuser':True}); u.set_password('ore-local-demo'); u.save(); print('Local account: ore')"
