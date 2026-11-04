"""Source guards for the cent-account EA build (C1 symbol suffix, C4 account guards, C5 live guard).

The terminal cannot be exercised from the test suite, so these tests assert on the
structure of mt5_ea/FX_Execution.mq5 in the style of test_ea_policy_pin.py. They fail
if a guard, input or canonical-name rule is removed. Behaviour on a real terminal is
verified by the owner after deployment (docs/decisions/0011-cent-ea-guards.md).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

EA_SOURCE = Path(__file__).parents[1] / "mt5_ea" / "FX_Execution.mq5"

MT5_CALLS = (
    "SymbolSelect",
    "SymbolInfoTick",
    "SymbolInfoInteger",
    "SymbolInfoDouble",
    "iTime",
    "CopyRates",
    "PipValue",
    "CalculateLot",
    "PreflightMarketOrder",
    "FindPositionTicketByTradeId",
)


@pytest.fixture(scope="module")
def source() -> str:
    return EA_SOURCE.read_text(encoding="utf-8")


def _mask(source: str) -> str:
    """Same-length copy with string literals, char literals and comments blanked."""
    out = list(source)
    i, n = 0, len(source)

    def blank(start: int, end: int) -> None:
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            end = source.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif c == "/" and nxt == "*":
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(i, end)
            i = end
        elif c == '"':
            j = i + 1
            while j < n and source[j] != '"':
                j += 2 if source[j] == "\\" else 1
            blank(i + 1, j)
            i = j + 1
        elif c == "'":
            j = i + 1
            while j < n and source[j] != "'":
                j += 2 if source[j] == "\\" else 1
            blank(i + 1, j)
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _function_body(source: str, name: str) -> str:
    """Body of the function definition `name` (not a prototype or a call), without the outer braces.

    A plain brace count fails because the JSON payload strings contain braces, so
    the matching runs on a masked copy and the slice is taken from the original.
    """
    masked = _mask(source)
    for match in re.finditer(r"\b" + re.escape(name) + r"\s*\(", masked):
        depth, i = 1, match.end()
        while i < len(masked) and depth:
            depth += {"(": 1, ")": -1}.get(masked[i], 0)
            i += 1
        j = i
        while j < len(masked) and masked[j].isspace():
            j += 1
        if j >= len(masked) or masked[j] != "{":
            continue
        depth, k = 1, j + 1
        while k < len(masked) and depth:
            depth += {"{": 1, "}": -1}.get(masked[k], 0)
            k += 1
        return source[j + 1 : k - 1]
    raise AssertionError(f"function definition not found: {name}")


def _before(text: str, first: str, second: str) -> bool:
    a, b = text.find(first), text.find(second)
    return a >= 0 and b >= 0 and a < b


def test_function_body_helper_ignores_braces_in_strings(source):
    body = _function_body(source, "WriteExecutionFeedback")
    assert 'payload += "}";' in body
    assert "FeedbackFolder()" in body


# ---------------------------------------------------------------- build identity


def test_ea_version_bumped(source):
    assert '#define EA_VERSION "2026.11.04-2"' in source


def test_envelope_checks_unchanged(source):
    body = _function_body(source, "ProcessPendingSignal")
    labels = re.findall(r'LogDebug\("Envelope fail: ([^"]*)', body)
    assert labels == [
        "schema_version",
        "policy_hash",
        "reservation_id",
        "expires_at",
        "max_spread_pips",
        "max_positions",
        "ExpectedPolicyHash is not configured",
        "policy_hash mismatch",
        "expired or unparseable expires_at=",
    ]
    assert 'input string ExpectedPolicyHash = "f00f1f9c3e9120971378f3c1eb95d28f09d1d5e2ee97035a7f4c00a998535dc8";' in source
    assert "input bool RequirePolicyHashPin = true;" in source


# -------------------------------------------------------------------- C1 symbol suffix


def test_symbol_suffix_input_defaults_to_empty(source):
    assert 'input string SymbolSuffix = "";' in source


def test_canonical_symbol_list_is_unchanged(source):
    assert 'string          g_symbols[] = {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"};' in source


def test_broker_symbol_is_canonical_plus_suffix_only(source):
    body = _function_body(source, "BrokerSymbol")
    assert "return canonical + SymbolSuffix;" in body
    for fn in ("StringToUpper", "StringToLower", "StringTrim", "StringReplace"):
        assert fn not in body


def test_canonical_symbol_is_exact_reverse_mapping(source):
    body = _function_body(source, "CanonicalSymbol")
    assert "name == g_symbols[i] + SymbolSuffix" in body
    assert "return g_symbols[i];" in body
    assert body.rstrip().endswith("return name;")
    assert "StringReplace" not in body and "StringSubstr" not in body


def test_export_index_maps_broker_name_to_canonical_index(source):
    body = _function_body(source, "ExportIndexForBrokerSymbol")
    assert "broker == g_symbols[i] + SymbolSuffix" in body
    assert "return -1;" in body


def test_resolve_symbols_selects_broker_names_and_tracks_failures(source):
    body = _function_body(source, "ResolveSymbols")
    assert "g_broker_symbols[i] = BrokerSymbol(g_symbols[i]);" in body
    assert "g_symbol_resolved[i] = SymbolSelect(g_broker_symbols[i], true);" in body
    assert "g_symbols_ok = allOk;" in body
    assert "failed != g_symbol_error_log" in body  # logs only when the failure set changes


def test_resolve_symbols_runs_at_init_and_with_the_spec_refresh(source):
    assert "ResolveSymbols();" in _function_body(source, "OnInit")
    export = _function_body(source, "ExportMarketData")
    specs_block = export[export.index("now - lastSpecs >= 60") :]
    assert _before(specs_block, "ResolveSymbols();", "WriteSymbolSpecs();")
    on_init = _function_body(source, "OnInit")
    assert _before(on_init, "ResolveSymbols();", "WriteAccountSnapshot();")
    assert _before(on_init, "ResolveSymbols();", "ExportMarketData(true);")


def test_export_bars_reads_broker_symbol_and_writes_canonical_names(source):
    body = _function_body(source, "ExportBars")
    assert "string canonical = g_symbols[si];" in body
    assert "string sym = g_broker_symbols[si];" in body
    for call in ("iTime(sym,", "CopyRates(sym,", "SymbolInfoInteger(sym,"):
        assert call in body
    assert '"\\\\bars_" + canonical + "_"' in body
    assert '\\"symbol\\":\\"" + canonical + ' in body
    assert '"\\\\bars_" + sym' not in body
    assert '\\"symbol\\":\\"" + sym' not in body


def test_status_keys_bars_by_canonical_and_reads_them_by_broker(source):
    body = _function_body(source, "WriteMarketStatus")
    assert 'payload += "\\"" + g_symbols[s] + "\\":{";' in body
    assert "iTime(g_broker_symbols[s]," in body
    assert "iTime(g_symbols[s]" not in body


def test_status_publishes_symbol_suffix_ok_flag_and_errors(source):
    body = _function_body(source, "WriteMarketStatus")
    assert '\\"symbol_suffix\\":\\"" + JsonEscape(SymbolSuffix)' in body
    assert '\\"symbols_ok\\":" + (g_symbols_ok ? "true" : "false")' in body
    assert '\\"symbol_errors\\":" + BuildSymbolErrorsJson()' in body
    errors = _function_body(source, "BuildSymbolErrorsJson")
    assert "!g_symbol_resolved[i]" in errors or "if(g_symbol_resolved[i]) continue;" in errors
    assert 'out += "\\"" + g_symbols[i] + "\\"";' in errors  # canonical names


def test_symbol_specs_keyed_by_canonical_read_by_broker_with_broker_symbol_field(source):
    body = _function_body(source, "WriteSymbolSpecs")
    assert "string canonical = g_symbols[s];" in body
    assert "string sym = g_broker_symbols[s];" in body
    assert 'payload += "\\"" + canonical + "\\":{";' in body
    assert "SymbolSelect(sym, true)" in body
    assert 'payload += "\\"broker_symbol\\":\\"" + JsonEscape(sym) + "\\"";' in body
    assert "SymbolInfoDouble(canonical" not in body and "SymbolInfoInteger(canonical" not in body


def test_quotes_read_broker_ticks_and_write_canonical_symbols(source):
    body = _function_body(source, "WriteMarketQuoteSnapshot")
    assert "string brokerSym = BrokerSymbol(symbols[i]);" in body
    assert "SymbolInfoTick(brokerSym, tick)" in body
    assert "SymbolInfoInteger(brokerSym, SYMBOL_DIGITS)" in body
    assert '{\\"symbol\\":\\"" + symbols[i] + "\\"' in body
    assert "SymbolInfoTick(symbols[i]" not in body


def test_deal_history_maps_broker_symbols_back_to_canonical(source):
    body = _function_body(source, "WriteDealHistory")
    assert "string brokerSym = HistoryDealGetString(d, DEAL_SYMBOL);" in body
    assert "int symIdx = ExportIndexForBrokerSymbol(brokerSym);" in body
    assert "if(symIdx < 0) continue;" in body
    assert "SymbolInfoInteger(brokerSym, SYMBOL_DIGITS)" in body
    assert '\\"symbol\\":\\"" + g_symbols[symIdx] + "\\""' in body
    assert '"\\"symbol\\":\\"" + brokerSym' not in body


def test_account_snapshot_uses_canonical_names_for_open_symbols(source):
    body = _function_body(source, "WriteAccountSnapshot")
    assert "string sym = CanonicalSymbol(PositionGetString(POSITION_SYMBOL));" in body
    assert 'openSymbolsJson += "\\"" + sym + "\\"";' in body
    assert 'StringFind(sym, "USD")' in body


def test_pip_value_classifies_jpy_on_the_canonical_name(source):
    body = _function_body(source, "PipValue")
    assert 'StringFind(CanonicalSymbol(sym), "JPY")' in body


def test_signal_symbol_is_canonical_and_translated_once_before_any_mt5_call(source):
    body = _function_body(source, "ProcessPendingSignal")
    assert "string tradeId, canonicalSymbol, direction;" in body
    assert 'ExtractJsonString(content, "symbol", canonicalSymbol)' in body
    decl = "string symbol = BrokerSymbol(canonicalSymbol);"
    assert body.count(decl) == 1
    assert body.index(decl) < body.index("SymbolSelect(symbol")
    for call in MT5_CALLS:
        assert not re.search(call + r"\(\s*canonicalSymbol", body), call
    assert not re.search(r"trade\.(Buy|Sell|BuyLimit|SellLimit)\(\s*finalLot,\s*(limitPrice,\s*)?canonicalSymbol", body)
    # the broker name is declared only after the envelope checks, so earlier code cannot use it
    assert body.index(decl) > body.index("REJECTED_EXPIRED")


def test_unresolved_symbol_rejects_with_feedback_before_symbol_select(source):
    body = _function_body(source, "ProcessPendingSignal")
    gate = body[body.index("if(!g_symbols_ok)") :]
    gate = gate[: gate.index("return;")]
    assert 'WriteExecutionFeedback(tradeId, 0, 0, "REJECTED_SYMBOL", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0);' in gate
    assert "FileDelete(filePath);" in gate
    assert _before(body, "ResolveSymbols();", "if(!g_symbols_ok)")
    assert body.index("if(!g_symbols_ok)") < body.index("SymbolSelect(symbol")


def test_signal_with_unknown_symbol_keeps_the_existing_silent_drop(source):
    body = _function_body(source, "ProcessPendingSignal")
    fail = body[body.index("if(!SymbolSelect(symbol, true))") :]
    fail = fail[: fail.index("return;")]
    assert 'LogDebug("SymbolSelect fail: " + symbol);' in fail
    assert "FileDelete(filePath);" in fail
    assert "WriteExecutionFeedback" not in fail


def test_empty_suffix_leaves_exported_names_unchanged(source):
    """With SymbolSuffix == "", BrokerSymbol(x) == x and CanonicalSymbol(x) == x.

    Every writer builds its names from canonical values (g_symbols, the local canonical
    name, or the quote list) and every MT5 call uses the broker value. With an empty
    suffix both values are the same string, so each file name, `symbol` field,
    `open_symbols` entry and symbols.json key equals what the previous build wrote.
    The assertions below pin that structure: no writer builds an exported name from a
    broker variable.
    """
    assert _function_body(source, "BrokerSymbol").strip() == "return canonical + SymbolSuffix;"
    bars = _function_body(source, "ExportBars")
    assert "bars_\" + canonical" in bars
    status = _function_body(source, "WriteMarketStatus")
    assert "g_broker_symbols" not in status.replace("iTime(g_broker_symbols[s],", "")
    specs = _function_body(source, "WriteSymbolSpecs")
    assert specs.count('"\\"" + canonical + "\\":{"') == 1
    assert "g_broker_symbols[s]" in specs
    deals = _function_body(source, "WriteDealHistory")
    assert "g_symbols[symIdx]" in deals
    quotes = _function_body(source, "WriteMarketQuoteSnapshot")
    assert 'symbols[i] + "\\"' in quotes
    snapshot = _function_body(source, "WriteAccountSnapshot")
    assert "CanonicalSymbol(PositionGetString(POSITION_SYMBOL))" in snapshot
    # execution and exit feedback carry no symbol field at all
    assert "symbol" not in _function_body(source, "WriteExecutionFeedback").lower()
    assert "symbol" not in _function_body(source, "WriteExitFeedback").lower()
    # the only writer that touches a broker name directly feeds MT5 calls, not file content
    for name in ("ExportBars", "WriteMarketStatus", "WriteSymbolSpecs", "WriteDealHistory", "WriteMarketQuoteSnapshot"):
        body = _function_body(source, name)
        assert not re.search(r'"\\\\bars_"\s*\+\s*(sym|brokerSym)\b', body)
        assert not re.search(r'\\"symbol\\":\\""\s*\+\s*(sym|brokerSym)\b', body)


# ---------------------------------------------------------------- C4 account guards


def test_margin_mode_names_cover_the_three_enum_values(source):
    body = _function_body(source, "AccountMarginModeName")
    assert "ACCOUNT_MARGIN_MODE" in body
    assert 'case ACCOUNT_MARGIN_MODE_RETAIL_HEDGING: return "hedging";' in body
    assert 'case ACCOUNT_MARGIN_MODE_RETAIL_NETTING: return "netting";' in body
    assert 'case ACCOUNT_MARGIN_MODE_EXCHANGE:       return "exchange";' in body
    assert 'default:                                 return "unknown";' in body


def test_only_hedging_allows_trading(source):
    body = _function_body(source, "AccountModeAllowsTrading")
    assert "== ACCOUNT_MARGIN_MODE_RETAIL_HEDGING" in body
    assert "NETTING" not in body and "EXCHANGE" not in body


def test_status_publishes_margin_mode_currency_and_spread_ceiling(source):
    body = _function_body(source, "WriteMarketStatus")
    assert '\\"account_margin_mode\\":\\"" + AccountMarginModeName()' in body
    assert '\\"account_currency\\":\\"" + JsonEscape(AccountInfoString(ACCOUNT_CURRENCY))' in body
    assert '\\"max_spread_pips_input\\":" + DoubleToString(MaxSpreadPips, 4)' in body


def test_non_hedging_account_rejects_with_feedback_and_consumes_the_signal(source):
    body = _function_body(source, "ProcessPendingSignal")
    start = body.index("if(!AccountModeAllowsTrading())")
    gate = body[start : body.index("return;", start)]
    assert 'WriteExecutionFeedback(tradeId, 0, 0, "REJECTED_ACCOUNT_MODE", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0);' in gate
    assert "FileDelete(filePath);" in gate
    assert "AccountMarginModeName()" in gate  # the reason names the mode


def test_account_mode_gate_sits_after_expiry_and_before_the_symbol_gate(source):
    body = _function_body(source, "ProcessPendingSignal")
    assert _before(body, "REJECTED_EXPIRED", "if(!AccountModeAllowsTrading())")
    assert _before(body, "if(!AccountModeAllowsTrading())", "if(!g_symbols_ok)")
    assert _before(body, "if(!AccountModeAllowsTrading())", "SymbolSelect(symbol")


def test_spread_cap_logic_and_default_are_unchanged(source):
    assert "input double MaxSpreadPips = 2.0;" in source
    body = _function_body(source, "ProcessPendingSignal")
    assert "double effectiveSpreadLimit = MathMin(MaxSpreadPips, signalMaxSpreadPips);" in body
    assert "if(spreadPips > effectiveSpreadLimit)" in body
    assert '"REJECTED_SPREAD"' in body


# ---------------------------------------------------------------- C5 live-account guard


def test_live_guard_inputs_default_to_closed(source):
    assert "input bool RequireDemoAccount = true;" in source
    assert "input bool AllowLiveAccount = false;" in source
    assert "input double LiveMaxLotPerOrder = 0.0;" in source
    assert "input int LiveMaxOrdersPerDay = 0;" in source


def test_real_account_means_trade_mode_is_not_demo(source):
    body = _function_body(source, "IsRealAccount")
    assert "AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO" in body


def test_kill_switch_file_lives_in_the_root_folder(source):
    assert 'string KillSwitchPath() { return RootFolder + "\\\\KILL_SWITCH"; }' in source
    assert "FileIsExist(KillSwitchPath())" in _function_body(source, "KillSwitchPresent")


def test_live_guard_checks_every_condition_with_a_reason(source):
    body = _function_body(source, "LiveGuardAllowsSignal")
    real_block = body[body.index("if(IsRealAccount())") : body.index("if(KillSwitchPresent())")]
    for condition in (
        "if(RequireDemoAccount)",
        "if(!AllowLiveAccount)",
        "if(!(LiveMaxLotPerOrder > 0.0))",
        "if(!(LiveMaxOrdersPerDay > 0))",
    ):
        assert condition in real_block, condition
    assert real_block.count('reason = "') == 4
    assert "if(KillSwitchPresent())" in body and "kill switch file present" in body
    assert "if(LiveMaxOrdersPerDay > 0)" in body
    assert "ReadLiveOrderCount(count, lastTradeId)" in body
    assert "count >= LiveMaxOrdersPerDay && lastTradeId != tradeId" in body  # same-trade retry stays eligible
    assert "daily order cap reached" in body
    assert body.count("return false;") >= 7
    assert body.rstrip().endswith("return true;")


def test_live_guard_gate_rejects_with_feedback_and_consumes_the_signal(source):
    body = _function_body(source, "ProcessPendingSignal")
    start = body.index("if(!LiveGuardAllowsSignal(tradeId, liveGuardReason))")
    gate = body[start : body.index("return;", start)]
    assert 'WriteExecutionFeedback(tradeId, 0, 0, "REJECTED_LIVE_GUARD", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0);' in gate
    assert "FileDelete(filePath);" in gate
    assert 'LogDebug("Live guard reject: " + liveGuardReason' in gate  # precise reason in ea_debug.log


def test_gate_order_is_expiry_then_account_mode_then_live_guard_then_symbol(source):
    body = _function_body(source, "ProcessPendingSignal")
    order = [
        "REJECTED_EXPIRED",
        "if(!AccountModeAllowsTrading())",
        "if(!LiveGuardAllowsSignal(tradeId, liveGuardReason))",
        "if(!g_symbols_ok)",
        "string symbol = BrokerSymbol(canonicalSymbol);",
        "SymbolSelect(symbol",
    ]
    positions = [body.index(item) for item in order]
    assert positions == sorted(positions)


def test_old_top_of_function_demo_return_is_gone(source):
    body = _function_body(source, "ProcessPendingSignal")
    assert "Demo account required; refusing" not in source
    # RequireDemoAccount is now only evaluated inside the live guard
    assert "RequireDemoAccount" not in body
    assert "RequireDemoAccount" in _function_body(source, "LiveGuardAllowsSignal")


def test_lot_ceiling_steps_down_with_an_epsilon_and_rejects_below_minimum(source):
    body = _function_body(source, "ApplyLiveLotCeiling")
    assert "MathFloor(cap / step + 1e-9) * step" in body
    assert "NormalizeDouble(" in body and "VolumeDigits(sym)" in body
    assert "capped < minLot - 1e-9" in body
    assert "return false;" in body
    assert "if(capped < lot)" in body  # never raises the lot
    assert "lot = capped;" in body
    assert "lot = cap" not in body.replace("lot = capped", "")
    assert "if(!(cap > 0.0) || lot <= cap)" in body


def test_lot_ceiling_runs_after_the_minimum_lot_check_and_rejects_with_live_guard(source):
    body = _function_body(source, "ProcessPendingSignal")
    assert body.index("finalLot < SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN)") < body.index("ApplyLiveLotCeiling(symbol, finalLot, lotGuardReason)")
    assert body.index("ApplyLiveLotCeiling(symbol, finalLot, lotGuardReason)") < body.index("PreflightMarketOrder(symbol")
    start = body.index("if(!ApplyLiveLotCeiling(")
    gate = body[start : body.index("return;", start)]
    assert 'WriteExecutionFeedback(tradeId, 0, 0, "REJECTED_LIVE_GUARD", 0.0, 0.0, spread, finalLot, 0.0, 0.0);' in gate
    assert "FileDelete(filePath);" in gate


def test_order_counter_file_is_under_the_feedback_folder_and_not_an_execution_file(source):
    assert 'string LiveOrdersFilePath() { return FeedbackFolder() + "\\\\live_orders_day.json"; }' in source
    # bridge/execution_feedback.py reads execution_*.json; the EA globs management_state_*.json
    assert not "live_orders_day.json".startswith(("execution_", "management_state_", "submission_", "market_quotes"))


def test_order_counter_is_keyed_by_utc_day_and_fails_closed_on_read(source):
    day = _function_body(source, "UtcDayString")
    assert "StringSubstr(IsoTimestampUtc(), 0, 10)" in day
    body = _function_body(source, "ReadLiveOrderCount")
    assert "if(!FileIsExist(path))" in body  # missing file means zero orders
    assert "return true;" in body[body.index("if(!FileIsExist(path))") :][:80]
    assert "if(!ReadTextFile(path, content))" in body
    assert 'ExtractJsonString(content, "utc_day", day)' in body
    assert 'ExtractJsonString(content, "orders", ordersToken)' in body
    assert "if(day != today)" in body
    assert '"1970"' in body  # unavailable clock must not reset the day


def test_order_counter_schema_is_validated_strictly_before_rollover(source):
    body = _function_body(source, "ReadLiveOrderCount")
    # every field is validated before a different day is accepted as rollover
    assert body.index("IsValidUtcDay(day)") < body.index("ParseStrictCount(ordersToken, orders)")
    assert body.index("ParseStrictCount(ordersToken, orders)") < body.index('ExtractJsonString(content, "last_trade_id", storedTradeId)')
    assert body.index('storedTradeId == ""') < body.index("if(day != today)")
    assert "ExtractJsonDouble" not in body  # StringToDouble accepts malformed text as 0
    assert body.index("if(day != today)") < body.index("count = orders;")
    day = _function_body(source, "IsValidUtcDay")
    assert "StringLen(day) != 10" in day
    assert "c != '-'" in day and "c < '0' || c > '9'" in day
    assert "TimeToString(parsed, TIME_DATE) == dotted" in day  # rejects 2026-02-30 and 2026-13-01
    count = _function_body(source, "ParseStrictCount")
    assert "len < 1 || len > 9" in count
    assert "c < '0' || c > '9'" in count  # no sign, decimal point or whitespace


def test_cap_gate_lets_the_counted_trade_id_retry_through(source):
    gate = _function_body(source, "LiveGuardAllowsSignal")
    record = _function_body(source, "RecordLiveOrderIntent")
    assert "bool LiveGuardAllowsSignal(const string tradeId, string &reason)" in source
    # counter advanced to the cap, marker write failed, same signal still pending:
    # the admission gate and the recorder must both accept it without a second count
    assert "count >= LiveMaxOrdersPerDay && lastTradeId != tradeId" in gate
    assert record.index("if(lastTradeId == tradeId)") < record.index("count >= LiveMaxOrdersPerDay")
    assert record.index("if(lastTradeId == tradeId)") < record.index("PublishAtomic(")
    body = _function_body(source, "ProcessPendingSignal")
    marker_fail = body[body.index("Cannot persist submission marker; order not sent") :][:120]
    assert "return;" in marker_fail and "FileDelete" not in marker_fail  # signal stays pending for the retry


def test_order_counter_write_is_atomic_verified_and_fails_closed(source):
    body = _function_body(source, "RecordLiveOrderIntent")
    assert "if(!(LiveMaxOrdersPerDay > 0))" in body
    assert "PublishAtomic(LiveOrdersFilePath(), payload)" in body
    assert "ReadLiveOrderCount(verifyCount, verifyTradeId)" in body
    assert "verifyCount != next" in body
    assert "if(lastTradeId == tradeId)" in body  # idempotent for a retried trade id
    assert "count >= LiveMaxOrdersPerDay" in body
    assert body.count("return false;") == 4
    assert '\\"utc_day\\":\\"" + UtcDayString()' in body


def test_order_counter_is_recorded_before_the_submission_marker(source):
    body = _function_body(source, "ProcessPendingSignal")
    record = body.index("RecordLiveOrderIntent(tradeId, counterReason)")
    marker = body.index("WriteTextFile(submissionMarker, tradeId)")
    assert body.index("PreflightMarketOrder(symbol") < record < marker
    start = body.index("if(!RecordLiveOrderIntent(")
    gate = body[start : body.index("return;", start)]
    assert '"REJECTED_LIVE_GUARD"' in gate
    assert "FileDelete(filePath);" in gate


def test_status_publishes_the_live_guard_object(source):
    status = _function_body(source, "WriteMarketStatus")
    assert '\\"live_guard\\":" + BuildLiveGuardJson()' in status
    body = _function_body(source, "BuildLiveGuardJson")
    for key in ("account_real", "require_demo", "allow_live", "max_lot", "max_orders_per_day", "orders_today", "kill_switch"):
        assert f'\\"{key}\\":' in body, key
    assert "ReadLiveOrderCount(count, lastTradeId) ? count : -1" in body
    assert "DoubleToString(LiveMaxLotPerOrder, 8)" in body


def test_kill_switch_never_touches_position_management(source):
    for name in (
        "ManageOpenPositions",
        "SweepClosedManagedPositions",
        "FinalizeClosedPosition",
        "OnTradeTransaction",
        "OnTimer",
    ):
        body = _function_body(source, name)
        for token in ("KillSwitch", "LiveGuard", "KILL_SWITCH"):
            assert token not in body, f"{name} references {token}"
    assert "PositionClose(" not in source.replace("PositionClosePartial(", "")
    callers = [m.start() for m in re.finditer(r"KillSwitchPresent\(\)", _mask(source))]
    assert len(callers) == 3  # definition, LiveGuardAllowsSignal, BuildLiveGuardJson


def test_trade_functions_are_only_reached_after_the_guards(source):
    body = _function_body(source, "ProcessPendingSignal")
    first_send = min(body.index(call) for call in ("trade.Buy(", "trade.Sell(", "trade.BuyLimit(", "trade.SellLimit("))
    for gate in (
        "if(!AccountModeAllowsTrading())",
        "if(!LiveGuardAllowsSignal(tradeId, liveGuardReason))",
        "if(!g_symbols_ok)",
        "if(!ApplyLiveLotCeiling(",
        "if(!RecordLiveOrderIntent(",
    ):
        assert body.index(gate) < first_send, gate
