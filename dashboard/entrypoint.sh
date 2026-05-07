#!/bin/bash
set -e

echo "==> Waiting for database..."
until python -c "import psycopg2; psycopg2.connect(dbname='$DB_NAME', user='$DB_USER', password='$DB_PASSWORD', host='$DB_HOST', port='$DB_PORT')" 2>/dev/null; do
  sleep 1
done
echo "==> Database ready."

echo "==> Running migrations..."
python manage.py migrate --noinput

echo "==> Collecting static files..."
python manage.py collectstatic --noinput

echo "==> Creating default superuser if needed..."
python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username='admin').exists():
    User.objects.create_superuser('admin', 'admin@tradingagents.local', 'admin')
    print('Superuser created: admin / admin')
else:
    print('Superuser already exists.')
" || true

echo "==> Setting up Celery Beat schedules..."
python manage.py shell -c "
from django_celery_beat.models import PeriodicTask, CrontabSchedule, IntervalSchedule
import json

# Daily analysis — 9:00 AM Mon-Fri
s1, _ = CrontabSchedule.objects.get_or_create(minute='0', hour='9', day_of_week='1-5', day_of_month='*', month_of_year='*')
PeriodicTask.objects.get_or_create(name='Daily Analysis Run', defaults={'crontab': s1, 'task': 'trading.run_daily_analysis', 'args': json.dumps([]), 'kwargs': json.dumps({'mode': 'full', 'dry_run': True})})

# Wheel cycle — 9:15 AM Mon-Fri
s2, _ = CrontabSchedule.objects.get_or_create(minute='15', hour='9', day_of_week='1-5', day_of_month='*', month_of_year='*')
PeriodicTask.objects.get_or_create(name='Wheel Strategy Cycle', defaults={'crontab': s2, 'task': 'trading.run_wheel_cycle'})

# Trailing stops — every 5 min
s3, _ = IntervalSchedule.objects.get_or_create(every=5, period=IntervalSchedule.MINUTES)
PeriodicTask.objects.get_or_create(name='Update Trailing Stops', defaults={'interval': s3, 'task': 'trading.update_trailing_stops'})

# Position sync — every 1 min
s4, _ = IntervalSchedule.objects.get_or_create(every=1, period=IntervalSchedule.MINUTES)
PeriodicTask.objects.get_or_create(name='Sync Alpaca Positions', defaults={'interval': s4, 'task': 'trading.sync_alpaca_positions'})

# Capitol Trades fetch — 8:00 AM daily
s5, _ = CrontabSchedule.objects.get_or_create(minute='0', hour='8', day_of_week='*', day_of_month='*', month_of_year='*')
PeriodicTask.objects.get_or_create(name='Fetch Capitol Trades', defaults={'crontab': s5, 'task': 'trading.fetch_capitol_trades'})

print('Celery Beat schedules configured.')
" || true

echo "==> Starting server..."
exec "$@"
