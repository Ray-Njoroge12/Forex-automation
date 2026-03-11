from core.env_loader import load_runtime_env
load_runtime_env()
from core.credentials import load_mt5_credentials_from_env
import MetaTrader5 as mt5
creds = load_mt5_credentials_from_env()
if not mt5.initialize(login=creds.login, password=creds.password, server=creds.server):
    print('INIT_FAIL', mt5.last_error())
    raise SystemExit(2)
try:
    positions = mt5.positions_get() or []
    print({'open_positions_count': len(positions), 'positions': [
        {
            'ticket': int(p.ticket),
            'symbol': p.symbol,
            'dir': 'BUY' if int(p.type)==int(mt5.POSITION_TYPE_BUY) else 'SELL',
            'entry': float(p.price_open),
            'current': float(p.price_current),
            'sl': float(p.sl or 0.0),
            'tp': float(p.tp or 0.0),
            'profit': float(p.profit),
            'volume': float(p.volume),
        } for p in positions
    ]})
finally:
    mt5.shutdown()
