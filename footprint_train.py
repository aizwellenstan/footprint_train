import os
import sys
import argparse
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import TensorDataset, DataLoader

# Windows API for high-speed Named Pipes
import win32pipe
import win32file
import pywintypes

# =====================================================================
# CORE ARCHITECTURE (USED BY BOTH MODES)
# =====================================================================
class SpatialEncoder1D(nn.Module):
    def __init__(self, in_features=3):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv1d(in_channels=in_features, out_channels=16, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.Conv1d(in_channels=16, out_channels=32, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.AdaptiveAvgPool1d(1) 
        )
    def forward(self, x):
        return self.conv(x).squeeze(-1)

class DirectionalFootprintCNNLSTM(nn.Module):
    def __init__(self, lookback=15, max_price_ticks=40):
        super().__init__()
        self.spatial_cnn = SpatialEncoder1D(in_features=3) 
        self.lstm = nn.LSTM(input_size=32, hidden_size=64, num_layers=1, batch_first=True)
        self.fc = nn.Linear(64, 3) # 0 = Short, 1 = Neutral, 2 = Long
        
    def forward(self, x):
        b, seq_len, ticks, feats = x.shape
        x_flat = x.view(b * seq_len, ticks, feats).transpose(1, 2)
        spatial_embeddings = self.spatial_cnn(x_flat)              
        lstm_in = spatial_embeddings.view(b, seq_len, 32)
        lstm_out, _ = self.lstm(lstm_in)
        return self.fc(lstm_out[:, -1, :])

# =====================================================================
# FUNCTION 1: OFFLINE TRAINING PIPELINE
# =====================================================================
def run_historical_training(csv_path, max_ticks, lookback):
    print(f"\n[TRAIN MODE] Initializing Offline Training Engine...")
    if not os.path.exists(csv_path):
        print(f"File Error: '{csv_path}' not found. Export data from Sierra Chart first.")
        return

    df = pd.read_csv(csv_path)
    unique_bars = sorted(df["BarIndex"].unique())
    total_minutes = len(unique_bars)
    print(f"Formatting {total_minutes} historical bars into 3D grid structures...")

    grid_3d = np.zeros((total_minutes, max_ticks, 3))
    for idx, bar_id in enumerate(unique_bars):
        bar_data = df[df["BarIndex"] == bar_id].sort_values(by="PriceLevel")
        ticks_to_copy = min(len(bar_data), max_ticks)
        if ticks_to_copy == 0: continue
        
        grid_3d[idx, :ticks_to_copy, 0] = bar_data["BidVolume"].to_numpy()[:ticks_to_copy]
        grid_3d[idx, :ticks_to_copy, 1] = bar_data["AskVolume"].to_numpy()[:ticks_to_copy]
        grid_3d[idx, :ticks_to_copy, 2] = (bar_data["IsBarHigh"] | bar_data["IsBarLow"]).to_numpy()[:ticks_to_copy]

    X, Y = [], []
    for i in range(lookback, total_minutes - 5):
        X.append(grid_3d[i - lookback : i])
        future_return = df[df["BarIndex"] == unique_bars[i+5]]["Close"].iloc[0] - df[df["BarIndex"] == unique_bars[i]]["Close"].iloc[0]
        if future_return < -1.5:   Y.append(0) # Short
        elif future_return > 1.5:  Y.append(2) # Long
        else:                      Y.append(1) # Neutral

    X_tensor = torch.tensor(np.array(X), dtype=torch.float32)
    Y_tensor = torch.tensor(np.array(Y), dtype=torch.long)

    model = DirectionalFootprintCNNLSTM(lookback=lookback, max_price_ticks=max_ticks)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.001, weight_decay=1e-4)
    criterion = nn.CrossEntropyLoss()
    loader = DataLoader(TensorDataset(X_tensor, Y_tensor), batch_size=64, shuffle=True)
    
    model.train() # Turn on gradient tracking/dropout layers
    print("Optimizing neural weights across training epochs...")
    for epoch in range(5):
        total_loss = 0.0
        for bx, by in loader:
            optimizer.zero_grad()
            loss = criterion(model(bx), by)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        print(f"Epoch {epoch+1}/5 | Cross-Entropy Loss: {total_loss / len(loader):.5f}")
        
    torch.save(model.state_dict(), "directional_footprint_model.pt")
    print("Success: Weights file checkpoint saved to disk as 'directional_footprint_model.pt'.")

# =====================================================================
# FUNCTION 2: LIVE PRODUCTION PREDICTOR PIPELINE
# =====================================================================
def run_live_predictor(max_ticks, lookback, confidence_threshold=0.70):
    print(f"\n[LIVE MODE] Booting Real-time Production Receiver...")
    SIGNAL_MAP = {0: -1, 1: 0, 2: 1}
    LABEL_MAP  = {-1: "SHORT 📉", 0: "NEUTRAL ⏳", 1: "LONG 📈"}

    model = DirectionalFootprintCNNLSTM(lookback=lookback, max_price_ticks=max_ticks)
    if os.path.exists("directional_footprint_model.pt"):
        model.load_state_dict(torch.load("directional_footprint_model.pt"))
        print("Model configuration loaded successfully from disk.")
    else:
        print("Warning: Running with unoptimized raw initialization weights.")
        
    model.eval() # CRITICAL: Freezes dropout/batchnorm for exact evaluations

    pipe_path = r'\\.\pipe\sc_footprint_stream'
    pipe = win32pipe.CreateNamedPipe(
        pipe_path, win32pipe.PIPE_ACCESS_INBOUND,
        win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
        1, 65536, 65536, 0, None
    )

    print(f"Windows Named Pipe Active. Open Sierra Chart study to link stream channel...")
    win32pipe.ConnectNamedPipe(pipe, None)

    historical_minutes_buffer = {}

    while True:
        try:
            resp, data = win32file.ReadFile(pipe, 65536)
            message = data.decode('utf-8')
            if not message.startswith("START_BAR") or not message.strip().endswith("END_BAR"): continue 
                
            parts = message.split("|")[1].split(";")
            bar_idx = int(parts[0].split(",")[0])
            
            live_bar_grid = np.zeros((max_ticks, 3))
            tick_idx = 0
            
            for row in parts[1:]:
                if not row.strip() or ',' not in row or tick_idx >= max_ticks: continue
                price_level, bid_vol, ask_vol, is_high, is_low = map(float, row.split(","))
                live_bar_grid[tick_idx, 0] = bid_vol
                live_bar_grid[tick_idx, 1] = ask_vol
                live_bar_grid[tick_idx, 2] = int(is_high) | int(is_low)
                tick_idx += 1
                
            historical_minutes_buffer[bar_idx] = live_bar_grid
            active_keys = sorted(historical_minutes_buffer.keys())
            
            if len(active_keys) >= lookback:
                input_window = [historical_minutes_buffer[k] for k in active_keys[-lookback:]]
                input_tensor = torch.tensor(np.array(input_window), dtype=torch.float32).unsqueeze(0)
                
                with torch.no_grad(): # CRITICAL: Cuts out gradient processing maps to optimize execution RAM
                    logits = model(input_tensor)
                    probabilities = F.softmax(logits, dim=1).squeeze(0).numpy()
                    
                best_class_idx = np.argmax(probabilities)
                final_signal = SIGNAL_MAP[best_class_idx] if probabilities[best_class_idx] >= confidence_threshold else 0
                
                print(f"BarIndex: {bar_idx} | Signal: {final_signal:2d} ({LABEL_MAP[final_signal]}) | "
                      f"S: {probabilities[0]*100:.1f}% | N: {probabilities[1]*100:.1f}% | L: {probabilities[2]*100:.1f}%")
                    
        except pywintypes.error as e:
            if e.winerror == 109:
                print("Pipeline stream disconnected by client side app.")
                break

# =====================================================================
# THE SYSTEM SWITCH MECHANISM
# =====================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Footprint ML Pipeline Router")
    parser.add_argument(
        "--mode", 
        type=str, 
        required=True, 
        choices=["train", "live"], 
        help="Select 'train' for training or 'live' for real-time predictions"
    )
    
    args = parser.parse_args()

    # Shared parameter definitions
    CSV_SOURCE_PATH = "C:/SierraChart/Data/footprint_history.csv"
    MAX_PRICE_TICKS = 40
    LOOKBACK_PERIOD = 15

    # Route execution based on command line choice
    if args.mode == "train":
        run_historical_training(CSV_SOURCE_PATH, MAX_PRICE_TICKS, LOOKBACK_PERIOD)
    elif args.mode == "live":
        run_live_predictor(MAX_PRICE_TICKS, LOOKBACK_PERIOD, confidence_threshold=0.70)
