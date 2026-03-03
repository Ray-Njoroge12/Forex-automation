#property strict

#include <Trade/Trade.mqh>

input string RootFolder = "fx_ai_engine/bridge";
input double MaxSpreadPips = 2.0;
input int PollIntervalSeconds = 1;

CTrade trade;

string PendingFolder() { return RootFolder + "/pending_signals"; }
string LockFolder() { return RootFolder + "/active_locks"; }
string FeedbackFolder() { return RootFolder + "/feedback"; }

int OnInit()
{
   EventSetTimer(PollIntervalSeconds);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   ProcessPendingSignal();
   WriteAccountSnapshot();
}

bool ExtractJsonString(const string json, const string key, string &out)
{
   string pattern = "\"" + key + "\":";
   int start = StringFind(json, pattern);
   if(start < 0) return false;
   start += StringLen(pattern);

   while(start < StringLen(json) && (StringGetCharacter(json, start) == ' ' || StringGetCharacter(json, start) == '"'))
      start++;

   int end = start;
   while(end < StringLen(json))
   {
      ushort c = StringGetCharacter(json, end);
      if(c == '"' || c == ',' || c == '}')
         break;
      end++;
   }

   out = StringSubstr(json, start, end - start);
   return true;
}

bool ExtractJsonDouble(const string json, const string key, double &out)
{
   string value;
   if(!ExtractJsonString(json, key, value))
      return false;
   out = StringToDouble(value);
   return true;
}

bool ReadTextFile(const string filePath, string &content)
{
   int handle = FileOpen(filePath, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
      return false;

   content = FileReadString(handle);
   while(!FileIsEnding(handle))
      content += FileReadString(handle);

   FileClose(handle);
   return true;
}

bool WriteTextFile(const string filePath, const string content)
{
   int handle = FileOpen(filePath, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
      return false;

   FileWriteString(handle, content);
   FileClose(handle);
   return true;
}

bool GetFirstPendingSignalFile(string &fileName)
{
   string searchMask = PendingFolder() + "/*.json";
   long findHandle = FileFindFirst(searchMask, fileName, FILE_COMMON);
   if(findHandle == INVALID_HANDLE)
      return false;

   FileFindClose(findHandle);
   return true;
}

double PipValue()
{
   if(StringFind(_Symbol, "JPY") >= 0)
      return 0.01;
   return 0.0001;
}

double CalculateLot(const double riskPercent, const double stopPips)
{
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskAmount = balance * riskPercent;

   double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);

   if(tickValue <= 0 || tickSize <= 0 || point <= 0 || stopPips <= 0)
      return 0.0;

   double valuePerPoint = tickValue / tickSize * point;
   double stopPoints = stopPips * PipValue() / point;
   double rawLot = riskAmount / (stopPoints * valuePerPoint);

   double minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lotStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   if(rawLot < minLot)
      return 0.0;

   double lot = MathFloor(rawLot / lotStep) * lotStep;
   if(lot > maxLot)
      lot = maxLot;

   return NormalizeDouble(lot, 2);
}

void WriteExecutionFeedback(
   const string tradeId,
   const ulong ticket,
   const string status,
   const double entryPrice,
   const double slippage,
   const double spreadAtEntry,
   const double profitLoss,
   const double rMultiple)
{
   string payload = "{";
   payload += "\"trade_id\":\"" + tradeId + "\",";
   payload += "\"ticket\":" + StringFormat("%I64u", ticket) + ",";
   payload += "\"status\":\"" + status + "\",";
   payload += "\"entry_price\":" + DoubleToString(entryPrice, _Digits) + ",";
   payload += "\"slippage\":" + DoubleToString(slippage, 6) + ",";
   payload += "\"spread_at_entry\":" + DoubleToString(spreadAtEntry, 6) + ",";
   payload += "\"profit_loss\":" + DoubleToString(profitLoss, 2) + ",";
   payload += "\"r_multiple\":" + DoubleToString(rMultiple, 4) + ",";
   payload += "\"close_time\":\"" + TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS) + "\"";
   payload += "}";

   string filePath = FeedbackFolder() + "/execution_" + tradeId + ".json";
   WriteTextFile(filePath, payload);
}

void WriteAccountSnapshot()
{
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double marginFree = AccountInfoDouble(ACCOUNT_MARGIN_FREE);

   int positions = PositionsTotal();
   double floating = 0.0;
   for(int i = 0; i < positions; i++)
   {
      if(PositionGetTicket(i) > 0)
         floating += PositionGetDouble(POSITION_PROFIT);
   }

   string payload = "{";
   payload += "\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS) + "\",";
   payload += "\"balance\":" + DoubleToString(balance, 2) + ",";
   payload += "\"equity\":" + DoubleToString(equity, 2) + ",";
   payload += "\"margin_free\":" + DoubleToString(marginFree, 2) + ",";
   payload += "\"open_positions_count\":" + IntegerToString(positions) + ",";
   payload += "\"floating_pnl\":" + DoubleToString(floating, 2);
   payload += "}";

   WriteTextFile(FeedbackFolder() + "/account_snapshot.json", payload);
}

void ProcessPendingSignal()
{
   string fileName;
   if(!GetFirstPendingSignalFile(fileName))
      return;

   string filePath = PendingFolder() + "/" + fileName;

   string content;
   if(!ReadTextFile(filePath, content))
      return;

   string tradeId, symbol, direction;
   double riskPercent = 0.0;
   double stopPips = 0.0;
   double takeProfitPips = 0.0;
   double requestedLot = -1.0;

   if(!ExtractJsonString(content, "trade_id", tradeId)) return;
   if(!ExtractJsonString(content, "symbol", symbol)) return;
   if(!ExtractJsonString(content, "direction", direction)) return;
   if(!ExtractJsonDouble(content, "risk_percent", riskPercent)) return;
   if(!ExtractJsonDouble(content, "stop_pips", stopPips)) return;
   if(!ExtractJsonDouble(content, "take_profit_pips", takeProfitPips)) return;
   ExtractJsonDouble(content, "lot", requestedLot);

   if(symbol != _Symbol)
      return;

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return;

   double spread = tick.ask - tick.bid;
   double spreadPips = spread / PipValue();
   if(spreadPips > MaxSpreadPips)
   {
      WriteExecutionFeedback(tradeId, 0, "REJECTED_SPREAD", 0.0, 0.0, spread, 0.0, 0.0);
      FileDelete(filePath, FILE_COMMON);
      FileDelete(LockFolder() + "/" + tradeId + ".lock", FILE_COMMON);
      return;
   }

   double recalculatedLot = CalculateLot(riskPercent, stopPips);
   if(recalculatedLot <= 0.0)
   {
      WriteExecutionFeedback(tradeId, 0, "REJECTED_LOT", 0.0, 0.0, spread, 0.0, 0.0);
      FileDelete(filePath, FILE_COMMON);
      FileDelete(LockFolder() + "/" + tradeId + ".lock", FILE_COMMON);
      return;
   }

   double finalLot = recalculatedLot;
   if(requestedLot > 0.0 && requestedLot < recalculatedLot)
      finalLot = requestedLot;

   double entry = (direction == "BUY") ? tick.ask : tick.bid;
   double sl = 0.0;
   double tp = 0.0;

   if(direction == "BUY")
   {
      sl = entry - stopPips * PipValue();
      tp = entry + takeProfitPips * PipValue();
   }
   else if(direction == "SELL")
   {
      sl = entry + stopPips * PipValue();
      tp = entry - takeProfitPips * PipValue();
   }
   else
   {
      WriteExecutionFeedback(tradeId, 0, "REJECTED_DIRECTION", 0.0, 0.0, spread, 0.0, 0.0);
      FileDelete(filePath, FILE_COMMON);
      FileDelete(LockFolder() + "/" + tradeId + ".lock", FILE_COMMON);
      return;
   }

   bool placed = false;
   if(direction == "BUY")
      placed = trade.Buy(finalLot, _Symbol, entry, sl, tp, tradeId);
   else
      placed = trade.Sell(finalLot, _Symbol, entry, sl, tp, tradeId);

   if(!placed)
   {
      WriteExecutionFeedback(tradeId, 0, "REJECTED_ORDER_SEND", 0.0, 0.0, spread, 0.0, 0.0);
      FileDelete(filePath, FILE_COMMON);
      FileDelete(LockFolder() + "/" + tradeId + ".lock", FILE_COMMON);
      return;
   }

   double fillPrice = trade.ResultPrice();
   double slippage = MathAbs(fillPrice - entry);
   ulong ticket = trade.ResultDeal();
   if(ticket == 0)
      ticket = trade.ResultOrder();
   WriteExecutionFeedback(tradeId, ticket, "EXECUTED", fillPrice, slippage, spread, 0.0, 0.0);

   FileDelete(filePath, FILE_COMMON);
   FileDelete(LockFolder() + "/" + tradeId + ".lock", FILE_COMMON);
}
