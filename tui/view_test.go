package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

func TestFormatUptime(t *testing.T) {
	cases := map[float64]string{
		0:     "0s",
		45:    "45s",
		125.4: "2m 05s",
		3661:  "1h 01m 01s",
		-4:    "0s",
	}
	for in, want := range cases {
		if got := formatUptime(in); got != want {
			t.Errorf("formatUptime(%v) = %q, want %q", in, got, want)
		}
	}
}

func TestDecodeNulls(t *testing.T) {
	raw := []byte(`{
		"paper": true,
		"sol_usd": null,
		"trade_streams": null,
		"sniper": null,
		"boards": null,
		"strategies": [{"key": "a", "pnl_sol": -0.2, "made_usd": null, "balance_usd": null}],
		"positions": null,
		"tokens": null,
		"people": null,
		"feed": null
	}`)
	var snap Snapshot
	if err := json.Unmarshal(raw, &snap); err != nil {
		t.Fatal(err)
	}
	if snap.SolUSD != nil || snap.TradeStreams != nil || snap.Sniper != nil || snap.Boards != nil {
		t.Fatalf("null objects were not cleared: %+v", snap)
	}
	if snap.Strategies[0].MadeUSD != nil || snap.Strategies[0].BalanceUSD != nil {
		t.Fatal("null money was not cleared")
	}
	m := sampleModel(t)
	m.snap = &snap
	m.haveSnap = true
	plain := stripANSI(m.View())
	if !strings.Contains(plain, "No paper buys yet.") {
		t.Fatalf("positions view missing empty state:\n%s", plain)
	}
}

func TestWaitingThenStale(t *testing.T) {
	m := newModel("http://127.0.0.1:9/api/snapshot")
	m.width, m.height = 100, 32
	plain := stripANSI(m.View())
	if !strings.Contains(plain, "Waiting for a snapshot") {
		t.Fatalf("first paint:\n%s", plain)
	}
	if !strings.Contains(plain, "nothing here sends a transaction") {
		t.Fatal("disclaimer missing before data")
	}
	next, _ := m.Update(fetchedMsg{err: errors.New("connection refused")})
	m = next.(*model)
	plain = stripANSI(m.View())
	if !strings.Contains(plain, "connection refused") || !strings.Contains(plain, "Waiting for a snapshot") {
		t.Fatalf("error should keep the waiting body:\n%s", plain)
	}
	usd := 148.2
	made := 12.45
	bal := 164.5
	mcap := 12000.0
	holders := 40.0
	vol := 15000.0
	bond := 12.0
	threshold := 0.62
	win := 0.4
	snap := Snapshot{
		Paper:        true,
		UptimeS:      125.4,
		Events:       842,
		SolUSD:       &usd,
		Sources:      []Source{{Name: "pumpportal", Status: "ok", Events: 120, Errors: 0}},
		TradeStreams: &TradeStreams{Subscribed: 4, EventsToday: 90},
		Strategies: []Strategy{{
			Key: "dex_curve_momentum@3", Role: "champion", Open: 1, Closed: 6,
			WinPct: 50, PnlSOL: 0.084, MadeUSD: &made, CashSOL: floatPtr(0.92),
			BalanceSOL: 1.11, BalanceUSD: &bal,
		}},
		Positions: []Position{{
			Coin: "OTTR", Strategy: "dex_curve_momentum", Status: "holding 50%",
			Bought: "0.100 SOL", Entry: "1e-08", Sold: "0.090 SOL",
			Mark: "2.00x tp1", Made: "+0.020 SOL (+$2.96)",
		}},
		Tokens: []Token{{
			Symbol: "OTTR", Mint: "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi",
			MintShort: "4vJ9…LKi", Phase: "holding", Detail: "dex_curve_momentum open", Age: "2m",
		}},
		Sniper: &Sniper{Labels: 12, Threshold: &threshold, BuyWinRate: &win},
		Boards: &Boards{
			Trenches: &Trenches{NewCreation: []TrenchRow{{
				Symbol: "AAA", McapUSD: &mcap, Holders: &holders, Mint: "mint",
			}}},
			Trending: []TrendingRow{{
				Symbol: "BBB", McapUSD: floatPtr(90000), VolumeUSD: &vol, Holders: floatPtr(200), Mint: "mint",
			}},
			Pump:  &Pump{New: []PumpRow{{Symbol: "CCC", McapUSD: floatPtr(8000), BondingPct: &bond, Mint: "mint"}}},
			Cards: []Card{{Verdict: "watch", Text: "OTTR\nverdict: watch"}},
		},
		People: []Card{{Verdict: "copy", Text: "wallet line\nmore"}},
		Feed:   []string{"snipe BUY OTTR 4vJ9 0.81 organic", "older"},
	}
	next, _ = m.Update(fetchedMsg{snap: snap})
	m = next.(*model)
	plain = stripANSI(m.View())
	for _, want := range []string{
		"SOLMEME", "PAPER", "2m 05s", "SOL $148.20", "842 events", "live",
		"nothing here sends a transaction",
		"dex_curve_momentum@3", "champion", "+0.084 SOL", "+$12.45", "$164.50",
		"pumpportal", "4 subscribed", "12 labels", "0.62", "40%",
		"snipe BUY OTTR", "q quit",
	} {
		if !strings.Contains(plain, want) {
			t.Errorf("desk missing %q\n%s", want, plain)
		}
	}
	if strings.Contains(plain, "connection refused") {
		t.Fatal("stale error survived a good snapshot")
	}
	assertWidth(t, m)

	next, _ = m.Update(fetchedMsg{err: errors.New("timeout")})
	m = next.(*model)
	plain = stripANSI(m.View())
	if !strings.Contains(plain, "dex_curve_momentum@3") || !strings.Contains(plain, "timeout") {
		t.Fatalf("stale snapshot dropped:\n%s", plain)
	}

	next, _ = m.Update(key("2"))
	m = next.(*model)
	plain = stripANSI(m.View())
	if !strings.Contains(plain, "0.100 SOL") || !strings.Contains(plain, "+0.020 SOL (+$2.96)") {
		t.Fatalf("positions:\n%s", plain)
	}
	if strings.Contains(plain, "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi") {
		t.Fatal("full mint leaked onto positions")
	}

	next, _ = m.Update(key("3"))
	m = next.(*model)
	plain = stripANSI(m.View())
	if !strings.Contains(plain, "4vJ9…LKi") || strings.Contains(plain, "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi") {
		t.Fatalf("token mint:\n%s", plain)
	}

	next, _ = m.Update(key("4"))
	m = next.(*model)
	plain = stripANSI(m.View())
	for _, want := range []string{"AAA", "BBB", "CCC", "Migrated", "Graduated", "verdict: watch", "$12,000"} {
		if !strings.Contains(plain, want) {
			t.Errorf("boards missing %q\n%s", want, plain)
		}
	}

	next, _ = m.Update(key("5"))
	m = next.(*model)
	plain = stripANSI(m.View())
	if !strings.Contains(plain, "wallet line") || !strings.Contains(plain, "more") {
		t.Fatalf("people card lost a line:\n%s", plain)
	}

	next, _ = m.Update(tea.KeyMsg{Type: tea.KeyShiftTab})
	m = next.(*model)
	if m.view != viewBoards {
		t.Fatalf("shift-tab view = %d", m.view)
	}
	next, cmd := m.Update(key("q"))
	if cmd == nil {
		t.Fatal("q did not quit")
	}
	if _, ok := cmd().(tea.QuitMsg); !ok {
		t.Fatalf("q cmd = %#v", cmd())
	}
	_ = next

	m.view = viewDesk
	next, cmd = m.Update(tea.KeyMsg{Type: tea.KeyCtrlC})
	if _, ok := cmd().(tea.QuitMsg); !ok {
		t.Fatal("ctrl+c did not quit")
	}
	next, _ = m.Update(key("?"))
	m = next.(*model)
	if !m.help || !strings.Contains(stripANSI(m.View()), "shift-tab") {
		t.Fatal("help line did not toggle on")
	}
	next, _ = m.Update(key("?"))
	m = next.(*model)
	if m.help {
		t.Fatal("help line stayed up")
	}
}

func TestEmptyBook(t *testing.T) {
	m := newModel("http://127.0.0.1:8787/api/snapshot")
	m.width, m.height = 90, 28
	m.haveSnap = true
	m.snap = &Snapshot{UptimeS: 3}
	for _, view := range []int{viewPositions, viewTokens, viewBoards, viewPeople} {
		m.view = view
		plain := stripANSI(m.View())
		switch view {
		case viewPositions:
			if !strings.Contains(plain, "No paper buys yet.") {
				t.Fatal(plain)
			}
		case viewTokens:
			if !strings.Contains(plain, "No tokens on the desk.") {
				t.Fatal(plain)
			}
		case viewBoards:
			if !strings.Contains(plain, "No board snapshot yet.") {
				t.Fatal(plain)
			}
		case viewPeople:
			if !strings.Contains(plain, "No people cards.") {
				t.Fatal(plain)
			}
		}
	}
}

func TestFetchSnapshot(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"paper":true,"uptime_s":9,"events":3,"positions":[]}`))
	}))
	defer srv.Close()
	snap, err := fetchSnapshot(srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	if !snap.Paper || snap.Events != 3 {
		t.Fatalf("%+v", snap)
	}
	bad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "nope", http.StatusBadGateway)
	}))
	defer bad.Close()
	if _, err := fetchSnapshot(bad.URL); err == nil {
		t.Fatal("expected HTTP error")
	}
}

func TestStrategySharedWallet(t *testing.T) {
	raw := []byte(`{"paper": true, "shared_wallet": true, "strategies": [
		{"key": "fade@abc", "role": "shadow", "open": 1, "closed": 2, "pnl_sol": 0.01,
		 "cash_sol": null, "balance_sol": 0.12, "balance_usd": 15.5}
	]}`)
	var snap Snapshot
	if err := json.Unmarshal(raw, &snap); err != nil {
		t.Fatal(err)
	}
	if !snap.SharedWallet || snap.Strategies[0].CashSOL != nil {
		t.Fatalf("shared decode: %+v", snap)
	}
	plain := stripANSI(strings.Join(strategyLines(snap.Strategies, true, 120), "\n"))
	if !strings.Contains(plain, "held 0.12 $15.50") || strings.Contains(plain, "cash") || strings.Contains(plain, "bal ") {
		t.Fatalf("shared wallet row:\n%s", plain)
	}

	// Per-strategy wallets keep cash and bal; a null cash shows a dash.
	plain = stripANSI(strings.Join(strategyLines(snap.Strategies, false, 120), "\n"))
	if !strings.Contains(plain, "cash —") || !strings.Contains(plain, "bal 0.12 $15.50") || strings.Contains(plain, "held") {
		t.Fatalf("own wallet row:\n%s", plain)
	}
	snap.Strategies[0].CashSOL = floatPtr(0.9)
	if plain = stripANSI(strings.Join(strategyLines(snap.Strategies, false, 120), "\n")); !strings.Contains(plain, "cash 0.9") {
		t.Fatalf("own wallet cash:\n%s", plain)
	}

	m := newModel("http://example")
	m.width, m.height = 100, 30
	m.haveSnap, m.snap = true, &snap
	snap.Strategies[0].CashSOL = nil
	if v := stripANSI(m.View()); !strings.Contains(v, "held 0.12") || strings.Contains(v, "cash") {
		t.Fatalf("desk with shared wallet:\n%s", v)
	}
	assertWidth(t, m)
}

func TestLossColor(t *testing.T) {
	made := -2.5
	m := newModel("http://example")
	m.width, m.height = 100, 30
	m.haveSnap = true
	m.snap = &Snapshot{Strategies: []Strategy{{
		Key: "fade", Role: "shadow", PnlSOL: -0.5, MadeUSD: &made, CashSOL: floatPtr(1), BalanceSOL: 0.5,
	}}}
	plain := m.View()
	if !strings.Contains(plain, "-0.5") || !strings.Contains(plain, "-$2.50") {
		t.Fatalf("loss figures missing:\n%s", stripANSI(plain))
	}
}

func TestWalletHeader(t *testing.T) {
	raw := []byte(`{"paper": true, "sol_usd": 128.16, "wallet": {
		"strategies": 7, "best": "near_graduation@43e2d7074b", "start_sol": 0.419,
		"start_usd": 50.0, "balance_sol": 0.45, "balance_usd": 53.7, "pnl_sol": 0.031,
		"pnl_usd": 3.7, "pnl_pct": 7.4, "goal_usd": 100.0, "goal_pct": 53.7, "reached": []
	}}`)
	var snap Snapshot
	if err := json.Unmarshal(raw, &snap); err != nil {
		t.Fatal(err)
	}
	w := snap.Wallet
	if w == nil || w.Strategies != 7 || w.PnlUSD == nil || w.Best != "near_graduation@43e2d7074b" ||
		w.GoalUSD == nil || *w.GoalUSD != 100 || w.GoalPct == nil || w.Reached == nil {
		t.Fatalf("wallet decode: %+v", w)
	}
	m := newModel("http://example")
	m.haveSnap = true
	m.snap = &snap
	m.width, m.height = 200, 30
	full := "Wallet $50.00 → $53.70 +$3.70 (+7.40%) near_graduation · Goal $100 54%"
	head := m.headerLines(m.width)
	if len(head) != 1 || !strings.Contains(stripANSI(head[0]), full) {
		t.Fatalf("wide header:\n%s", stripANSI(strings.Join(head, "\n")))
	}
	if !strings.Contains(head[0], stUp.Render("+$3.70 (+7.40%)")) {
		t.Fatal("gain should use the up style")
	}
	if !strings.Contains(head[0], stMuted.Render("near_graduation")) {
		t.Fatal("best strategy should be muted")
	}
	if !strings.Contains(head[0], stText.Render("54%")) {
		t.Fatal("goal progress should use the text style")
	}
	assertWidth(t, m)

	// Narrowing drops the best name, then the start figure, then the goal.
	want := []string{
		full,
		"Wallet $50.00 → $53.70 +$3.70 (+7.40%) · Goal $100 54%",
		"Wallet $53.70 +$3.70 (+7.40%) · Goal $100 54%",
		"Wallet $53.70 +$3.70 (+7.40%)",
		"Wallet +$3.70 (+7.40%)",
		"+$3.70 (+7.40%)",
	}
	got := walletSegments(snap.Wallet)
	if len(got) != len(want) {
		t.Fatalf("variants: %q", got)
	}
	for i := range want {
		if stripANSI(got[i]) != want[i] {
			t.Errorf("variant %d = %q, want %q", i, stripANSI(got[i]), want[i])
		}
	}
	for _, w := range []int{120, 100, 60, 45, 40, 24, 12} {
		m.width = w
		plain := stripANSI(strings.Join(m.headerLines(w), "\n"))
		if !strings.Contains(plain, "+$3.70") && w >= 24 {
			t.Errorf("width %d lost the gain:\n%s", w, plain)
		}
		assertWidth(t, m)
	}
	m.width = 50
	if plain := stripANSI(strings.Join(m.headerLines(50), "\n")); !strings.Contains(plain, "Wallet $53.70 +$3.70 (+7.40%) · Goal $100 54%") {
		t.Errorf("mid width should keep the goal and drop the start figure:\n%s", plain)
	}

	// Reached: any strategy at the goal shows "reached" in the up style.
	reached := *snap.Wallet
	reached.BalanceUSD, reached.PnlUSD, reached.PnlPct = floatPtr(104.2), floatPtr(54.2), floatPtr(108.4)
	reached.GoalPct = floatPtr(104.2)
	reached.Reached = []string{"near_graduation@43e2d7074b"}
	snap.Wallet = &reached
	m.width = 200
	head = m.headerLines(200)
	if !strings.Contains(stripANSI(head[0]), "near_graduation · Goal $100 reached") {
		t.Fatalf("reached header:\n%s", stripANSI(head[0]))
	}
	if !strings.Contains(head[0], stUp.Render("reached")) || strings.Contains(stripANSI(head[0]), "104%") {
		t.Fatal("reached should replace the percentage in the up style")
	}

	// No goal: the goal part is omitted.
	noGoal := reached
	noGoal.GoalUSD, noGoal.GoalPct, noGoal.Reached = nil, nil, nil
	snap.Wallet = &noGoal
	if plain := stripANSI(m.headerLines(200)[0]); strings.Contains(plain, "Goal") ||
		!strings.Contains(plain, "+$54.20 (+108.40%) near_graduation") {
		t.Fatalf("no goal:\n%s", plain)
	}

	// Loss, no SOL price: SOL figures in the down style.
	snap.Wallet = &Wallet{
		Strategies: 7, Best: "fade@abc", StartSOL: 0.419, BalanceSOL: 0.4, PnlSOL: -0.019,
		PnlPct: floatPtr(-4.53), GoalUSD: floatPtr(100),
	}
	head = m.headerLines(200)
	if !strings.Contains(stripANSI(head[0]), "Wallet 0.419 SOL → 0.4 SOL -0.019 SOL (-4.53%) fade · Goal $100") {
		t.Fatalf("SOL fallback:\n%s", stripANSI(head[0]))
	}
	if !strings.Contains(head[0], stDown.Render("-0.019 SOL (-4.53%)")) {
		t.Fatal("loss should use the down style")
	}

	snap.Wallet = nil
	if plain := stripANSI(m.headerLines(160)[0]); !strings.Contains(plain, "Wallet —") {
		t.Fatalf("nil wallet:\n%s", plain)
	}
	m.haveSnap, m.snap = false, nil
	if plain := stripANSI(strings.Join(m.headerLines(160), "\n")); strings.Contains(plain, "Wallet") {
		t.Fatalf("wallet before first snapshot:\n%s", plain)
	}
}

func TestWalletRange(t *testing.T) {
	raw := []byte(`{"paper": true, "sol_usd": 128.16, "wallet": {
		"strategies": 7, "best": "near_graduation@43e2d7074b", "start_sol": 0.419,
		"start_usd": 50.0, "balance_sol": 0.45, "balance_usd": 53.7, "pnl_sol": 0.031,
		"pnl_usd": 3.7, "pnl_pct": 7.4, "goal_usd": 100.0, "goal_pct": 53.7, "reached": [],
		"range": {"unit": "usd", "high": 58.2, "high_ts": 1759370000.5, "low": 47.9,
			"low_ts": null, "from_high_pct": -7.7}
	}}`)
	var snap Snapshot
	if err := json.Unmarshal(raw, &snap); err != nil {
		t.Fatal(err)
	}
	r := snap.Wallet.Range
	if r == nil || r.Unit != "usd" || r.High != 58.2 || r.HighTS == nil || r.LowTS != nil ||
		r.FromHighPct == nil || *r.FromHighPct != -7.7 {
		t.Fatalf("range decode: %+v", r)
	}
	m := newModel("http://example")
	m.haveSnap = true
	m.snap = &snap
	m.width, m.height = 200, 30
	full := "Wallet $50.00 → $53.70 +$3.70 (+7.40%) near_graduation · High $58.20 −7.7% · Low $47.90 · Goal $100 54%"
	head := m.headerLines(m.width)
	if !strings.Contains(stripANSI(strings.Join(head, "\n")), full) {
		t.Fatalf("wide header:\n%s", stripANSI(strings.Join(head, "\n")))
	}
	if !strings.Contains(head[len(head)-1], stDown.Render("−7.7%")) && !strings.Contains(head[0], stDown.Render("−7.7%")) {
		t.Fatal("drawdown should use the down style")
	}
	if !strings.Contains(strings.Join(head, ""), stMuted.Render("Low $47.90")) {
		t.Fatal("low should be muted")
	}
	assertWidth(t, m)

	// Narrowing drops the low, then the high, then the usual order.
	want := []string{
		full,
		"Wallet $50.00 → $53.70 +$3.70 (+7.40%) near_graduation · High $58.20 −7.7% · Goal $100 54%",
		"Wallet $50.00 → $53.70 +$3.70 (+7.40%) near_graduation · Goal $100 54%",
		"Wallet $50.00 → $53.70 +$3.70 (+7.40%) · Goal $100 54%",
		"Wallet $53.70 +$3.70 (+7.40%) · Goal $100 54%",
		"Wallet $53.70 +$3.70 (+7.40%)",
		"Wallet +$3.70 (+7.40%)",
		"+$3.70 (+7.40%)",
	}
	assertVariants(t, walletSegments(snap.Wallet), want)
	for _, w := range []int{200, 160, 120, 100, 80, 60, 45, 24, 12} {
		m.width = w
		plain := stripANSI(strings.Join(m.headerLines(w), "\n"))
		if !strings.Contains(plain, "+$3.70") && w >= 24 {
			t.Errorf("width %d lost the gain:\n%s", w, plain)
		}
		assertWidth(t, m)
	}

	// At the high: "at high" in the up style.
	at := *snap.Wallet
	at.Range = &WalletRange{Unit: "usd", High: 53.7, Low: 47.9, FromHighPct: floatPtr(0)}
	got := walletSegments(&at)
	if stripANSI(got[1]) != "Wallet $50.00 → $53.70 +$3.70 (+7.40%) near_graduation · High $53.70 at high · Goal $100 54%" {
		t.Fatalf("at high: %q", stripANSI(got[1]))
	}
	if !strings.Contains(got[1], stUp.Render("at high")) {
		t.Fatal("at high should use the up style")
	}

	// No drawdown figure: the high alone.
	at.Range = &WalletRange{Unit: "usd", High: 53.7, Low: 47.9}
	if plain := stripANSI(walletSegments(&at)[1]); !strings.Contains(plain, "· High $53.70 · Goal") {
		t.Fatalf("nil from_high_pct: %q", plain)
	}

	// SOL unit when there is no SOL price.
	snap.Wallet = &Wallet{
		Strategies: 7, Best: "fade@abc", StartSOL: 0.419, BalanceSOL: 0.4, PnlSOL: -0.019,
		PnlPct: floatPtr(-4.53),
		Range:  &WalletRange{Unit: "sol", High: 0.455, Low: 0.39, FromHighPct: floatPtr(-12.09)},
	}
	if plain := stripANSI(walletSegments(snap.Wallet)[0]); plain != "Wallet 0.419 SOL → 0.4 SOL -0.019 SOL (-4.53%) fade · High 0.455 SOL −12.1% · Low 0.39 SOL" {
		t.Fatalf("SOL range: %q", plain)
	}
}

// One shared wallet: no best strategy, so the name part is skipped and its
// variants collapse.
func TestWalletShared(t *testing.T) {
	raw := []byte(`{"wallet": {
		"strategies": 7, "best": null, "start_sol": 0.419,
		"start_usd": 50.0, "balance_sol": 0.45, "balance_usd": 53.7, "pnl_sol": 0.031,
		"pnl_usd": 3.7, "pnl_pct": 7.4, "goal_usd": 100.0, "goal_pct": 53.7, "reached": [],
		"range": {"unit": "usd", "high": 58.2, "high_ts": null, "low": 47.9,
			"low_ts": null, "from_high_pct": -7.7}
	}}`)
	var snap Snapshot
	if err := json.Unmarshal(raw, &snap); err != nil {
		t.Fatal(err)
	}
	if snap.Wallet.Best != "" {
		t.Fatalf("best = %q", snap.Wallet.Best)
	}
	assertVariants(t, walletSegments(snap.Wallet), []string{
		"Wallet $50.00 → $53.70 +$3.70 (+7.40%) · High $58.20 −7.7% · Low $47.90 · Goal $100 54%",
		"Wallet $50.00 → $53.70 +$3.70 (+7.40%) · High $58.20 −7.7% · Goal $100 54%",
		"Wallet $50.00 → $53.70 +$3.70 (+7.40%) · Goal $100 54%",
		"Wallet $53.70 +$3.70 (+7.40%) · Goal $100 54%",
		"Wallet $53.70 +$3.70 (+7.40%)",
		"Wallet +$3.70 (+7.40%)",
		"+$3.70 (+7.40%)",
	})
	noGoal := *snap.Wallet
	noGoal.GoalUSD, noGoal.GoalPct, noGoal.Range = nil, nil, nil
	assertVariants(t, walletSegments(&noGoal), []string{
		"Wallet $50.00 → $53.70 +$3.70 (+7.40%)",
		"Wallet $53.70 +$3.70 (+7.40%)",
		"Wallet +$3.70 (+7.40%)",
		"+$3.70 (+7.40%)",
	})
}

func TestDrawdownFormat(t *testing.T) {
	for _, c := range []struct {
		v    float64
		want string
	}{{0, ""}, {-7.7, "−7.7%"}, {-12.06, "−12.1%"}, {-0.04, "−0.04%"}, {-0.001, ""}, {1, ""}} {
		if got := formatDrawdown(c.v); got != c.want {
			t.Errorf("formatDrawdown(%v) = %q, want %q", c.v, got, c.want)
		}
	}
}

func assertVariants(t *testing.T, got, want []string) {
	t.Helper()
	if len(got) != len(want) {
		plain := make([]string, len(got))
		for i, g := range got {
			plain[i] = stripANSI(g)
		}
		t.Fatalf("variants: %q", plain)
	}
	for i := range want {
		if stripANSI(got[i]) != want[i] {
			t.Errorf("variant %d = %q, want %q", i, stripANSI(got[i]), want[i])
		}
	}
}

func TestGoalFormat(t *testing.T) {
	for _, c := range []struct {
		v    float64
		want string
	}{{100, "$100"}, {1000, "$1,000"}, {99.5, "$99.50"}} {
		if got := formatGoalUSD(c.v); got != c.want {
			t.Errorf("formatGoalUSD(%v) = %q, want %q", c.v, got, c.want)
		}
	}
	for _, c := range []struct {
		v    float64
		want string
	}{{53.7, "54%"}, {99.6, "99%"}, {100, "100%"}, {130.2, "130%"}} {
		if got := formatGoalPct(floatPtr(c.v)); got != c.want {
			t.Errorf("formatGoalPct(%v) = %q, want %q", c.v, got, c.want)
		}
	}
	if formatGoalPct(nil) != "" {
		t.Error("nil goal pct should be empty")
	}
	if shortStrategy("near_graduation@43e2d7074b") != "near_graduation" || shortStrategy("plain") != "plain" {
		t.Error("shortStrategy")
	}
}

func TestFormatUSDCents(t *testing.T) {
	cases := []struct {
		v      float64
		signed bool
		want   string
	}{
		{8304.1, false, "$8,304.10"},
		{26.019, true, "+$26.02"},
		{-1234567.891, true, "-$1,234,567.89"},
		{-0.001, true, "$0.00"},
		{0, true, "$0.00"},
	}
	for _, c := range cases {
		if got := formatUSDCents(c.v, c.signed); got != c.want {
			t.Errorf("formatUSDCents(%v, %v) = %q, want %q", c.v, c.signed, got, c.want)
		}
	}
}

func sampleModel(t *testing.T) *model {
	t.Helper()
	m := newModel("http://127.0.0.1:8787/api/snapshot")
	m.width, m.height = 100, 30
	m.view = viewPositions
	return m
}

func key(s string) tea.KeyMsg {
	return tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(s)}
}

func floatPtr(v float64) *float64 { return &v }

func assertWidth(t *testing.T, m *model) {
	t.Helper()
	for _, line := range strings.Split(m.View(), "\n") {
		if lipgloss.Width(line) > m.width {
			t.Errorf("line width %d > %d: %q", lipgloss.Width(line), m.width, stripANSI(line))
		}
	}
}

func stripANSI(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); {
		if s[i] == 0x1b {
			j := i + 1
			if j < len(s) && s[j] == '[' {
				j++
				for j < len(s) {
					c := s[j]
					j++
					if (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') {
						break
					}
				}
			}
			i = j
			continue
		}
		b.WriteByte(s[i])
		i++
	}
	return b.String()
}

func boardsPlain(b *Boards, width int) []string {
	lines := boardLines(b, width)
	out := make([]string, 0, len(lines))
	for _, ln := range lines {
		out = append(out, strings.Split(stripANSI(ln), "\n")...)
	}
	return out
}

func assertBoardWidth(t *testing.T, b *Boards, width int) {
	t.Helper()
	for _, ln := range boardLines(b, width) {
		for _, part := range strings.Split(ln, "\n") {
			if w := lipgloss.Width(part); w > width {
				t.Errorf("boards line width %d > %d: %q", w, width, stripANSI(part))
			}
		}
	}
}

func TestBoardsPumpAsTrenches(t *testing.T) {
	raw := []byte(`{
		"trenches": {"new_creation": [], "near_completion": [], "completed": []},
		"trenches_source": "pump.fun",
		"trending": [{"symbol": "VOLX", "mcap_usd": 250000, "volume_usd": 81000, "holders": null, "mint": "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"}],
		"trending_source": "dexscreener",
		"pump": {
			"new": [{"symbol": "NEWB", "mcap_usd": 5200, "bonding_pct": 12, "mint": "m1"}],
			"almost": [{"symbol": "ALMO", "mcap_usd": 41000, "bonding_pct": 88, "mint": "m2"}],
			"graduated": [{"symbol": "GRAD", "mcap_usd": 90000, "bonding_pct": 100, "mint": "m3"}]
		},
		"cards": []
	}`)
	var b Boards
	if err := json.Unmarshal(raw, &b); err != nil {
		t.Fatal(err)
	}
	if b.TrenchesSource != "pump.fun" || b.TrendingSource != "dexscreener" || b.Trending[0].Holders != nil {
		t.Fatalf("decode: %+v", b)
	}
	plain := strings.Join(boardsPlain(&b, 100), "\n")
	for _, want := range []string{"TRENCHES  via pump.fun", "NEWB", "ALMO", "GRAD", "Graduated", "curve", "TRENDING  1h volume · DexScreener", "VOLX"} {
		if !strings.Contains(plain, want) {
			t.Errorf("missing %q\n%s", want, plain)
		}
	}
	if strings.Contains(plain, "PUMP.FUN") {
		t.Errorf("pump.fun section duplicated:\n%s", plain)
	}
	if strings.Count(plain, "NEWB") != 1 {
		t.Errorf("pump rows rendered twice:\n%s", plain)
	}
	if strings.Contains(plain, "holders") || strings.Contains(plain, "Migrated") {
		t.Errorf("null holders or GMGN lanes shown:\n%s", plain)
	}
	for _, w := range []int{100, 60, 40, 24} {
		assertBoardWidth(t, &b, w)
	}

	m := sampleModel(t)
	m.width, m.height = 44, 80
	m.snap = &Snapshot{Paper: true, Boards: &b}
	m.haveSnap = true
	next, _ := m.Update(key("4"))
	m = next.(*model)
	assertWidth(t, m)
	if !strings.Contains(stripANSI(m.View()), "via pump.fun") {
		t.Errorf("narrow boards view lost source note:\n%s", stripANSI(m.View()))
	}
}

func TestBoardsGMGNAndEmpty(t *testing.T) {
	b := &Boards{
		TrenchesSource: "gmgn",
		Trenches: &Trenches{Completed: []TrenchRow{{
			Symbol: "MIGR", McapUSD: floatPtr(70000), Holders: floatPtr(1500), Mint: "m4",
		}}},
		TrendingSource: "gmgn",
		Trending:       []TrendingRow{{Symbol: "HOTT", McapUSD: floatPtr(300000), VolumeUSD: floatPtr(50000), Holders: floatPtr(2400), Mint: "m5"}},
		Pump:           &Pump{New: []PumpRow{{Symbol: "PNEW", McapUSD: floatPtr(4000), BondingPct: floatPtr(5), Mint: "m6"}}},
	}
	plain := strings.Join(boardsPlain(b, 100), "\n")
	for _, want := range []string{"TRENCHES  via GMGN", "Migrated", "MIGR", "1,500 holders", "TRENDING  via GMGN", "2,400 holders", "PUMP.FUN", "PNEW"} {
		if !strings.Contains(plain, want) {
			t.Errorf("gmgn mode missing %q\n%s", want, plain)
		}
	}
	assertBoardWidth(t, b, 100)
	assertBoardWidth(t, b, 30)

	empty := &Boards{Trenches: &Trenches{}, Pump: &Pump{}}
	plain = strings.Join(boardsPlain(empty, 80), "\n")
	if !strings.Contains(plain, "needs GMGN_API_KEY or pump.fun data") {
		t.Errorf("empty trenches hint missing:\n%s", plain)
	}
	if strings.Contains(plain, "Migrated") || strings.Contains(plain, "None") {
		t.Errorf("empty trenches should show only the hint:\n%s", plain)
	}
	if !strings.Contains(plain, "PUMP.FUN") {
		t.Errorf("pump section missing without a trenches source:\n%s", plain)
	}
	assertBoardWidth(t, empty, 20)
}
