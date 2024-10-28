# Instalar las librerías necesarias
!pip install python-binance pandas ta requests

# Importar las bibliotecas
import os
import logging
import time
import pandas as pd
from binance.client import Client
from binance.enums import *
from ta.trend import SMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange, BollingerBands

# Configuración de logging
logging.basicConfig(filename='bot.log', level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')

# Define tus claves de API como variables de entorno en Colab

os.environ['BINANCE_API_KEY'] = "5GgzPDI1AcBF1daWk787P5fZKhyb4PGesxKazUnHtejd0El2pNC6ovwGFwuULnML"
os.environ['BINANCE_API_SECRET'] = "5GgzPDI1AcBF1daWk787P5fZKhyb4PGesxKazUnHtejd0El2pNC6ovwGFwuULnML"

# Conectar con la API de Binance usando variables de entorno
api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')
client = Client(api_key, api_secret)

# Parámetros de la estrategia
SMA_SHORT = 50  # Media móvil rápida
SMA_LONG = 200  # Media móvil lenta
STOP_LOSS_PERCENTAGE = 0.05  # Stop-Loss del 5%
TRAILING_STOP_LOSS_BUFFER_BASE = 0.01  # Base del trailing stop-loss
FIXED_TRADE_AMOUNT = 10  # Operar con 97 USDT
MIN_VOLUME = 1000000  # Volumen mínimo para analizar un activo (en USDT)
MIN_VOLATILITY = 0.02  # Volatilidad mínima para filtrar activos
RSI_THRESHOLD_BUY = 30  # RSI para compra (sobreventa)
RSI_THRESHOLD_SELL = 70  # RSI para venta (sobrecompra)

def safe_api_call(call, *args, max_retries=5, delay=60, **kwargs):
    """
    Realiza llamadas a la API de Binance con manejo de excepciones y reintentos.
    """
    retries = 0
    while retries < max_retries:
        try:
            return call(*args, **kwargs)
        except Exception as e:
            logging.error(f"Error en la API: {e}. Reintentando en {delay} segundos...")
            retries += 1
            time.sleep(delay)
    raise Exception(f"Error persistente después de {max_retries} intentos.")

def get_symbols():
    """
    Obtener todos los símbolos disponibles en Binance que operan con USDT y filtrar por volumen.
    """
    exchange_info = safe_api_call(client.get_exchange_info)
    symbols = [s['symbol'] for s in exchange_info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
    return symbols

def filter_symbols_by_volume(symbols, min_volume=MIN_VOLUME):
    """
    Filtrar los símbolos basados en el volumen diario.
    """
    filtered_symbols = []
    for symbol in symbols:
        tickers = safe_api_call(client.get_ticker, symbol=symbol)
        volume = float(tickers['quoteVolume'])
        if volume >= min_volume:
            filtered_symbols.append(symbol)
    return filtered_symbols

def filter_symbols_by_volatility(symbols, min_volatility=MIN_VOLATILITY):
    """
    Filtrar los símbolos basados en la volatilidad histórica.
    """
    filtered_symbols = []
    for symbol in symbols:
        data = get_data(symbol)
        data['price_change'] = data['close'].pct_change()
        volatility = data['price_change'].std()
        if volatility >= min_volatility:
            filtered_symbols.append(symbol)
    return filtered_symbols

def get_data(symbol, limit=500):
    """
    Obtener datos históricos de precios (velas) de Binance.
    """
    klines = safe_api_call(client.get_klines, symbol=symbol, interval=Client.KLINE_INTERVAL_1DAY, limit=limit)
    data = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 
                                         'quote_asset_volume', 'number_of_trades', 'taker_buy_base_asset_volume', 
                                         'taker_buy_quote_asset_volume', 'ignore'])
    data['close'] = pd.to_numeric(data['close'])
    return data

def apply_advanced_strategy(data):
    """
    Aplicar una estrategia combinada de SMA, RSI y Bandas de Bollinger.
    """
    sma_short = SMAIndicator(close=data['close'], window=SMA_SHORT).sma_indicator()
    sma_long = SMAIndicator(close=data['close'], window=SMA_LONG).sma_indicator()
    rsi = RSIIndicator(close=data['close'], window=14).rsi()
    bb = BollingerBands(close=data['close'], window=20, window_dev=2)

    data['SMA_short'] = sma_short
    data['SMA_long'] = sma_long
    data['RSI'] = rsi
    data['bb_upper'] = bb.bollinger_hband()
    data['bb_lower'] = bb.bollinger_lband()
    data['position'] = 0

    # Señal de cruce y filtro de RSI
    data['signal'] = 0
    data.loc[(data['SMA_short'] > data['SMA_long']) & (data['RSI'] < RSI_THRESHOLD_BUY), 'signal'] = 1  # Comprar
    data.loc[(data['SMA_short'] < data['SMA_long']) & (data['RSI'] > RSI_THRESHOLD_SELL), 'signal'] = -1  # Vender
    
    data['position'] = data['signal'].diff()
    return data

def calculate_trailing_stop_loss(purchase_price, current_price, data):
    """
    Calcular el trailing stop-loss usando el ATR para adaptarse a la volatilidad.
    """
    atr = AverageTrueRange(high=data['high'], low=data['low'], close=data['close'], window=14).average_true_range()
    trailing_buffer = max(TRAILING_STOP_LOSS_BUFFER_BASE, atr.iloc[-1] / 100)
    stop_loss_price = max(purchase_price * (1 - STOP_LOSS_PERCENTAGE), current_price * (1 - trailing_buffer))
    return stop_loss_price

def get_current_quantity(symbol):
    """
    Obtener la cantidad actual del activo en la cuenta.
    """
    try:
        position = safe_api_call(client.get_asset_balance, asset=symbol[:-4])
        return float(position['free'])
    except:
        return 0

def get_purchase_price(symbol):
    """
    Obtener el precio de compra de la última operación.
    """
    trades = safe_api_call(client.get_my_trades, symbol=symbol)
    return float(trades[-1]['price']) if trades else 0

def calculate_trade_qty(symbol, current_price, fixed_amount=FIXED_TRADE_AMOUNT):
    """
    Calcular la cantidad a comprar basada en un monto fijo en USD.
    """
    trade_qty = fixed_amount / current_price
    step_size = safe_api_call(client.get_symbol_info, symbol=symbol)['filters'][2]['stepSize']
    trade_qty = round(trade_qty - (trade_qty % float(step_size)), 8)
    return trade_qty

def execute_advanced_trades(symbol, data):
    """
    Ejecutar órdenes de compra o venta basado en la estrategia avanzada, 
    considerando Stop-Loss, Take-Profit y Trailing Stop-Loss ajustado por ATR.
    """
    last_row = data.iloc[-1]
    current_price = last_row['close']
    current_qty = get_current_quantity(symbol)
    
    if current_qty > 0:  # Si ya hay una posición abierta
        purchase_price = get_purchase_price(symbol)
        stop_loss_price = calculate_trailing_stop_loss(purchase_price, current_price, data)
        
        # Vender si se alcanza el stop-loss
        if current_price <= stop_loss_price:
            safe_api_call(client.order_market_sell, symbol=symbol, quantity=current_qty)
            logging.info(f"Vendido {current_qty} de {symbol} a {current_price}. Stop-Loss alcanzado.")
            return
    
    # Condiciones de compra y venta con filtro de RSI y Bollinger Bands
    if last_row['position'] == 1 and current_qty == 0 and current_price < last_row['bb_lower']:
        trade_qty = calculate_trade_qty(symbol, current_price)
        safe_api_call(client.order_market_buy, symbol=symbol, quantity=trade_qty)
        logging.info(f"Comprado {trade_qty} de {symbol} a {current_price}.")
    
    elif last_row['position'] == -1 and current_qty > 0 and current_price > last_row['bb_upper']:
        safe_api_call(client.order_market_sell, symbol=symbol, quantity=current_qty)
        logging.info(f"Vendido {current_qty} de {symbol} a {current_price}.")

def main():
    """
    Ejecutar la estrategia en un ciclo continuo.
    """
    while True:
        symbols = get_symbols()
        symbols = filter_symbols_by_volume(symbols)
        symbols = filter_symbols_by_volatility(symbols)

        for symbol in symbols:
            data = get_data(symbol)
            data = apply_advanced_strategy(data)
            execute_advanced_trades(symbol, data)

        # Esperar 4 horas antes de la próxima ejecución
        time.sleep(14400)

if __name__ == "__main__":
    main()
