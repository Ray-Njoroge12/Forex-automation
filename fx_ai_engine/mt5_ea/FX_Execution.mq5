#property strict

#include <Trade/Trade.mqh>

input string RootFolder = "bridge";
input double MaxSpreadPips = 5.0; 
input int PollIntervalSeconds = 1;

// --- Trade Management Inputs ---
input double BreakEvenTriggerR = 1.0;     // Move SL to entry when profit reaches this R-multiple
input double BreakEvenBufferPips = 1.0;   // Buffer pips above entry for break-even SL
input double PartialCloseR = 1.5;         // Close 50% at this R-multiple (0 = disabled)
input double TrailingATRMultiplier = 2.0; // Trail SL using N * ATR (0 = disabled)
input int    TrailingATRPeriod = 14;       // ATR period for trailing calculation

CTrade trade;

// Per-position trade management parameters (indexed by slot 0..MAX_POS-1)
#define MAX_POS 10
ulong  g_tickets[MAX_POS];
double g_be_trigger_r[MAX_POS];
double g_partial_close_r[MAX_POS];
double g_trailing_atr_mult[MAX_POS];
bool   g_tp_mode_trail[MAX_POS];
bool   g_partial_closed[MAX_POS];

void InitPositionArrays()
{
   for(int i = 0; i < MAX_POS; i++)
   {
      g_tickets[i]           = 0;
      g_be_trigger_r[i]      = 1.0;
      g_partial_close_r[i]   = 1.5;
      g_trailing_atr_mult[i] = 2.0;
      g_tp_mode_trail[i]     = false;
      g_partial_closed[i]    = false;
   }
}

int FindSlot(ulong ticket)
{
   for(int i = 0; i < MAX_POS; i++)
      if(g_tickets[i] == ticket) return i;
   return -1;
}

int AllocSlot(ulong ticket)
{
   for(int i = 0; i < MAX_POS; i++)
      if(g_tickets[i] == 0) { g_tickets[i] = ticket; return i; }
   return -1;
}

void FreeSlot(ulong ticket)
{
   int s = FindSlot(ticket);
   if(s >= 0)
   {
      g_tickets[s]           = 0;
      g_be_trigger_r[s]      = 1.0;
      g_partial_close_r[s]   = 1.5;
      g_trailing_atr_mult[s] = 2.0;
      g_tp_mode_trail[s]     = false;
      g_partial_closed[s]    = false;
   }
}

// Helper to sanitize paths
string CleanPath(string p) { return p; }

string PendingFolder() { return RootFolder + "\\pending_signals"; }
string LockFolder() { return RootFolder + "\\active_locks"; }
string FeedbackFolder() { return RootFolder + "\\feedback"; }
string ExitsFolder() { return RootFolder + "\\exits"; }

int OnInit()
{
   EventSetTimer(PollIntervalSeconds);
   LogDebug("OnInit: FX_Execution Started");
   InitPositionArrays();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
{
   if(trans.type == TRADE_TRANSACTION_HISTORY_ADD)
   {
      ulong deal_ticket = trans.deal;
      if(deal_ticket == 0) return;
      
      if(HistoryDealSelect(deal_ticket))
      {
         long deal_entry = HistoryDealGetInteger(deal_ticket, DEAL_ENTRY);
         if(deal_entry == DEAL_ENTRY_OUT || deal_entry == DEAL_ENTRY_INOUT || deal_entry == DEAL_ENTRY_OUT_BY)
         {
            ulong pos_ticket = HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID);
            double profit = HistoryDealGetDouble(deal_ticket, DEAL_PROFIT);
            double fee = HistoryDealGetDouble(deal_ticket, DEAL_FEE);
            double swap = HistoryDealGetDouble(deal_ticket, DEAL_SWAP);
            double commission = HistoryDealGetDouble(deal_ticket, DEAL_COMMISSION);
            
            double total_pl = profit + fee + swap + commission;
            
            datetime close_time = (datetime)HistoryDealGetInteger(deal_ticket, DEAL_TIME);
            
            WriteExitFeedback(pos_ticket, total_pl, close_time);
            FreeSlot(pos_ticket);
         }
      }
   }
}

void OnTimer()
{
   static datetime lastSnapshot = 0;
   
   ProcessPendingSignal();
   ManageOpenPositions();
   
   if(TimeCurrent() - lastSnapshot >= 5) { // Update every 5s
      WriteAccountSnapshot();
      lastSnapshot = TimeCurrent();
   }
}

//+------------------------------------------------------------------+
//| Active Trade Management: Break-Even, Partial Close, Trailing SL  |
//+------------------------------------------------------------------+
void ManageOpenPositions()
{
   int total = PositionsTotal();
   for(int i = total - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      
      string sym      = PositionGetString(POSITION_SYMBOL);
      long   posType  = PositionGetInteger(POSITION_TYPE);
      double openPrice= PositionGetDouble(POSITION_PRICE_OPEN);
      double currentSL= PositionGetDouble(POSITION_SL);
      double currentTP= PositionGetDouble(POSITION_TP);
      double volume   = PositionGetDouble(POSITION_VOLUME);
      int slot = FindSlot(ticket);
      double slotBE      = (slot >= 0) ? g_be_trigger_r[slot]      : BreakEvenTriggerR;
      double slotPartial = (slot >= 0) ? g_partial_close_r[slot]   : PartialCloseR;
      double slotTrail   = (slot >= 0) ? g_trailing_atr_mult[slot] : TrailingATRMultiplier;
      bool   slotClosed  = (slot >= 0) ? g_partial_closed[slot]    : false;

      // Calculate initial stop distance (R-unit) from entry and current SL
      double initialStopDist = MathAbs(openPrice - currentSL);
      if(initialStopDist <= 0) continue; // Safety: skip if no SL set
      
      // Get current price
      double currentPrice = 0.0;
      if(posType == POSITION_TYPE_BUY)
         currentPrice = SymbolInfoDouble(sym, SYMBOL_BID);
      else
         currentPrice = SymbolInfoDouble(sym, SYMBOL_ASK);
      
      if(currentPrice <= 0) continue;
      
      // Calculate current profit in R-multiples
      double profitDist = 0.0;
      if(posType == POSITION_TYPE_BUY)
         profitDist = currentPrice - openPrice;
      else
         profitDist = openPrice - currentPrice;
      
      double currentR = profitDist / initialStopDist;
      
      // --- 1. Break-Even Logic ---
      if(slotBE > 0 && currentR >= slotBE)
      {
         double pipVal = PipValue(sym);
         double newSL = 0.0;
         
         if(posType == POSITION_TYPE_BUY)
            newSL = openPrice + BreakEvenBufferPips * pipVal;
         else
            newSL = openPrice - BreakEvenBufferPips * pipVal;
         
         // Only modify if the new SL is better than the current one
         bool shouldModify = false;
         if(posType == POSITION_TYPE_BUY && newSL > currentSL)
            shouldModify = true;
         else if(posType == POSITION_TYPE_SELL && (newSL < currentSL || currentSL == 0))
            shouldModify = true;
         
         if(shouldModify)
         {
            // Respect minimum stop level
            long stopLevel = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
            double point = SymbolInfoDouble(sym, SYMBOL_POINT);
            double minDist = stopLevel * point;
            
            bool distOk = false;
            if(posType == POSITION_TYPE_BUY)
               distOk = (currentPrice - newSL) >= minDist;
            else
               distOk = (newSL - currentPrice) >= minDist;
            
            if(distOk)
            {
               int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
               newSL = NormalizeDouble(newSL, digits);
               if(trade.PositionModify(ticket, newSL, currentTP))
                  LogDebug("BE moved SL: ticket=" + (string)ticket + " newSL=" + DoubleToString(newSL, digits));
               else
                  LogDebug("BE modify fail: ticket=" + (string)ticket + " err=" + trade.ResultRetcodeDescription());
            }
         }
      }
      
      // --- 2. Partial Close at PartialCloseR ---
      if(slotPartial > 0 && currentR >= slotPartial && !slotClosed)
      {
         double minLot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
         double lotStep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
         double halfVol = MathFloor((volume * 0.5) / lotStep) * lotStep;

         if(halfVol >= minLot && volume > minLot)
         {
            halfVol = NormalizeDouble(halfVol, 2);
            if(trade.PositionClosePartial(ticket, halfVol))
            {
               LogDebug("Partial close: ticket=" + (string)ticket + " closed=" + DoubleToString(halfVol, 2) + " at R=" + DoubleToString(currentR, 2));
               if(slot >= 0) g_partial_closed[slot] = true;
            }
            else
               LogDebug("Partial close fail: ticket=" + (string)ticket + " err=" + trade.ResultRetcodeDescription());
         }
      }
      
      // --- 3. ATR Trailing Stop ---
      if(slotTrail > 0 && currentR >= slotBE)
      {
         // Only trail after break-even has been activated
         double atrVal = GetATR(sym, PERIOD_M15, TrailingATRPeriod);
         if(atrVal > 0)
         {
            double trailDist = atrVal * slotTrail;
            double trailSL = 0.0;
            
            if(posType == POSITION_TYPE_BUY)
               trailSL = currentPrice - trailDist;
            else
               trailSL = currentPrice + trailDist;
            
            int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
            trailSL = NormalizeDouble(trailSL, digits);
            
            // Only move SL in the favorable direction
            bool shouldTrail = false;
            if(posType == POSITION_TYPE_BUY && trailSL > currentSL && trailSL < currentPrice)
               shouldTrail = true;
            else if(posType == POSITION_TYPE_SELL && trailSL < currentSL && trailSL > currentPrice)
               shouldTrail = true;
            
            if(shouldTrail)
            {
               long stopLevel = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
               double point = SymbolInfoDouble(sym, SYMBOL_POINT);
               double minDist = stopLevel * point;
               
               bool distOk = false;
               if(posType == POSITION_TYPE_BUY)
                  distOk = (currentPrice - trailSL) >= minDist;
               else
                  distOk = (trailSL - currentPrice) >= minDist;
               
               if(distOk)
               {
                  if(trade.PositionModify(ticket, trailSL, currentTP))
                     LogDebug("Trail SL: ticket=" + (string)ticket + " newSL=" + DoubleToString(trailSL, digits) + " ATR=" + DoubleToString(atrVal, 6));
               }
            }
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Get ATR value for trailing stop calculation                       |
//+------------------------------------------------------------------+
double GetATR(string sym, ENUM_TIMEFRAMES tf, int period)
{
   int atrHandle = iATR(sym, tf, period);
   if(atrHandle == INVALID_HANDLE) return 0.0;
   
   double atrBuffer[];
   ArraySetAsSeries(atrBuffer, true);
   
   if(CopyBuffer(atrHandle, 0, 0, 1, atrBuffer) <= 0)
   {
      IndicatorRelease(atrHandle);
      return 0.0;
   }
   
   double val = atrBuffer[0];
   IndicatorRelease(atrHandle);
   return val;
}

void LogDebug(string msg)
{
   string logPath = RootFolder + "\\ea_debug.log";
   int handle = FileOpen(logPath, FILE_WRITE | FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ);
   if(handle != INVALID_HANDLE)
   {
      FileSeek(handle, 0, SEEK_END);
      FileWriteString(handle, TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS) + ": " + msg + "\r\n");
      FileClose(handle);
   }
   Print("FX_AI: ", msg);
}

bool ExtractJsonString(const string json, const string key, string &out)
{
   string pattern = "\"" + key + "\":";
   int start = StringFind(json, pattern);
   if(start < 0) return false;
   start += StringLen(pattern);

   while(start < StringLen(json) && (StringGetCharacter(json, start) == ' ' || StringGetCharacter(json, start) == '"' || StringGetCharacter(json, start) == ':'))
      start++;

   int end = start;
   while(end < StringLen(json))
   {
      ushort c = StringGetCharacter(json, end);
      if(c == '"' || c == ',' || c == '}' || c == ']')
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
   int handle = FileOpen(filePath, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ);
   if(handle == INVALID_HANDLE)
   {
      LogDebug("FileOpen fail: " + filePath + " err=" + (string)GetLastError());
      return false;
   }

   content = "";
   while(!FileIsEnding(handle))
      content += FileReadString(handle);

   FileClose(handle);
   return true;
}

bool WriteTextFile(const string filePath, const string content)
{
   int handle = FileOpen(filePath, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_WRITE);
   if(handle == INVALID_HANDLE)
      return false;

   FileWriteString(handle, content);
   FileClose(handle);
   return true;
}

bool GetFirstPendingSignalFile(string &fileName)
{
   string searchMask = PendingFolder() + "\\*.json";
   long findHandle = FileFindFirst(searchMask, fileName);
   if(findHandle == INVALID_HANDLE)
      return false;

   FileFindClose(findHandle);
   return true;
}

double PipValue(string sym)
{
   if(StringFind(sym, "JPY") >= 0)
      return 0.01;
   return 0.0001;
}

double CalculateLot(string sym, const double riskPercent, const double stopPips)
{
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskAmount = balance * riskPercent;

   double tickValue = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double tickSize = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);

   if(tickValue <= 0 || tickSize <= 0 || point <= 0 || stopPips <= 0)
      return 0.0;

   double valuePerPoint = tickValue / tickSize * point;
   double stopPoints = stopPips * PipValue(sym) / point;
   double rawLimit = (stopPoints > 0) ? (riskAmount / (stopPoints * valuePerPoint)) : 0.0;

   double minLot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double lotStep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);

   if(rawLimit < minLot)
   {
       LogDebug("Lot too low: " + (string)riskAmount + " min=" + (string)minLot);
       return 0.0;
   }

   double lot = MathFloor(rawLimit / lotStep) * lotStep;
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
   payload += "\"entry_price\":" + DoubleToString(entryPrice, 5) + ",";
   payload += "\"slippage\":" + DoubleToString(slippage, 6) + ",";
   payload += "\"spread_at_entry\":" + DoubleToString(spreadAtEntry, 6) + ",";
   payload += "\"profit_loss\":" + DoubleToString(profitLoss, 2) + ",";
   payload += "\"r_multiple\":" + DoubleToString(rMultiple, 4) + ",";
   payload += "\"close_time\":\"" + TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS) + "\"";
   payload += "}";

   string filePath = FeedbackFolder() + "\\execution_" + tradeId + ".json";
   WriteTextFile(filePath, payload);
}

void WriteExitFeedback(
   const ulong ticket,
   const double profitLoss,
   const datetime closeTime)
{
   string payload = "{";
   payload += "\"ticket\":" + StringFormat("%I64u", ticket) + ",";
   payload += "\"profit_loss\":" + DoubleToString(profitLoss, 2) + ",";
   payload += "\"status\":\"CLOSED\",";
   payload += "\"close_time\":\"" + TimeToString(closeTime, TIME_DATE | TIME_SECONDS) + "\"";
   payload += "}";

   string filePath = ExitsFolder() + "\\exit_" + StringFormat("%I64u", ticket) + ".json";
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

   WriteTextFile(FeedbackFolder() + "\\account_snapshot.json", payload);
}

void ProcessPendingSignal()
{
   string fileName;
   if(!GetFirstPendingSignalFile(fileName))
      return;

   LogDebug("Found Signal File: " + fileName);
   string filePath = PendingFolder() + "\\" + fileName;

   string content;
   if(!ReadTextFile(filePath, content))
   {
      LogDebug("Read fail: " + fileName);
      return;
   }

   string tradeId, symbol, direction;
   double riskPercent = 0.0;
   double stopPips = 0.0;
   double takeProfitPips = 0.0;
   double requestedLot = -1.0;
   string orderType = "MARKET";
   double limitPrice = 0.0;

   if(!ExtractJsonString(content, "trade_id", tradeId)) { LogDebug("JSON fail: tid"); return; }
   if(!ExtractJsonString(content, "symbol", symbol)) { LogDebug("JSON fail: sym"); return; }
   if(!ExtractJsonString(content, "direction", direction)) { LogDebug("JSON fail: dir"); return; }
   if(!ExtractJsonDouble(content, "risk_percent", riskPercent)) { LogDebug("JSON fail: risk"); return; }
   if(!ExtractJsonDouble(content, "stop_pips", stopPips)) { LogDebug("JSON fail: stop"); return; }
   if(!ExtractJsonDouble(content, "take_profit_pips", takeProfitPips)) { LogDebug("JSON fail: tp"); return; }
   ExtractJsonDouble(content, "lot", requestedLot);
   ExtractJsonString(content, "order_type", orderType);
   ExtractJsonDouble(content, "limit_price", limitPrice);
   double beR       = 1.0;
   double partialR  = 1.5;
   double trailMult = 2.0;
   string tpModeStr = "FIXED";
   ExtractJsonDouble(content, "be_trigger_r",      beR);
   ExtractJsonDouble(content, "partial_close_r",   partialR);
   ExtractJsonDouble(content, "trailing_atr_mult", trailMult);
   ExtractJsonString(content, "tp_mode",           tpModeStr);
   bool trailMode = (tpModeStr == "TRAIL");

   LogDebug("Parsed symbol: " + symbol + " dir: " + direction + " order_type: " + orderType);

   if(!SymbolSelect(symbol, true))
   {
      LogDebug("SymbolSelect fail: " + symbol);
      FileDelete(filePath);
      return;
   }

   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
   {
      LogDebug("Tick fetch fail: " + symbol);
      return;
   }

   double spread = tick.ask - tick.bid;
   double spreadPips = spread / PipValue(symbol);
   if(spreadPips > MaxSpreadPips)
   {
      LogDebug("Spread too wide: " + (string)spreadPips);
      WriteExecutionFeedback(tradeId, 0, "REJECTED_SPREAD", 0.0, 0.0, spread, 0.0, 0.0);
      FileDelete(filePath);
      return;
   }

   double recalculateLot = CalculateLot(symbol, riskPercent, stopPips);
   if(recalculateLot <= 0.0)
   {
      LogDebug("Lot calculation fail (too small)");
      WriteExecutionFeedback(tradeId, 0, "REJECTED_LOT", 0.0, 0.0, spread, 0.0, 0.0);
      FileDelete(filePath);
      return;
   }

   double finalLot = recalculateLot;
   if(requestedLot > 0.0 && requestedLot < recalculateLot)
      finalLot = requestedLot;

   // --- Determine execution method based on order_type ---
   bool placed = false;
   
   if(orderType == "LIMIT" && limitPrice > 0)
   {
      // LIMIT ORDER: place pending order at the specified limit_price
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      limitPrice = NormalizeDouble(limitPrice, digits);
      double sl = (direction == "BUY") ? (limitPrice - stopPips * PipValue(symbol)) : (limitPrice + stopPips * PipValue(symbol));
      double tp = 0.0;
      if(!trailMode)
         tp = (direction == "BUY") ? (limitPrice + takeProfitPips * PipValue(symbol)) : (limitPrice - takeProfitPips * PipValue(symbol));
      sl = NormalizeDouble(sl, digits);
      tp = NormalizeDouble(tp, digits);
      
      LogDebug("Sending LIMIT Order: " + symbol + " " + direction + " lot: " + (string)finalLot + " price: " + DoubleToString(limitPrice, digits));
      
      if(direction == "BUY")
         placed = trade.BuyLimit(finalLot, limitPrice, symbol, sl, tp, ORDER_TIME_GTC, 0, tradeId);
      else if(direction == "SELL")
         placed = trade.SellLimit(finalLot, limitPrice, symbol, sl, tp, ORDER_TIME_GTC, 0, tradeId);
   }
   else
   {
      // MARKET ORDER: execute immediately at current price
      double entry = (direction == "BUY") ? tick.ask : tick.bid;
      double sl = (direction == "BUY") ? (entry - stopPips * PipValue(symbol)) : (entry + stopPips * PipValue(symbol));
      double tp = 0.0;
      if(!trailMode)
         tp = (direction == "BUY") ? (entry + takeProfitPips * PipValue(symbol)) : (entry - takeProfitPips * PipValue(symbol));
      
      LogDebug("Sending MARKET Order: " + symbol + " " + direction + " lot: " + (string)finalLot);
      
      if(direction == "BUY")
         placed = trade.Buy(finalLot, symbol, entry, sl, tp, tradeId);
      else if(direction == "SELL")
         placed = trade.Sell(finalLot, symbol, entry, sl, tp, tradeId);
   }

   if(!placed)
   {
      LogDebug("Order Execution FAIL: " + trade.ResultRetcodeDescription());
      WriteExecutionFeedback(tradeId, 0, "REJECTED_ORDER_SEND", 0.0, 0.0, spread, 0.0, 0.0);
      FileDelete(filePath);
      return;
   }

   double fillPrice = trade.ResultPrice();
   double slippage = MathAbs(fillPrice - entry);
   ulong ticket = trade.ResultDeal();
   if(ticket == 0) ticket = trade.ResultOrder();
   
   LogDebug("EXECUTION SUCCESS: Ticket " + (string)ticket);
   ulong posTicket = trade.ResultDeal();
   if(posTicket == 0) posTicket = trade.ResultOrder();
   int slot = AllocSlot(posTicket);
   if(slot >= 0)
   {
      g_be_trigger_r[slot]     = beR;
      g_partial_close_r[slot]  = partialR;
      g_trailing_atr_mult[slot]= trailMult;
      g_tp_mode_trail[slot]    = trailMode;
      g_partial_closed[slot]   = false;
   }
   WriteExecutionFeedback(tradeId, ticket, "EXECUTED", fillPrice, slippage, spread, 0.0, 0.0);

   FileDelete(filePath);
}
