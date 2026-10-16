"""Run inside yonyou_browser.py after login; use saved query parameters."""
from pathlib import Path
from datetime import datetime
from fetch_yonyou import fetch_vouchers

output = Path(ROOT).parent / 'data/yonyou_refresh' / datetime.now().strftime('%Y%m%d_%H%M%S_%f') / 'vouchers'
RESULT = fetch_vouchers(context, output)
RESULT['saved'] = str(output / 'actual_vouchers.json')
