import MetaTrader5 as mt5
import os

def check_paths():
    if not mt5.initialize():
        print("initialize() failed, error code =", mt5.last_error())
        quit()
    
    terminal_info = mt5.terminal_info()
    if terminal_info:
        print(f"Data Path: {terminal_info.data_path}")
        print(f"Commmon Path: {terminal_info.commondatapath}")
    
    mt5.shutdown()

if __name__ == "__main__":
    check_paths()
