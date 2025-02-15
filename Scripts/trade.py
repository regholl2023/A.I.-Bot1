import logging
from alpaca_trade_api import REST
from datetime import datetime
from .config import APCA_API_KEY_ID, APCA_API_SECRET_KEY, APCA_API_BASE_URL, NOTIONAL
from .database import get_session, StockData
from .indicators import compute_technical_indicators, prepare_features
from .modeling import retrain_model
from .utils import is_market_open

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s:%(levelname)s:%(message)s'
)

api = REST(
    key_id=APCA_API_KEY_ID,
    secret_key=APCA_API_SECRET_KEY,
    base_url=APCA_API_BASE_URL
)

active_positions = {}
latest_indicators_dict = {}
previous_obv = {}
bar_count_since_last_train = 0


def get_current_position(symbol):
    """Returns the current position size for a given symbol, 0 if none."""
    try:
        position = api.get_position(symbol)
        qty = float(position.qty)
        logging.info(f"Current position for {symbol}: {qty} shares.")
        return qty
    except Exception:
        # No position
        return 0.0


def close_position(symbol):
    """Closes any open position in 'symbol'."""
    try:
        position = api.get_position(symbol)
        qty = float(position.qty)
        side = 'sell' if qty > 0 else 'buy'
        api.submit_order(
            symbol=symbol,
            qty=abs(qty),
            side=side,
            type='market',
            time_in_force='day'
        )
        logging.info(f"Closed position for {symbol}: {side} {abs(qty)} shares.")
    except Exception as e:
        logging.error(f"Error closing position for {symbol}: {e}")


def short_position(symbol, shares):
    """
    Submits a market order to short-sell a specified number of shares of a stock.
    
    Parameters:
        symbol (str): The ticker symbol of the stock (e.g., 'AAPL').
        shares (int): The number of shares to short.
    """
    logging.info(f"Submitting SHORT SELL order for {symbol} with {shares} shares.")
    try:
        # This creates a short position if you do not already hold these shares.
        api.submit_order(
            symbol=symbol,
            qty=shares,
            side='sell',
            type='market',
            time_in_force='day'
        )
        logging.info(f"Successfully submitted SHORT SELL order for {symbol} with {shares} shares.")
    except Exception as e:
        logging.error(f"Error submitting short sell order for {symbol}: {e}")


def submit_notional_order(symbol, side, notional):
    """Submits a market order based on a notional amount."""
    logging.info(f"Submitting {side.upper()} order for {symbol}, notional: {notional}")
    try:
        api.submit_order(
            symbol=symbol,
            notional=notional,
            side=side,
            type='market',
            time_in_force='day'
        )
        logging.info(f"Order submitted: {side.upper()} {symbol} with notional {notional}")
    except Exception as e:
        logging.error(f"Error submitting order for {symbol}: {e}")


def on_bar(bar, model):
    """
    Callback for each new bar. Inserts data, updates indicators, retrains model if needed,
    and triggers trade logic if the market is open.
    """
    from .database import insert_stock_data  # Local import to avoid circular dependencies
    global bar_count_since_last_train, active_positions, previous_obv, latest_indicators_dict

    sym = bar.symbol
    ts = bar.timestamp

    # Insert the new bar into the database
    try:
        insert_stock_data(sym, bar)
        logging.info(f"Inserted new bar for {sym} at {ts}.")
    except Exception as e:
        logging.error(f"Error inserting bar for {sym}: {e}")
        return model

    # Periodic retraining logic
    bar_count_since_last_train += 1
    if bar_count_since_last_train >= 10:  # Adjust frequency as needed
        try:
            new_model = retrain_model()
            if new_model is not None:
                model = new_model
                logging.info("Model retrained successfully.")
            bar_count_since_last_train = 0
        except Exception as e:
            logging.error(f"Error during model retraining: {e}")

    if model is None:
        logging.info("No trained model available. Skipping predictions.")
        return model

    # Fetch recent data from DB for feature generation
    try:
        with get_session() as session:
            records = (
                session.query(StockData)
                .filter(StockData.symbol == sym)
                .order_by(StockData.timestamp.desc())
                .limit(30)
                .all()
            )
    except Exception as e:
        logging.error(f"Error fetching data from database for {sym}: {e}")
        return model

    if not records or len(records) < 30:
        logging.warning(f"Insufficient data for {sym}. Skipping processing.")
        return model

    # Prepare data for feature generation
    try:
        import pandas as pd
        data = pd.DataFrame(
            [
                {
                    'open': r.open,
                    'high': r.high,
                    'low': r.low,
                    'close': r.close,
                    'volume': r.volume,
                    'timestamp': r.timestamp,
                }
                for r in records
            ]
        )
        data = data.sort_values('timestamp')

        # Compute indicators only if they are not already computed
        if "indicators_computed" not in data.columns or not data["indicators_computed"].all():
            data = compute_technical_indicators(data)
            data["indicators_computed"] = True  # Flag to indicate indicators are computed
            logging.info(f"Computed technical indicators for {sym}.")
        else:
            logging.info(f"Indicators already computed for {sym}. Skipping recomputation.")

        if data.empty:
            logging.warning(f"No technical indicators computed for {sym}. Skipping.")
            return model

        X_all, _ = prepare_features(data)
        if X_all.empty:
            logging.warning(f"No features prepared for {sym}. Skipping.")
            return model
    except Exception as e:
        logging.error(f"Error preparing data for {sym}: {e}")
        return model

    # Store latest indicators
    try:
        last_row = data.iloc[-1]
        macd_diff = last_row.get('macd_diff', 0.0)
        current_obv = last_row.get('obv', 0.0)
        prev_obv_val = previous_obv.get(sym, current_obv)
        previous_obv[sym] = current_obv

        latest_indicators_dict[sym] = {
            'macd_diff': macd_diff,
            'obv': current_obv,
            'obv_prev': prev_obv_val,
        }
    except Exception as e:
        logging.error(f"Error storing indicators for {sym}: {e}")
        return model

    # Execute trading logic if market is open
    if is_market_open():
        try:
            # Make predictions
            ensemble_pred = model.predict_proba(X_all)[:, 1]
            latest_prob = ensemble_pred[-1]

            # Execute trade logic
            execute_trade([(sym, latest_prob)], model)
            logging.info(f"Executed trade logic for {sym}. Probability: {latest_prob:.2f}")
        except Exception as e:
            logging.error(f"Error during prediction/trade execution for {sym}: {e}")

    return model




def execute_trade(pred_results, model):
    """
    Execute trades based on model predictions and simple threshold logic.
    Includes logic for buying, selling, and shorting.
    """
    global active_positions
    BUY_THRESHOLD = 0.55
    SELL_THRESHOLD = 0.45
    SHORT_THRESHOLD = 0.44  # Probability below which to short
    COVER_THRESHOLD = 0.50  # Probability above which to cover shorts

    for symbol, prob in pred_results:
        logging.info(f"Symbol: {symbol}, Probability: {prob}")

        indicators = latest_indicators_dict.get(symbol, {})
        macd_diff = indicators.get('macd_diff', 0.0)
        obv = indicators.get('obv', 0.0)
        obv_prev = indicators.get('obv_prev', obv)
        obv_increasing = (obv > obv_prev)

        want_to_buy = (prob > BUY_THRESHOLD) and obv_increasing
        want_to_sell = (prob < SELL_THRESHOLD) and (symbol in active_positions and active_positions[symbol] == 'long')
        want_to_short = (prob < SHORT_THRESHOLD) and not obv_increasing
        want_to_cover = (prob > COVER_THRESHOLD) and (symbol in active_positions and active_positions[symbol] == 'short')

        # Handle buying logic
        if want_to_buy:
            if symbol not in active_positions:
                logging.info(f"Buying {symbol}: prob={prob:.2f}, macd_diff={macd_diff:.4f}, obv_increasing={obv_increasing}")
                submit_notional_order(symbol, 'buy', NOTIONAL)
                active_positions[symbol] = 'long'
            else:
                logging.info(f"Already holding {symbol}, no need to buy again.")

        # Handle selling logic
        elif want_to_sell:
            logging.info(f"Selling {symbol}: prob={prob:.2f}")
            close_position(symbol)
            del active_positions[symbol]

        # Handle shorting logic
        elif want_to_short:
            if symbol not in active_positions:
                latest_trade = api.get_latest_trade(symbol)  # Returns a TradeV2 object
                price = latest_trade.price                  # Extract the numeric price
                shares_to_short = int(NOTIONAL // price)    # Now this will work as intended

                if shares_to_short > 0:
                    logging.info(f"Shorting {symbol}: prob={prob:.2f}, macd_diff={macd_diff:.4f}, obv_increasing={obv_increasing}, shares={shares_to_short}")
                    short_position(symbol, shares_to_short)  # Use shares instead of notional for shorting
                    active_positions[symbol] = 'short'
                else:
                    logging.warning(f"Insufficient shares to short for {symbol} at current price {price:.2f} with notional {NOTIONAL:.2f}.")
            else:
                logging.info(f"Already holding {symbol}, no need to short again.")

        # Handle covering shorts
        elif want_to_cover:
            logging.info(f"Covering short position for {symbol}: prob={prob:.2f}")
            close_position(symbol)
            del active_positions[symbol]

        else:
            logging.info(f"{symbol}: Prob={prob:.2f}, no buy/sell/short/cover signal triggered.")

