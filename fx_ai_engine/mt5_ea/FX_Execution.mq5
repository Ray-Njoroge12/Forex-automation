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
string g_trade_ids[MAX_POS];
double g_be_trigger_r[MAX_POS];
double g_partial_close_r[MAX_POS];
double g_trailing_atr_mult[MAX_POS];
bool   g_tp_mode_trail[MAX_POS];
double g_initial_stop_dist[MAX_POS];
double g_initial_volume[MAX_POS];
bool   g_partial_closed[MAX_POS];

void InitPositionArrays()
{
   for(int i = 0; i < MAX_POS; i++)
   {
      g_tickets[i]           = 0;
      g_trade_ids[i]         = "";
      g_be_trigger_r[i]      = 1.0;
      g_partial_close_r[i]   = 1.5;
      g_trailing_atr_mult[i] = 2.0;
      g_tp_mode_trail[i]     = false;
      g_initial_stop_dist[i] = 0.0;
      g_initial_volume[i]    = 0.0;
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
      g_trade_ids[s]         = "";
      g_be_trigger_r[s]      = 1.0;
      g_partial_close_r[s]   = 1.5;
      g_trailing_atr_mult[s] = 2.0;
      g_tp_mode_trail[s]     = false;
      g_initial_stop_dist[s] = 0.0;
      g_initial_volume[s]    = 0.0;
      g_partial_closed[s]    = false;
   }
}

// Helper to sanitize paths
string CleanPath(string p) { return p; }

string PendingFolder() { return RootFolder + "\\pending_signals"; }
string LockFolder() { return RootFolder + "\\active_locks"; }
string FeedbackFolder() { return RootFolder + "\\feedback"; }
string ExitsFolder() { return RootFolder + "\\exits"; }
string ManagementStateFilePath(const ulong ticket) { return FeedbackFolder() + "\\management_state_" + StringFormat("%I64u", ticket) + ".json"; }
string ExitFilePath(const ulong ticket) { return ExitsFolder() + "\\exit_" + StringFormat("%I64u", ticket) + ".json"; }

bool WriteManagementStateForSlot(const int slot);
void DeleteManagementStateFile(const ulong ticket);
void RestoreManagementState();
int CountManagedOpenPositions();
string BuildManagedTicketsJson(bool managed);
string BuildManagementStateError();
bool TryLoadManagementState(
   const ulong ticket,
   string &tradeId,
   double &initialStopDist,
   double &initialVolume,
   bool &partialClosed);
double DealNetProfit(const ulong dealTicket);
bool BuildClosedPositionSummary(
   const ulong ticket,
   string &tradeId,
   double &profitLoss,
   double &rMultiple,
   bool &hasRMultiple,
   datetime &closeTime);
bool FinalizeClosedPosition(const ulong ticket, const string fallbackTradeId = "");
void SweepClosedManagedPositions();

int OnInit()
{
   InitPositionArrays();
   RestoreManagementState();
   EventSetTimer(PollIntervalSeconds);
   LogDebug("OnInit: FX_Execution Started");
   WriteAccountSnapshot();
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
            if(pos_ticket == 0) return;

            if(PositionSelectByTicket(pos_ticket))
            {
               LogDebug("Ignoring non-final exit event for position=" + (string)pos_ticket);
               return;
            }

            string trade_id = GetTradeIdForPosition(pos_ticket);
            if(trade_id == "")
               trade_id = HistoryDealGetString(deal_ticket, DEAL_COMMENT);

            if(!FinalizeClosedPosition(pos_ticket, trade_id))
               LogDebug("Deferred final close finalization for position=" + (string)pos_ticket);
         }
      }
   }
}

void OnTimer()
{
   static datetime lastSnapshot = 0;
   
   ProcessPendingSignal();
   ManageOpenPositions();
   SweepClosedManagedPositions();
   
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
      bool   slotTrailMode = (slot >= 0) ? g_tp_mode_trail[slot]    : false;
      double slotInitialStopDist = (slot >= 0) ? g_initial_stop_dist[slot] : 0.0;

      // Calculate initial stop distance (R-unit) from the original entry risk.
      // Fall back to current SL only when no stored original stop distance exists yet.
      double initialStopDist = slotInitialStopDist;
      if(initialStopDist <= 0)
         initialStopDist = MathAbs(openPrice - currentSL);
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
               if(slot >= 0)
               {
                  g_partial_closed[slot] = true;
                  WriteManagementStateForSlot(slot);
               }
            }
            else
               LogDebug("Partial close fail: ticket=" + (string)ticket + " err=" + trade.ResultRetcodeDescription());
         }
      }
      
      // --- 3. ATR Trailing Stop ---
      if(slotTrailMode && slotTrail > 0 && currentR >= slotBE)
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
   const ulong positionTicket,
   const string status,
   const double entryPrice,
   const double slippage,
   const double spreadAtEntry,
   const double lotSize,
   const double profitLoss,
   const double rMultiple)
{
   string payload = "{";
   payload += "\"trade_id\":\"" + tradeId + "\",";
   payload += "\"ticket\":" + StringFormat("%I64u", ticket) + ",";
   payload += "\"position_ticket\":" + StringFormat("%I64u", positionTicket) + ",";
   payload += "\"status\":\"" + status + "\",";
   payload += "\"entry_price\":" + DoubleToString(entryPrice, 5) + ",";
   payload += "\"slippage\":" + DoubleToString(slippage, 6) + ",";
   payload += "\"spread_at_entry\":" + DoubleToString(spreadAtEntry, 6) + ",";
   payload += "\"lot_size\":" + DoubleToString(lotSize, 2) + ",";
   payload += "\"profit_loss\":" + DoubleToString(profitLoss, 2) + ",";
   payload += "\"r_multiple\":" + DoubleToString(rMultiple, 4) + ",";
   payload += "\"close_time\":\"" + TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS) + "\"";
   payload += "}";

   string filePath = FeedbackFolder() + "\\execution_" + tradeId + ".json";
   WriteTextFile(filePath, payload);
}

void WriteExitFeedback(
   const ulong ticket,
   const string tradeId,
   const double profitLoss,
   const datetime closeTime,
   const double rMultiple,
   const bool hasRMultiple)
{
   string status = "CLOSED_BREAKEVEN";
   if(profitLoss > 0)
      status = "CLOSED_WIN";
   else if(profitLoss < 0)
      status = "CLOSED_LOSS";

   string payload = "{";
   payload += "\"ticket\":" + StringFormat("%I64u", ticket) + ",";
   payload += "\"position_ticket\":" + StringFormat("%I64u", ticket) + ",";
   payload += "\"trade_id\":\"" + tradeId + "\",";
   payload += "\"profit_loss\":" + DoubleToString(profitLoss, 2) + ",";
   payload += "\"status\":\"" + status + "\",";
   payload += "\"is_final_exit\":true,";
   if(hasRMultiple)
      payload += "\"r_multiple\":" + DoubleToString(rMultiple, 4) + ",";
   payload += "\"close_time\":\"" + TimeToString(closeTime, TIME_DATE | TIME_SECONDS) + "\"";
   payload += "}";

   string filePath = ExitFilePath(ticket);
   WriteTextFile(filePath, payload);
}

void WriteAccountSnapshot()
{
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double marginFree = AccountInfoDouble(ACCOUNT_MARGIN_FREE);

   int positions = PositionsTotal();
   double floating = 0.0;
   int usdExposureCount = 0;
   string openSymbolsJson = "[";
   string seen = "|";
   int symbolCount = 0;
   for(int i = 0; i < positions; i++)
   {
      if(PositionGetTicket(i) <= 0)
         continue;
      floating += PositionGetDouble(POSITION_PROFIT);
      string sym = PositionGetString(POSITION_SYMBOL);
      string token = "|" + sym + "|";
      if(StringFind(seen, token) < 0)
      {
         if(symbolCount > 0)
            openSymbolsJson += ",";
         openSymbolsJson += "\"" + sym + "\"";
         seen += sym + "|";
         symbolCount++;
         if(StringFind(sym, "USD") >= 0)
            usdExposureCount++;
      }
   }
   openSymbolsJson += "]";
   int managedPositions = CountManagedOpenPositions();
   bool managementStateRestored = (positions <= 0) || (managedPositions == positions);
   string managedTicketsJson = BuildManagedTicketsJson(true);
   string unmanagedTicketsJson = BuildManagedTicketsJson(false);
   string managementStateError = BuildManagementStateError();

   string payload = "{";
   payload += "\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS) + "\",";
   payload += "\"balance\":" + DoubleToString(balance, 2) + ",";
   payload += "\"equity\":" + DoubleToString(equity, 2) + ",";
   payload += "\"margin_free\":" + DoubleToString(marginFree, 2) + ",";
   payload += "\"open_positions_count\":" + IntegerToString(positions) + ",";
   payload += "\"floating_pnl\":" + DoubleToString(floating, 2) + ",";
   payload += "\"open_symbols\":" + openSymbolsJson + ",";
   payload += "\"open_usd_exposure_count\":" + IntegerToString(usdExposureCount) + ",";
   payload += "\"management_state_restored\":" + (managementStateRestored ? "true" : "false") + ",";
   payload += "\"managed_positions_count\":" + IntegerToString(managedPositions) + ",";
   payload += "\"managed_position_tickets\":" + managedTicketsJson + ",";
   payload += "\"unmanaged_position_tickets\":" + unmanagedTicketsJson;
   if(!managementStateRestored)
      payload += ",\"management_state_error\":\"" + managementStateError + "\"";
   payload += "}";

   WriteTextFile(FeedbackFolder() + "\\account_snapshot.json", payload);
}

string GetTradeIdForPosition(ulong ticket)
{
   int s = FindSlot(ticket);
   if(s >= 0)
      return g_trade_ids[s];
   return "";
}

bool TryLoadManagementState(
   const ulong ticket,
   string &tradeId,
   double &initialStopDist,
   double &initialVolume,
   bool &partialClosed)
{
   string content;
   if(!ReadTextFile(ManagementStateFilePath(ticket), content))
      return false;

   double ticketValue = 0.0;
   double partialClosedValue = 0.0;
   double initialVolumeValue = 0.0;
   bool ok = true;
   ok = ok && ExtractJsonDouble(content, "position_ticket", ticketValue);
   ok = ok && ExtractJsonString(content, "trade_id", tradeId);
   ok = ok && ExtractJsonDouble(content, "initial_stop_dist", initialStopDist);
   ok = ok && ExtractJsonDouble(content, "partial_closed", partialClosedValue);
   ExtractJsonDouble(content, "initial_volume", initialVolumeValue);
   if(!ok || (ulong)ticketValue != ticket)
      return false;

   initialVolume = initialVolumeValue;
   partialClosed = (partialClosedValue > 0.5);
   return true;
}

double DealNetProfit(const ulong dealTicket)
{
   double profit = HistoryDealGetDouble(dealTicket, DEAL_PROFIT);
   double fee = HistoryDealGetDouble(dealTicket, DEAL_FEE);
   double swap = HistoryDealGetDouble(dealTicket, DEAL_SWAP);
   double commission = HistoryDealGetDouble(dealTicket, DEAL_COMMISSION);
   return profit + fee + swap + commission;
}

bool BuildClosedPositionSummary(
   const ulong ticket,
   string &tradeId,
   double &profitLoss,
   double &rMultiple,
   bool &hasRMultiple,
   datetime &closeTime)
{
   profitLoss = 0.0;
   rMultiple = 0.0;
   hasRMultiple = false;
   closeTime = 0;
   if(ticket == 0)
      return false;

   double initialStopDist = 0.0;
   double initialVolume = 0.0;
   int slot = FindSlot(ticket);
   if(slot >= 0)
   {
      if(tradeId == "")
         tradeId = g_trade_ids[slot];
      initialStopDist = g_initial_stop_dist[slot];
      initialVolume = g_initial_volume[slot];
   }

   if(tradeId == "" || initialStopDist <= 0 || initialVolume <= 0)
   {
      string persistedTradeId = "";
      double persistedStopDist = 0.0;
      double persistedInitialVolume = 0.0;
      bool persistedPartialClosed = false;
      if(TryLoadManagementState(ticket, persistedTradeId, persistedStopDist, persistedInitialVolume, persistedPartialClosed))
      {
         if(tradeId == "")
            tradeId = persistedTradeId;
         if(initialStopDist <= 0)
            initialStopDist = persistedStopDist;
         if(initialVolume <= 0)
            initialVolume = persistedInitialVolume;
      }
   }

   datetime historyStart = TimeCurrent() - 86400 * 30;
   if(!HistorySelect(historyStart, TimeCurrent() + 60))
      return false;

   ulong openDeal = 0;
   datetime openTime = 0;
   string commentTradeId = tradeId;
   for(int i = 0; i < HistoryDealsTotal(); i++)
   {
      ulong dealTicket = HistoryDealGetTicket(i);
      if(dealTicket == 0) continue;
      if((ulong)HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID) != ticket)
         continue;

      string dealComment = HistoryDealGetString(dealTicket, DEAL_COMMENT);
      StringTrimLeft(dealComment);
      StringTrimRight(dealComment);
      if(commentTradeId == "" && dealComment != "")
         commentTradeId = dealComment;

      long dealEntry = HistoryDealGetInteger(dealTicket, DEAL_ENTRY);
      datetime dealTime = (datetime)HistoryDealGetInteger(dealTicket, DEAL_TIME);
      if(dealEntry == DEAL_ENTRY_IN)
      {
         if(openDeal == 0 || dealTime < openTime)
         {
            openDeal = dealTicket;
            openTime = dealTime;
         }
         continue;
      }
      if(dealEntry == DEAL_ENTRY_OUT || dealEntry == DEAL_ENTRY_INOUT || dealEntry == DEAL_ENTRY_OUT_BY)
      {
         profitLoss += DealNetProfit(dealTicket);
         if(dealTime >= closeTime)
            closeTime = dealTime;
      }
   }

   if(closeTime <= 0)
      return false;
   if(tradeId == "")
      tradeId = commentTradeId;
   if(openDeal != 0 && initialVolume <= 0)
      initialVolume = HistoryDealGetDouble(openDeal, DEAL_VOLUME);

   if(openDeal != 0 && initialStopDist > 0 && initialVolume > 0)
   {
      double entryPrice = HistoryDealGetDouble(openDeal, DEAL_PRICE);
      long openType = HistoryDealGetInteger(openDeal, DEAL_TYPE);
      bool isBuy = (openType == DEAL_TYPE_BUY);
      if(isBuy || openType == DEAL_TYPE_SELL)
      {
         for(int i = 0; i < HistoryDealsTotal(); i++)
         {
            ulong dealTicket = HistoryDealGetTicket(i);
            if(dealTicket == 0) continue;
            if((ulong)HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID) != ticket)
               continue;
            long dealEntry = HistoryDealGetInteger(dealTicket, DEAL_ENTRY);
            if(!(dealEntry == DEAL_ENTRY_OUT || dealEntry == DEAL_ENTRY_INOUT || dealEntry == DEAL_ENTRY_OUT_BY))
               continue;

            double closePrice = HistoryDealGetDouble(dealTicket, DEAL_PRICE);
            double closedVolume = HistoryDealGetDouble(dealTicket, DEAL_VOLUME);
            double legR = isBuy ? ((closePrice - entryPrice) / initialStopDist) : ((entryPrice - closePrice) / initialStopDist);
            rMultiple += legR * (closedVolume / initialVolume);
         }
         hasRMultiple = true;
      }
   }
   return true;
}

bool FinalizeClosedPosition(const ulong ticket, const string fallbackTradeId)
{
   if(ticket == 0)
      return false;
   if(PositionSelectByTicket(ticket))
      return false;

   string tradeId = fallbackTradeId;
   double profitLoss = 0.0;
   double rMultiple = 0.0;
   bool hasRMultiple = false;
   datetime closeTime = 0;
   if(!BuildClosedPositionSummary(ticket, tradeId, profitLoss, rMultiple, hasRMultiple, closeTime))
      return false;

   if(!FileIsExist(ExitFilePath(ticket)))
      WriteExitFeedback(ticket, tradeId, profitLoss, closeTime, rMultiple, hasRMultiple);
   DeleteManagementStateFile(ticket);
   FreeSlot(ticket);
   LogDebug("Finalized closed position ticket=" + (string)ticket + " profit_loss=" + DoubleToString(profitLoss, 2));
   return true;
}

void SweepClosedManagedPositions()
{
   for(int i = 0; i < MAX_POS; i++)
   {
      ulong ticket = g_tickets[i];
      if(ticket == 0) continue;
      if(!PositionSelectByTicket(ticket))
         FinalizeClosedPosition(ticket, g_trade_ids[i]);
   }

   string fileName;
   long findHandle = FileFindFirst(FeedbackFolder() + "\\management_state_*.json", fileName);
   if(findHandle == INVALID_HANDLE)
      return;

   do
   {
      string content;
      if(!ReadTextFile(FeedbackFolder() + "\\" + fileName, content))
         continue;
      double ticketValue = 0.0;
      if(!ExtractJsonDouble(content, "position_ticket", ticketValue))
         continue;
      ulong ticket = (ulong)ticketValue;
      if(ticket == 0 || PositionSelectByTicket(ticket))
         continue;
      FinalizeClosedPosition(ticket);
   }
   while(FileFindNext(findHandle, fileName));
   FileFindClose(findHandle);
}

ulong FindPositionTicketByTradeId(const string symbol, const string tradeId)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong positionTicket = PositionGetTicket(i);
      if(positionTicket == 0)
         continue;

      string positionSymbol = PositionGetString(POSITION_SYMBOL);
      string positionComment = PositionGetString(POSITION_COMMENT);
      if(positionSymbol == symbol && positionComment == tradeId)
         return positionTicket;
   }
   return 0;
}

bool WriteManagementStateForSlot(const int slot)
{
   if(slot < 0 || slot >= MAX_POS) return false;
   ulong ticket = g_tickets[slot];
   if(ticket == 0) return false;

   string payload = "{";
   payload += "\"position_ticket\":" + StringFormat("%I64u", ticket) + ",";
   payload += "\"trade_id\":\"" + g_trade_ids[slot] + "\",";
   payload += "\"be_trigger_r\":" + DoubleToString(g_be_trigger_r[slot], 4) + ",";
   payload += "\"partial_close_r\":" + DoubleToString(g_partial_close_r[slot], 4) + ",";
   payload += "\"trailing_atr_mult\":" + DoubleToString(g_trailing_atr_mult[slot], 4) + ",";
   payload += "\"tp_mode_trail\":" + (g_tp_mode_trail[slot] ? "1" : "0") + ",";
   payload += "\"initial_stop_dist\":" + DoubleToString(g_initial_stop_dist[slot], 8) + ",";
   payload += "\"initial_volume\":" + DoubleToString(g_initial_volume[slot], 2) + ",";
   payload += "\"partial_closed\":" + (g_partial_closed[slot] ? "1" : "0") + ",";
   payload += "\"saved_at\":\"" + TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS) + "\"";
   payload += "}";
   return WriteTextFile(ManagementStateFilePath(ticket), payload);
}

void DeleteManagementStateFile(const ulong ticket)
{
   if(ticket == 0) return;
   FileDelete(ManagementStateFilePath(ticket));
}

void RestoreManagementState()
{
   string fileName;
   long findHandle = FileFindFirst(FeedbackFolder() + "\\management_state_*.json", fileName);
   if(findHandle != INVALID_HANDLE)
   {
      do
      {
         string filePath = FeedbackFolder() + "\\" + fileName;
         string content;
         if(!ReadTextFile(filePath, content))
         {
            LogDebug("Management restore read fail: " + fileName);
            continue;
         }

         double ticketValue = 0.0;
         double beR = 1.0;
         double partialR = 1.5;
         double trailMult = 2.0;
         double trailMode = 0.0;
         double initialStopDist = 0.0;
         double initialVolume = 0.0;
         double partialClosed = 0.0;
         string tradeId = "";
         bool ok = true;
         ok = ok && ExtractJsonDouble(content, "position_ticket", ticketValue);
         ok = ok && ExtractJsonString(content, "trade_id", tradeId);
         ok = ok && ExtractJsonDouble(content, "be_trigger_r", beR);
         ok = ok && ExtractJsonDouble(content, "partial_close_r", partialR);
         ok = ok && ExtractJsonDouble(content, "trailing_atr_mult", trailMult);
         ok = ok && ExtractJsonDouble(content, "tp_mode_trail", trailMode);
         ok = ok && ExtractJsonDouble(content, "initial_stop_dist", initialStopDist);
         ok = ok && ExtractJsonDouble(content, "partial_closed", partialClosed);
         ExtractJsonDouble(content, "initial_volume", initialVolume);
         ulong ticket = (ulong)ticketValue;

         if(!ok || ticket == 0)
         {
            LogDebug("Management restore parse fail: " + fileName);
            continue;
         }
         if(!PositionSelectByTicket(ticket))
         {
            LogDebug("Removing stale management state: " + fileName);
            FileDelete(filePath);
            continue;
         }

         int slot = AllocSlot(ticket);
         if(slot < 0)
         {
            LogDebug("Management restore slot allocation fail: " + fileName);
            continue;
         }

         g_trade_ids[slot] = tradeId;
         g_be_trigger_r[slot] = beR;
         g_partial_close_r[slot] = partialR;
         g_trailing_atr_mult[slot] = trailMult;
         g_tp_mode_trail[slot] = (trailMode > 0.5);
         g_initial_stop_dist[slot] = initialStopDist;
         g_initial_volume[slot] = (initialVolume > 0) ? initialVolume : PositionGetDouble(POSITION_VOLUME);
         g_partial_closed[slot] = (partialClosed > 0.5);
         LogDebug("Management restored: ticket=" + (string)ticket + " trade_id=" + tradeId);
      }
      while(FileFindNext(findHandle, fileName));
      FileFindClose(findHandle);
   }

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(FindSlot(ticket) < 0)
         LogDebug("Management restore missing for open position=" + (string)ticket);
   }
}

int CountManagedOpenPositions()
{
   int managed = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      int slot = FindSlot(ticket);
      if(slot >= 0 && g_initial_stop_dist[slot] > 0)
         managed++;
   }
   return managed;
}

string BuildManagedTicketsJson(bool managed)
{
   string out = "[";
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      int slot = FindSlot(ticket);
      bool hasState = (slot >= 0 && g_initial_stop_dist[slot] > 0);
      if(hasState != managed)
         continue;
      if(count > 0)
         out += ",";
      out += StringFormat("%I64u", ticket);
      count++;
   }
   out += "]";
   return out;
}

string BuildManagementStateError()
{
   int total = PositionsTotal();
   if(total <= 0)
      return "";
   int managed = CountManagedOpenPositions();
   if(managed == total)
      return "";
   return "managed_positions=" + IntegerToString(managed) + "/" + IntegerToString(total) + " missing_tickets=" + BuildManagedTicketsJson(false);
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
   bool trailMode = false;
   bool hardTpMode = true;
   if(tpModeStr == "TRAIL")
   {
      trailMode = true;
      hardTpMode = false;
   }
   else if(tpModeStr == "HYBRID")
   {
      trailMode = true;
      hardTpMode = true;
   }
   else if(tpModeStr != "FIXED")
   {
      LogDebug("Unknown tp_mode, defaulting to FIXED: " + tpModeStr);
      tpModeStr = "FIXED";
   }

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
      WriteExecutionFeedback(tradeId, 0, 0, "REJECTED_SPREAD", 0.0, 0.0, spread, 0.0, 0.0, 0.0);
      FileDelete(filePath);
      return;
   }

   double recalculateLot = CalculateLot(symbol, riskPercent, stopPips);
   if(recalculateLot <= 0.0)
   {
      LogDebug("Lot calculation fail (too small)");
      WriteExecutionFeedback(tradeId, 0, 0, "REJECTED_LOT", 0.0, 0.0, spread, 0.0, 0.0, 0.0);
      FileDelete(filePath);
      return;
   }

   double finalLot = recalculateLot;
   if(requestedLot > 0.0 && requestedLot < recalculateLot)
      finalLot = requestedLot;

   // --- Determine execution method based on order_type ---
   bool placed = false;
   double entry = 0.0; // declared here so it is in scope for slippage calculation below

   if(orderType == "LIMIT" && limitPrice > 0)
   {
      // LIMIT ORDER: place pending order at the specified limit_price
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      limitPrice = NormalizeDouble(limitPrice, digits);
      entry = limitPrice;
      double sl = (direction == "BUY") ? (limitPrice - stopPips * PipValue(symbol)) : (limitPrice + stopPips * PipValue(symbol));
      double tp = 0.0;
      if(hardTpMode)
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
      entry = (direction == "BUY") ? tick.ask : tick.bid;
      double sl = (direction == "BUY") ? (entry - stopPips * PipValue(symbol)) : (entry + stopPips * PipValue(symbol));
      double tp = 0.0;
      if(hardTpMode)
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
      WriteExecutionFeedback(tradeId, 0, 0, "REJECTED_ORDER_SEND", 0.0, 0.0, spread, finalLot, 0.0, 0.0);
      FileDelete(filePath);
      return;
   }

   double fillPrice = trade.ResultPrice();
   double resolvedEntryPrice = (fillPrice > 0.0) ? fillPrice : entry;
   double slippage = 0.0;
   if(fillPrice > 0.0 && entry > 0.0)
      slippage = MathAbs(fillPrice - entry);
   ulong ticket = trade.ResultDeal();
   if(ticket == 0) ticket = trade.ResultOrder();
   
   LogDebug("EXECUTION SUCCESS: Ticket " + (string)ticket);
   ulong posTicket = FindPositionTicketByTradeId(symbol, tradeId);
   if(posTicket == 0 && PositionSelectByTicket(trade.ResultOrder())) posTicket = trade.ResultOrder();
   if(posTicket == 0 && PositionSelectByTicket(trade.ResultDeal())) posTicket = trade.ResultDeal();
   if(posTicket == 0) posTicket = trade.ResultOrder();
   double initialStopDist = stopPips * PipValue(symbol);
   if(posTicket != 0 && PositionSelectByTicket(posTicket))
   {
      double posOpenPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      double posStopPrice = PositionGetDouble(POSITION_SL);
      double actualStopDist = MathAbs(posOpenPrice - posStopPrice);
      if(actualStopDist > 0)
         initialStopDist = actualStopDist;
   }
   int slot = AllocSlot(posTicket);
   if(slot >= 0)
   {
      g_trade_ids[slot]        = tradeId;
      g_be_trigger_r[slot]     = beR;
      g_partial_close_r[slot]  = partialR;
      g_trailing_atr_mult[slot]= trailMult;
      g_tp_mode_trail[slot]    = trailMode;
      g_initial_stop_dist[slot]= initialStopDist;
      g_initial_volume[slot]   = finalLot;
      g_partial_closed[slot]   = false;
      WriteManagementStateForSlot(slot);
   }
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   LogDebug(
      "Trade management armed: ticket=" + (string)posTicket +
      " tp_mode=" + tpModeStr +
      " trail=" + (trailMode ? "1" : "0") +
      " hard_tp=" + (hardTpMode ? "1" : "0") +
      " initial_stop_dist=" + DoubleToString(initialStopDist, digits)
   );
   WriteExecutionFeedback(tradeId, ticket, posTicket, "EXECUTED", resolvedEntryPrice, slippage, spread, finalLot, 0.0, 0.0);

   FileDelete(filePath);
}
