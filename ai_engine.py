import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
import json
import os

class XAUUSDAIEngine:
    def __init__(self):
        self.model = RandomForestClassifier(n_estimators=100, random_state=42)
        
    def generate_dummy_data(self):
        # Generate dummy market data for demonstration
        np.random.seed(42)
        rows = 1000
        data = {
            'open': np.random.uniform(2000, 2100, rows),
            'high': np.random.uniform(2100, 2150, rows),
            'low': np.random.uniform(1950, 2000, rows),
            'close': np.random.uniform(2000, 2100, rows),
            'volume': np.random.randint(1000, 5000, rows)
        }
        df = pd.DataFrame(data)
        # Target: 1 if close > open of next day, else 0
        df['target'] = (df['close'].shift(-1) > df['close']).astype(int)
        return df.dropna()

    def train(self, df):
        X = df[['open', 'high', 'low', 'close', 'volume']]
        y = df['target']
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2)
        self.model.fit(X_train, y_train)
        print(f"Model trained with accuracy: {self.model.score(X_test, y_test):.2f}")

    def predict(self, current_data):
        # current_data should be a dict of latest OHLCV
        features = np.array([[current_data['open'], current_data['high'], 
                             current_data['low'], current_data['close'], 
                             current_data['volume']]])
        prediction = self.model.predict(features)[0]
        return "BUY" if prediction == 1 else "SELL"

if __name__ == "__main__":
    engine = XAUUSDAIEngine()
    data = engine.generate_dummy_data()
    engine.train(data)
    
    # Simulate a prediction
    latest = {'open': 2050.0, 'high': 2065.0, 'low': 2045.0, 'close': 2060.0, 'volume': 3000}
    signal = engine.predict(latest)
    print(f"Signal generated for XAUUSD: {signal}")
    
    # Save signal for EA to read
    with open("trade_signal.json", "w") as f:
        json.dump({"signal": signal, "timestamp": "2026-04-29T02:15:00Z"}, f)
