//+------------------------------------------------------------------+
//| FX_HistoryExport.mq5 - one-off script: export broker bar history  |
//| for replay research (plan build E1). Read-only; places no orders. |
//|                                                                  |
//| Output: MQL5/Files/history/<SYMBOL>_<TF>.csv with raw server-time |
//| epoch seconds, plus <SYMBOL>_<TF>.meta.json. Convert to UTC with  |
//| python -m validation.convert_mt5_history (handles server DST).    |
//| Raise Tools > Options > Charts > "Max bars in chart" if M15 rows  |
//| stop short of the requested years.                               |
//+------------------------------------------------------------------+
#property script_show_inputs
#property strict

input int    YearsBack = 5;
input string OutFolder = "history";

string          g_symbols[] = {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"};
ENUM_TIMEFRAMES g_tfs[]     = {PERIOD_M15, PERIOD_H1, PERIOD_H4, PERIOD_D1};

string TfName(const ENUM_TIMEFRAMES tf)
{
   switch(tf)
   {
      case PERIOD_M15: return "M15";
      case PERIOD_H1:  return "H1";
      case PERIOD_H4:  return "H4";
      case PERIOD_D1:  return "D1";
      default:         return "";
   }
}

// CopyRates may return partial data while the terminal downloads history.
int CopyStable(const string sym, const ENUM_TIMEFRAMES tf, const datetime from, const datetime to, MqlRates &rates[])
{
   int last = -1;
   for(int attempt = 0; attempt < 20 && !IsStopped(); attempt++)
   {
      int n = CopyRates(sym, tf, from, to, rates);
      if(n > 0 && n == last)
         return n;
      last = n;
      Sleep(1500);
   }
   return last;
}

void OnStart()
{
   datetime to = TimeCurrent();
   datetime from = to - (datetime)((long)YearsBack * 365 * 86400);
   long offset = (long)(MathRound((double)(TimeTradeServer() - TimeGMT()) / 900.0) * 900.0);
   FolderCreate(OutFolder);

   for(int s = 0; s < ArraySize(g_symbols); s++)
   {
      string sym = g_symbols[s];
      if(!SymbolSelect(sym, true))
      {
         Print("FX_HistoryExport: cannot select ", sym);
         continue;
      }
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      for(int t = 0; t < ArraySize(g_tfs); t++)
      {
         MqlRates rates[];
         ArraySetAsSeries(rates, false);
         int n = CopyStable(sym, g_tfs[t], from, to, rates);
         if(n <= 0)
         {
            Print("FX_HistoryExport: no data for ", sym, " ", TfName(g_tfs[t]), " err=", GetLastError());
            continue;
         }
         // Drop the bar that is still forming.
         datetime forming = iTime(sym, g_tfs[t], 0);
         int rows = (rates[n - 1].time == forming) ? n - 1 : n;

         string base = OutFolder + "\\" + sym + "_" + TfName(g_tfs[t]);
         int h = FileOpen(base + ".csv", FILE_WRITE | FILE_TXT | FILE_ANSI);
         if(h == INVALID_HANDLE)
         {
            Print("FX_HistoryExport: cannot write ", base, ".csv err=", GetLastError());
            continue;
         }
         FileWriteString(h, "server_time,open,high,low,close,tick_volume,spread_points\r\n");
         for(int i = 0; i < rows; i++)
         {
            FileWriteString(h,
               IntegerToString((long)rates[i].time) + "," +
               DoubleToString(rates[i].open, digits) + "," + DoubleToString(rates[i].high, digits) + "," +
               DoubleToString(rates[i].low, digits) + "," + DoubleToString(rates[i].close, digits) + "," +
               IntegerToString(rates[i].tick_volume) + "," + IntegerToString(rates[i].spread) + "\r\n");
         }
         FileClose(h);

         int m = FileOpen(base + ".meta.json", FILE_WRITE | FILE_TXT | FILE_ANSI);
         if(m != INVALID_HANDLE)
         {
            FileWriteString(m, "{\"schema_version\":1,\"symbol\":\"" + sym + "\",\"timeframe\":\"" + TfName(g_tfs[t]) + "\"," +
               "\"rows\":" + IntegerToString(rows) + ",\"first_server_time\":" + IntegerToString((long)rates[0].time) + "," +
               "\"last_server_time\":" + IntegerToString((long)rates[rows - 1].time) + "," +
               "\"export_server_utc_offset_seconds\":" + IntegerToString(offset) + "," +
               "\"server\":\"" + AccountInfoString(ACCOUNT_SERVER) + "\",\"years_requested\":" + IntegerToString(YearsBack) + "}");
            FileClose(m);
         }
         Print("FX_HistoryExport: ", sym, " ", TfName(g_tfs[t]), " rows=", rows);
      }
   }
   Print("FX_HistoryExport: done");
}
