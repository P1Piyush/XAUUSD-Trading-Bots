# 📈 XAUUSD AI Trading Bots

Institutional-grade automated trading system for Gold (XAUUSD). This repository bridges high-performance machine learning predictions in Python with real-time execution in MetaTrader 5 (MQL5).

## 🚀 Architecture
1. **AI Engine (`ai_engine.py`)**: A Python backend that processes historical data, trains a Random Forest model, and generates live BUY/SELL signals.
2. **Execution Engine (`ExpertAdvisor.mq5`)**: An MQL5 Expert Advisor that runs inside MetaTrader 5, reads signals from the AI engine, and executes trades with strict risk management.

## 📦 Installation

### 1. Python Setup
```bash
/usr/bin/python3 -m pip install pandas scikit-learn
python3 ai_engine.py
```

### 2. MetaTrader 5 Setup
1. Open MetaTrader 5.
2. Navigate to `File` > `Open Data Folder`.
3. Copy `ExpertAdvisor.mq5` into `MQL5/Experts`.
4. Compile the script and attach it to an XAUUSD chart.

## 🛠 Features
- **Machine Learning Integration**: Real-time signal generation using Random Forest classifiers.
- **Risk Management**: Automated Stop Loss (SL) and Take Profit (TP) calculation.
- **High-Frequency Execution**: Designed for low-latency trade placement in the MT5 ecosystem.
