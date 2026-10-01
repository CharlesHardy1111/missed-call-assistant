"""UTC persistence and explicit business-timezone presentation."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def twilio_event_time(value):
    """Voice status callbacks supply RFC 2822 Timestamp; never guess a timezone."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def display_timestamp(value, timezone_name):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError('Missing timezone')
    except (ValueError, TypeError):
        # Historical rows discarded their timezone. Preserve rather than invent it.
        return {'text': f'{value or "Unavailable"} (legacy timestamp; timezone unknown)',
                'utc': None}
    utc = parsed.astimezone(timezone.utc)
    local = utc.astimezone(ZoneInfo(timezone_name))
    # Explicit 12-hour formatting, independent of server locale / OS strftime flags.
    months = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
    text = (f'{months[local.month - 1]} {local.day:02d}, {local.year} '
            f'{local.hour % 12 or 12:02d}:{local.minute:02d} '
            f'{"AM" if local.hour < 12 else "PM"} '
            f'{local.tzname()} ({timezone_name})')
    return {'text': text, 'utc': utc.isoformat()}
