package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

const fetchTimeout = 2 * time.Second

var httpClient = &http.Client{Timeout: fetchTimeout}

// Snapshot is the paper session the Python app publishes. Missing fields and
// JSON nulls stay at their zero values.
type Snapshot struct {
	Paper        bool          `json:"paper"`
	SharedWallet bool          `json:"shared_wallet"`
	UI           string        `json:"ui"`
	Now          float64       `json:"now"`
	UptimeS      float64       `json:"uptime_s"`
	Events       float64       `json:"events"`
	SolUSD       *float64      `json:"sol_usd"`
	Wallet       *Wallet       `json:"wallet"`
	Sources      []Source      `json:"sources"`
	TradeStreams *TradeStreams `json:"trade_streams"`
	Strategies   []Strategy    `json:"strategies"`
	Positions    []Position    `json:"positions"`
	Tokens       []Token       `json:"tokens"`
	Sniper       *Sniper       `json:"sniper"`
	Boards       *Boards       `json:"boards"`
	People       []Card        `json:"people"`
	PeopleNote   string        `json:"people_note"`
	Feed         []string      `json:"feed"`
	ScanNote     string        `json:"scan_note"`
}

// Wallet is the user's paper wallet, measured against the goal. It is null
// until the engine runs. Best names the strategy whose copy it is and is empty
// when every strategy trades one shared wallet. USD fields are null when there
// is no SOL price. PnlPct and GoalPct are percent points.
type Wallet struct {
	Strategies int          `json:"strategies"`
	Best       string       `json:"best"`
	StartSOL   float64      `json:"start_sol"`
	StartUSD   *float64     `json:"start_usd"`
	BalanceSOL float64      `json:"balance_sol"`
	BalanceUSD *float64     `json:"balance_usd"`
	PnlSOL     float64      `json:"pnl_sol"`
	PnlUSD     *float64     `json:"pnl_usd"`
	PnlPct     *float64     `json:"pnl_pct"`
	GoalUSD    *float64     `json:"goal_usd"`
	GoalPct    *float64     `json:"goal_pct"`
	Reached    []string     `json:"reached"`
	Range      *WalletRange `json:"range"`
}

// WalletRange is the session's highest and lowest wallet value, in USD when
// Unit is "usd" and otherwise in SOL. FromHighPct is how far below the high
// the wallet is now, in percent points (0 or negative). It is null when there
// is no history.
type WalletRange struct {
	Unit        string   `json:"unit"`
	High        float64  `json:"high"`
	HighTS      *float64 `json:"high_ts"`
	Low         float64  `json:"low"`
	LowTS       *float64 `json:"low_ts"`
	FromHighPct *float64 `json:"from_high_pct"`
}

type Source struct {
	Name   string `json:"name"`
	Status string `json:"status"`
	Events int    `json:"events"`
	Errors int    `json:"errors"`
	Note   string `json:"note"`
}

type TradeStreams struct {
	Unavailable     bool `json:"unavailable"`
	Subscribed      int  `json:"subscribed"`
	EventsToday     int  `json:"events_today"`
	BudgetExhausted bool `json:"budget_exhausted"`
}

type Strategy struct {
	Key        string   `json:"key"`
	Role       string   `json:"role"`
	Open       int      `json:"open"`
	Closed     int      `json:"closed"`
	WinPct     float64  `json:"win_pct"`
	PnlSOL     float64  `json:"pnl_sol"`
	MadeUSD    *float64 `json:"made_usd"`
	CashSOL    *float64 `json:"cash_sol"`
	BalanceSOL float64  `json:"balance_sol"`
	BalanceUSD *float64 `json:"balance_usd"`
}

type Position struct {
	Coin     string `json:"coin"`
	Strategy string `json:"strategy"`
	Status   string `json:"status"`
	Bought   string `json:"bought"`
	Entry    string `json:"entry"`
	Sold     string `json:"sold"`
	Mark     string `json:"mark"`
	Made     string `json:"made"`
}

type Token struct {
	Symbol    string `json:"symbol"`
	Mint      string `json:"mint"`
	MintShort string `json:"mint_short"`
	Phase     string `json:"phase"`
	Detail    string `json:"detail"`
	Age       string `json:"age"`
	Source    string `json:"source"`
}

type Sniper struct {
	Labels     int      `json:"labels"`
	Threshold  *float64 `json:"threshold"`
	BuyWinRate *float64 `json:"buy_win_rate"`
}

type Boards struct {
	Trenches       *Trenches     `json:"trenches"`
	TrenchesSource string        `json:"trenches_source"`
	Trending       []TrendingRow `json:"trending"`
	TrendingSource string        `json:"trending_source"`
	Pump           *Pump         `json:"pump"`
	Cards          []Card        `json:"cards"`
}

type Trenches struct {
	NewCreation    []TrenchRow `json:"new_creation"`
	NearCompletion []TrenchRow `json:"near_completion"`
	Completed      []TrenchRow `json:"completed"`
}

type TrenchRow struct {
	Symbol  string   `json:"symbol"`
	McapUSD *float64 `json:"mcap_usd"`
	Holders *float64 `json:"holders"`
	Mint    string   `json:"mint"`
}

type TrendingRow struct {
	Symbol    string   `json:"symbol"`
	McapUSD   *float64 `json:"mcap_usd"`
	VolumeUSD *float64 `json:"volume_usd"`
	Holders   *float64 `json:"holders"`
	Mint      string   `json:"mint"`
}

type Pump struct {
	New       []PumpRow `json:"new"`
	Almost    []PumpRow `json:"almost"`
	Graduated []PumpRow `json:"graduated"`
}

type PumpRow struct {
	Symbol     string   `json:"symbol"`
	McapUSD    *float64 `json:"mcap_usd"`
	BondingPct *float64 `json:"bonding_pct"`
	Mint       string   `json:"mint"`
}

type Card struct {
	Verdict string `json:"verdict"`
	Text    string `json:"text"`
}

func fetchSnapshot(url string) (Snapshot, error) {
	ctx, cancel := context.WithTimeout(context.Background(), fetchTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return Snapshot{}, err
	}
	req.Header.Set("Accept", "application/json")
	resp, err := httpClient.Do(req)
	if err != nil {
		return Snapshot{}, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return Snapshot{}, err
	}
	if resp.StatusCode != http.StatusOK {
		return Snapshot{}, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	var snap Snapshot
	if err := json.Unmarshal(body, &snap); err != nil {
		return Snapshot{}, fmt.Errorf("snapshot JSON: %w", err)
	}
	return snap, nil
}
