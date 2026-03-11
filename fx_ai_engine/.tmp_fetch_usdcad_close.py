from datetime import datetime, timedelta, timezone
from core.env_loader import load_runtime_env
load_runtime_env()
from core.credentials import load_mt5_credentials_from_env
import MetaTrader5 as mt5
creds = load_mt5_credentials_from_env()
if not mt5.initialize(login=creds.login, password=creds.password, server=creds.server):
    print('INIT_FAIL', mt5.last_error())
    raise SystemExit(2)
try:
    date_to = datetime.now(timezone.utc) + timedelta(minutes=5)
    date_from = date_to - timedelta(hours=12)
    deals = mt5.history_deals_get(date_from, date_to) or []
    for d in deals:
        pos_id = int(getattr(d, 'position_id', 0) or 0)
        if pos_id in {1810829, 1811109}:
            print({
                'deal_ticket': int(getattr(d,'ticket',0) or 0),
                'position_id': pos_id,
                'entry_flag': int(getattr(d,'entry',-1)),
                'type': int(getattr(d,'type',-1)),
                'price': float(getattr(d,'price',0.0) or 0.0),
                'profit': float(getattr(d,'profit',0.0) or 0.0),
                'time': int(getattr(d,'time',0) or 0),
                'comment': str(getattr(d,'comment','') or '')
            })
finally:
    mt5.shutdown()
