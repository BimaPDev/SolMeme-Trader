package main

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/lipgloss"
)

func (m *model) View() string {
	w, h := m.width, m.height
	if w < 1 || h < 1 {
		return "SOLMEME · PAPER · waiting"
	}
	lines := m.compose(w, h)
	if len(lines) > h {
		lines = lines[:h]
	}
	for len(lines) < h {
		lines = append(lines, "")
	}
	for i, ln := range lines {
		lines[i] = padLine(ln, w)
	}
	return strings.Join(lines, "\n")
}

func (m *model) compose(w, h int) []string {
	top, bodyH, feedH, footer := m.measure(w, h)
	var mid []string
	if bodyH > 0 {
		mid = window(m.scrollLines(w), m.scroll, bodyH)
	}
	if feedH > 0 {
		mid = append(mid, m.feedBlock(w, feedH)...)
	}
	out := append(append(append([]string{}, top...), mid...), footer...)
	if len(out) > h {
		return out[:h]
	}
	return out
}

func (m *model) measure(w, h int) (top []string, bodyH, feedH int, footer []string) {
	top = m.chromeTop(w)
	feedH = m.feedHeight(w, h)
	room := h - len(top) - 2
	if room < 1 {
		bodyH = 0
		feedH = 0
	} else if feedH > 0 {
		bodyH = room - feedH
	} else {
		bodyH = room
	}
	footer = []string{rule(w), m.footerLine(w, m.scrollMark(bodyH))}
	return top, bodyH, feedH, footer
}

func (m *model) chromeTop(w int) []string {
	top := m.headerLines(w)
	if m.help {
		top = append(top, m.helpLine(w))
	}
	return append(top, rule(w))
}

func (m *model) feedHeight(w, h int) int {
	if m.view != viewDesk || !m.haveSnap || m.snap == nil || h <= 0 {
		return 0
	}
	room := h - len(m.chromeTop(w)) - 2
	if room < 8 {
		return 0
	}
	want := naturalFeed(*m.snap)
	maxFeed := room / 4
	if maxFeed < 3 {
		maxFeed = 3
	}
	if maxFeed > 8 {
		maxFeed = 8
	}
	if want > maxFeed {
		want = maxFeed
	}
	if want > room-3 {
		want = room - 3
	}
	if want < 1 {
		return 0
	}
	return want
}

func (m *model) headerLines(w int) []string {
	up := "0s"
	sol := "—"
	events := "0 events"
	conn := "waiting"
	connSt := stGold
	if m.haveSnap && m.snap != nil {
		up = formatUptime(m.snap.UptimeS)
		sol = formatSolPrice(m.snap.SolUSD)
		events = formatEvents(m.snap.Events) + " events"
		if m.errText == "" {
			conn = "live"
			connSt = stUp
		} else {
			conn = "stale"
			connSt = stDown
		}
	}
	dot := stMuted.Render(" · ")
	left := strings.Join([]string{
		stGoldBold.Render("SOLMEME"),
		stGold.Render("PAPER"),
		stText.Render(up),
		stText.Render("SOL " + sol),
		stMuted.Render(events),
		connSt.Render(conn),
	}, dot)
	note := stMuted.Render("nothing here sends a transaction")
	lines := []string{left}
	if m.haveSnap && m.snap != nil {
		variants := walletSegments(m.snap.Wallet)
		if lipgloss.Width(left)+lipgloss.Width(dot)+lipgloss.Width(variants[0]) <= w {
			lines[0] = left + dot + variants[0]
		} else {
			lines = append(lines, pickFit(variants, w))
		}
	}
	last := lines[len(lines)-1]
	if lipgloss.Width(last)+2+lipgloss.Width(note) <= w {
		gap := w - lipgloss.Width(last) - lipgloss.Width(note)
		lines[len(lines)-1] = last + stBlank.Render(strings.Repeat(" ", gap)) + note
	} else {
		lines = append(lines, note)
	}
	if m.errText != "" {
		lines = append(lines, stDown.Render(fit("error · "+cleanErr(m.errText), w)))
	}
	return lines
}

// walletSegments renders the wallet readout from widest to narrowest. The
// session low goes first, then the high, then the best strategy's name (when
// there is one), then the start figure, then the goal; the coloured P&L stays
// to the end.
func walletSegments(wal *Wallet) []string {
	label := stMuted.Render("Wallet ")
	if wal == nil {
		return []string{label + stMuted.Render("—")}
	}
	start, now, gain, pct, sign := walletFigures(*wal)
	delta := gain
	if pct != "" {
		delta += " (" + pct + ")"
	}
	d := pnlStyle(sign).Render(delta)
	sp := stBlank.Render(" ")
	dot := stMuted.Render(" · ")
	full := label + stText.Render(start) + stMuted.Render(" → ") + stText.Render(now) + sp + d
	short := label + stText.Render(now) + sp + d
	best := ""
	if name := shortStrategy(wal.Best); name != "" {
		best = sp + stMuted.Render(name)
	}
	high, low := walletRange(wal.Range)
	if high != "" {
		high = dot + high
		low = dot + low
	}
	goal := walletGoal(*wal)
	if goal == "" {
		return dedupe([]string{
			full + best + high + low,
			full + best + high,
			full + best,
			full,
			short + best,
			short,
			label + d,
			d,
		})
	}
	goal = dot + goal
	return dedupe([]string{
		full + best + high + low + goal,
		full + best + high + goal,
		full + best + goal,
		full + goal,
		short + goal,
		short,
		label + d,
		d,
	})
}

// walletRange renders "High $58.20 −7.7%" (or "High $58.20 at high") and
// "Low $47.90" for the session range. Both are empty without a range.
func walletRange(r *WalletRange) (high, low string) {
	if r == nil {
		return "", ""
	}
	amount := func(v float64) string {
		if r.Unit == "usd" {
			return formatUSDCents(v, false)
		}
		return formatSOL(v) + " SOL"
	}
	high = stMuted.Render("High ") + stText.Render(amount(r.High))
	if r.FromHighPct != nil {
		if dd := formatDrawdown(*r.FromHighPct); dd == "" {
			high += stBlank.Render(" ") + stUp.Render("at high")
		} else {
			high += stBlank.Render(" ") + stDown.Render(dd)
		}
	}
	return high, stMuted.Render("Low " + amount(r.Low))
}

// walletGoal renders "Goal $100 54%", or "Goal $100 reached" once any
// strategy's wallet has reached it. It is empty without a goal.
func walletGoal(w Wallet) string {
	if w.GoalUSD == nil {
		return ""
	}
	text := stMuted.Render("Goal ") + stText.Render(formatGoalUSD(*w.GoalUSD))
	if len(w.Reached) > 0 {
		return text + stBlank.Render(" ") + stUp.Render("reached")
	}
	if p := formatGoalPct(w.GoalPct); p != "" {
		text += stBlank.Render(" ") + stText.Render(p)
	}
	return text
}

// shortStrategy drops the version hash: "near_graduation@43e2" -> "near_graduation".
func shortStrategy(key string) string {
	if i := strings.Index(key, "@"); i >= 0 {
		key = key[:i]
	}
	return strings.TrimSpace(key)
}

func dedupe(variants []string) []string {
	out := variants[:0]
	for i, v := range variants {
		if i > 0 && v == out[len(out)-1] {
			continue
		}
		out = append(out, v)
	}
	return out
}

// pickFit returns the first variant that fits w, else the narrowest.
func pickFit(variants []string, w int) string {
	for _, v := range variants {
		if lipgloss.Width(v) <= w {
			return v
		}
	}
	return variants[len(variants)-1]
}

func (m *model) helpLine(w int) string {
	text := "1-5 views   tab shift-tab left right   j k up down scroll   ? hide   q quit"
	return stMuted.Render(fit(text, w))
}

func (m *model) footerLine(w int, mark string) string {
	var parts []string
	for i, name := range viewNames {
		label := fmt.Sprintf("%d %s", i+1, name)
		if i == m.view {
			parts = append(parts, stGoldBold.Render(label))
		} else {
			parts = append(parts, stMuted.Render(label))
		}
	}
	left := strings.Join(parts, stMuted.Render("  "))
	right := stMuted.Render("q quit")
	if mark != "" {
		right = stMuted.Render(mark+"  ") + right
	}
	gap := w - lipgloss.Width(left) - lipgloss.Width(right)
	if gap < 1 {
		return padLine(left+" "+right, w)
	}
	return left + stBlank.Render(strings.Repeat(" ", gap)) + right
}

func (m *model) scrollMark(bodyH int) string {
	if m.width <= 0 || bodyH <= 0 {
		return ""
	}
	n := len(m.scrollLines(m.width))
	below := m.scroll+bodyH < n
	switch {
	case m.scroll > 0 && below:
		return "↑↓"
	case m.scroll > 0:
		return "↑"
	case below:
		return "↓"
	default:
		return ""
	}
}

func (m *model) scrollLines(w int) []string {
	if !m.haveSnap || m.snap == nil {
		return []string{stText.Render(fit("Waiting for a snapshot from "+m.url, w))}
	}
	switch m.view {
	case viewDesk:
		lines := deskLines(*m.snap, w)
		if m.feedHeight(w, m.height) == 0 {
			lines = append(lines, "")
			lines = append(lines, m.feedBlock(w, naturalFeed(*m.snap))...)
		}
		return lines
	case viewPositions:
		return positionLines(m.snap.Positions, w)
	case viewTokens:
		return tokenLines(m.snap.Tokens, w)
	case viewBoards:
		return boardLines(m.snap.Boards, w)
	case viewPeople:
		return peopleLines(m.snap.People, m.snap.PeopleNote, w)
	default:
		return nil
	}
}

func deskLines(s Snapshot, w int) []string {
	leftW := w
	rightW := 0
	side := false
	if w >= 88 {
		rightW = w / 3
		if rightW < 28 {
			rightW = 28
		}
		if rightW > 36 {
			rightW = 36
		}
		leftW = w - rightW - 3
		side = leftW >= 36
	}
	left := append([]string{stGold.Render("STRATEGIES")}, strategyLines(s.Strategies, s.SharedWallet, leftW)...)
	srcW := w
	if side {
		srcW = rightW
	}
	right := append([]string{stGold.Render("SOURCES")}, sourceLines(s.Sources, srcW)...)
	var block []string
	if side {
		block = joinCols(left, right, leftW, rightW)
	} else {
		block = append(left, "")
		block = append(block, right...)
	}
	if line := streamsLine(s.TradeStreams); line != "" {
		block = append(block, stMuted.Render(fit(line, w)))
	}
	if line := sniperLine(s.Sniper); line != "" {
		block = append(block, stMuted.Render(fit(line, w)))
	}
	if note := strings.TrimSpace(s.ScanNote); note != "" {
		block = append(block, stMuted.Render(fit("scan · "+note, w)))
	}
	return block
}

// strategyLines renders one block per strategy. With a shared wallet there is
// no per-strategy cash, and the balance is the value of that strategy's open
// positions, shown as "held".
func strategyLines(rows []Strategy, shared bool, width int) []string {
	if len(rows) == 0 {
		return []string{stMuted.Render("No strategies.")}
	}
	var out []string
	for _, row := range rows {
		out = append(out, strategyBlock(row, shared, width)...)
	}
	return out
}

func strategyBlock(s Strategy, shared bool, width int) []string {
	if width < 8 {
		width = 8
	}
	key := s.Key
	if key == "" {
		key = "—"
	}
	role := s.Role
	if role == "" {
		role = "—"
	}
	roleSt := stMuted
	if strings.EqualFold(role, "champion") {
		roleSt = stGold
	}
	roleRendered := roleSt.Render(role)
	keyBudget := width - lipgloss.Width(roleRendered) - 2
	var title string
	if keyBudget >= 4 {
		title = stTextBold.Render(fit(key, keyBudget)) + "  " + roleRendered
	} else {
		title = stTextBold.Render(fit(key+"  "+role, width))
	}
	made := "—"
	madeSt := stMuted
	if s.MadeUSD != nil {
		made = formatUSD(*s.MadeUSD, true)
		madeSt = pnlStyle(*s.MadeUSD)
	}
	segs := []seg{
		{fmt.Sprintf("%d open", s.Open), stText},
		{fmt.Sprintf("%d closed", s.Closed), stText},
		{formatPct(s.WinPct), stText},
		{formatSignedSOL(s.PnlSOL) + " SOL", pnlStyle(s.PnlSOL)},
		{made, madeSt},
	}
	if !shared {
		cash := "cash —"
		if s.CashSOL != nil {
			cash = "cash " + formatSOL(*s.CashSOL)
		}
		segs = append(segs, seg{cash, stMuted})
	}
	segs = append(segs, seg{balanceLabel(s, shared), stText})
	return append([]string{title}, wrapSegs(segs, width)...)
}

func balanceLabel(s Strategy, shared bool) string {
	label := "bal "
	if shared {
		label = "held "
	}
	label += formatSOL(s.BalanceSOL)
	if s.BalanceUSD != nil {
		label += " " + formatUSD(*s.BalanceUSD, false)
	}
	return label
}

func sourceLines(rows []Source, width int) []string {
	if width < 8 {
		width = 8
	}
	if len(rows) == 0 {
		return []string{stMuted.Render("No sources.")}
	}
	var out []string
	for _, src := range rows {
		name := src.Name
		if name == "" {
			name = "—"
		}
		status := src.Status
		if status == "" {
			status = "—"
		}
		segs := []seg{
			{name, stText},
			{status, statusStyle(status)},
			{fmt.Sprintf("%d ev", src.Events), stMuted},
			{fmt.Sprintf("%d err", src.Errors), errStyle(src.Errors)},
		}
		out = append(out, wrapSegs(segs, width)...)
		if note := strings.TrimSpace(src.Note); note != "" {
			out = append(out, stMuted.Render(fit(note, width)))
		}
	}
	return out
}

func errStyle(n int) lipgloss.Style {
	if n > 0 {
		return stDown
	}
	return stMuted
}

func streamsLine(ts *TradeStreams) string {
	if ts == nil {
		return ""
	}
	parts := []string{"streams"}
	if ts.Unavailable {
		parts = append(parts, "unavailable")
	}
	parts = append(parts, fmt.Sprintf("%d subscribed", ts.Subscribed))
	parts = append(parts, fmt.Sprintf("%d events today", ts.EventsToday))
	if ts.BudgetExhausted {
		parts = append(parts, "budget exhausted")
	}
	return strings.Join(parts, " · ")
}

func sniperLine(sn *Sniper) string {
	if sn == nil {
		return ""
	}
	return fmt.Sprintf("sniper · %d labels · threshold %s · buy win %s",
		sn.Labels, formatThreshold(sn.Threshold), formatWinRate(sn.BuyWinRate))
}

func positionLines(rows []Position, width int) []string {
	out := []string{stGold.Render("POSITIONS")}
	if len(rows) == 0 {
		return append(out, stMuted.Render("No paper buys yet."))
	}
	for i, row := range rows {
		if i > 0 {
			out = append(out, "")
		}
		coin := dash(row.Coin)
		strategy := dash(row.Strategy)
		status := dash(row.Status)
		head := []seg{{coin, stTextBold}, {strategy, stMuted}, {status, stText}}
		out = append(out, wrapSegs(head, width)...)
		detail := []seg{
			{"bought " + dash(row.Bought), stMuted},
			{"entry " + dash(row.Entry), stMuted},
			{"sold " + dash(row.Sold), stMuted},
			{"mark " + dash(row.Mark), stMuted},
			{dash(row.Made), madeStyle(strings.TrimSpace(row.Made))},
		}
		out = append(out, wrapSegs(detail, width)...)
	}
	return out
}

func tokenLines(rows []Token, width int) []string {
	out := []string{stGold.Render("TOKENS")}
	if len(rows) == 0 {
		return append(out, stMuted.Render("No tokens on the desk."))
	}
	symW, phaseW, ageW, mintW := 10, 10, 6, 12
	fixed := symW + phaseW + ageW + mintW + 4
	for _, tok := range rows {
		sym := tok.Symbol
		if sym == "" {
			sym = shortMint(tok.Mint, tok.MintShort)
		}
		if sym == "" {
			sym = "—"
		}
		phase := dash(tok.Phase)
		age := dash(tok.Age)
		mint := shortMint(tok.Mint, tok.MintShort)
		if mint == "" {
			mint = "—"
		}
		detail := tok.Detail
		if tok.Source != "" {
			if detail != "" {
				detail += "  "
			}
			detail += tok.Source
		}
		if width < fixed+8 {
			segs := []seg{
				{sym, stTextBold},
				{phase, phaseStyle(tok.Phase)},
				{age, stMuted},
				{mint, stMuted},
			}
			if detail != "" {
				segs = append(segs, seg{detail, stText})
			}
			out = append(out, wrapSegs(segs, width)...)
			continue
		}
		rest := width - fixed
		line := stTextBold.Render(pad(sym, symW)) + " " +
			phaseStyle(tok.Phase).Render(pad(phase, phaseW)) + " " +
			stMuted.Render(pad(age, ageW)) + " " +
			stMuted.Render(pad(mint, mintW)) + " " +
			stText.Render(fit(detail, rest))
		out = append(out, line)
	}
	return out
}

func boardLines(b *Boards, width int) []string {
	out := []string{stGold.Render("BOARDS")}
	if b == nil {
		return append(out, stMuted.Render("No board snapshot yet."))
	}
	var trenches Trenches
	if b.Trenches != nil {
		trenches = *b.Trenches
	}
	var pump Pump
	if b.Pump != nil {
		pump = *b.Pump
	}
	colW, _ := columnWidth(width)
	pumpAsTrenches := b.TrenchesSource == "pump.fun"
	switch {
	case pumpAsTrenches:
		out = append(out, headingNote("TRENCHES", "via pump.fun", width))
		out = append(out, pumpColumns(pump, colW, width)...)
	case b.TrenchesSource == "" && trenchesEmpty(trenches):
		out = append(out, stGold.Render("TRENCHES"))
		out = append(out, stMuted.Render(fit("needs GMGN_API_KEY or pump.fun data", width)))
	default:
		note := ""
		if b.TrenchesSource == "gmgn" {
			note = "via GMGN"
		}
		out = append(out, headingNote("TRENCHES", note, width))
		out = append(out, threeColumns([3]colBlock{
			{title: "New", lines: trenchRows(trenches.NewCreation, colW)},
			{title: "Almost", lines: trenchRows(trenches.NearCompletion, colW)},
			{title: "Migrated", lines: trenchRows(trenches.Completed, colW)},
		}, width)...)
	}
	out = append(out, "")
	out = append(out, headingNote("TRENDING", trendingNote(b.TrendingSource), width))
	if len(b.Trending) == 0 {
		out = append(out, stMuted.Render("No trending names."))
	} else {
		for _, row := range b.Trending {
			parts := []string{
				formatUSDOrDash(row.McapUSD, false),
				"vol " + formatUSDOrDash(row.VolumeUSD, false),
			}
			if row.Holders != nil {
				parts = append(parts, formatCount(row.Holders)+" holders")
			}
			out = append(out, quoteLine(row.Symbol, parts, row.Mint, width))
		}
	}
	out = append(out, "")
	if !pumpAsTrenches {
		out = append(out, stGold.Render("PUMP.FUN"))
		out = append(out, pumpColumns(pump, colW, width)...)
		out = append(out, "")
	}
	out = append(out, stGold.Render("CARDS"))
	out = append(out, renderCards(b.Cards, width, "No verdict cards.")...)
	return out
}

// headingNote renders a gold section title followed by a muted source note,
// trimmed so it never runs past width.
func headingNote(title, note string, width int) string {
	if note == "" || lipgloss.Width(title)+2 >= width {
		return stGold.Render(fit(title, width))
	}
	return stGold.Render(title) + "  " + stMuted.Render(fit(note, width-lipgloss.Width(title)-2))
}

func trendingNote(source string) string {
	switch source {
	case "gmgn":
		return "via GMGN"
	case "dexscreener":
		return "1h volume · DexScreener"
	}
	return ""
}

func trenchesEmpty(t Trenches) bool {
	return len(t.NewCreation) == 0 && len(t.NearCompletion) == 0 && len(t.Completed) == 0
}

func pumpColumns(pump Pump, colW, width int) []string {
	return threeColumns([3]colBlock{
		{title: "New", lines: pumpRows(pump.New, colW)},
		{title: "Almost", lines: pumpRows(pump.Almost, colW)},
		{title: "Graduated", lines: pumpRows(pump.Graduated, colW)},
	}, width)
}

func trenchRows(rows []TrenchRow, width int) []string {
	var out []string
	for _, row := range rows {
		parts := []string{formatUSDOrDash(row.McapUSD, false)}
		if row.Holders != nil {
			parts = append(parts, formatCount(row.Holders)+" holders")
		}
		out = append(out, quoteLine(row.Symbol, parts, row.Mint, width))
	}
	return out
}

func pumpRows(rows []PumpRow, width int) []string {
	var out []string
	for _, row := range rows {
		out = append(out, quoteLine(row.Symbol, []string{
			formatUSDOrDash(row.McapUSD, false),
			"curve " + formatBond(row.BondingPct),
		}, row.Mint, width))
	}
	return out
}

func peopleLines(cards []Card, note string, width int) []string {
	out := []string{stGold.Render("PEOPLE")}
	if len(cards) == 0 {
		if strings.TrimSpace(note) == "" {
			note = "No people cards."
		}
		return append(out, stMuted.Render(note))
	}
	out = append(out, renderCards(cards, width, "No people cards.")...)
	return out
}

type colBlock struct {
	title string
	lines []string
}

func columnWidth(width int) (int, bool) {
	colW := (width - 2) / 3
	if colW < 16 {
		return width, true
	}
	return colW, false
}

func threeColumns(cols [3]colBlock, width int) []string {
	colW, stacked := columnWidth(width)
	if stacked {
		var out []string
		for _, col := range cols {
			out = append(out, stGold.Render(col.title))
			out = append(out, columnBody(col.lines, width)...)
		}
		return out
	}
	blocks := make([]string, 3)
	for i, col := range cols {
		body := append([]string{stGold.Render(pad(col.title, colW))}, columnBody(col.lines, colW)...)
		blocks[i] = strings.Join(body, "\n")
	}
	joined := lipgloss.JoinHorizontal(lipgloss.Top,
		lipgloss.NewStyle().Width(colW).Background(bg).Render(blocks[0]),
		stBlank.Render(" "),
		lipgloss.NewStyle().Width(colW).Background(bg).Render(blocks[1]),
		stBlank.Render(" "),
		lipgloss.NewStyle().Width(colW).Background(bg).Render(blocks[2]),
	)
	return linesOf(joined)
}

func columnBody(lines []string, width int) []string {
	if len(lines) == 0 {
		return []string{stMuted.Render(fit("none", width))}
	}
	out := make([]string, len(lines))
	for i, ln := range lines {
		if lipgloss.Width(ln) > width {
			out[i] = truncateANSI(ln, width)
		} else {
			out[i] = ln
		}
	}
	return out
}

func quoteLine(symbol string, parts []string, mint string, width int) string {
	if symbol == "" {
		symbol = shortMint(mint, "")
	}
	if symbol == "" {
		symbol = "?"
	}
	segs := []seg{{symbol, stTextBold}}
	for _, part := range parts {
		segs = append(segs, seg{part, stMuted})
	}
	short := shortMint(mint, "")
	if short != "" && short != symbol {
		segs = append(segs, seg{short, stMuted})
	}
	for {
		lines := wrapSegs(segs, width)
		if len(lines) <= 1 || len(segs) == 1 {
			if len(lines) == 0 {
				return ""
			}
			return lines[0]
		}
		segs = segs[:len(segs)-1]
	}
}

func formatUSDOrDash(v *float64, signed bool) string {
	if v == nil {
		return "—"
	}
	return formatUSD(*v, signed)
}

func renderCards(cards []Card, width int, empty string) []string {
	if len(cards) == 0 {
		return []string{stMuted.Render(empty)}
	}
	var out []string
	for i, card := range cards {
		if i > 0 {
			out = append(out, "")
		}
		out = append(out, linesOf(renderCard(card, width))...)
	}
	return out
}

func renderCard(c Card, width int) string {
	label := strings.ToUpper(strings.TrimSpace(c.Verdict))
	if label == "" {
		label = "NOTE"
	}
	color := verdictHex(c.Verdict)
	st := lipgloss.NewStyle().
		Border(lipgloss.NormalBorder()).
		BorderForeground(lipgloss.Color(color)).
		BorderBackground(bg).
		Foreground(lipgloss.Color(cText)).
		Background(bg).
		Padding(0, 1)
	frame := st.GetHorizontalFrameSize()
	inner := width - frame
	if inner < 4 {
		inner = 4
	}
	if inner > width {
		inner = width
	}
	st = st.Width(inner)
	head := ink(color).Bold(true).Render(label)
	body := c.Text
	if strings.TrimSpace(body) == "" {
		return st.Render(head)
	}
	return st.Render(head + "\n" + body)
}

func (m *model) feedBlock(w, n int) []string {
	if n <= 0 {
		return nil
	}
	lines := []string{stGold.Render("FEED")}
	var items []string
	if m.snap != nil {
		items = m.snap.Feed
	}
	room := n - 1
	if room < 1 {
		return []string{stGold.Render("FEED")}[:min(1, n)]
	}
	if len(items) == 0 {
		lines = append(lines, stMuted.Render("Feed is empty."))
	} else {
		if len(items) > room {
			items = items[len(items)-room:]
		}
		for _, line := range items {
			lines = append(lines, feedStyle(line).Render(fit(line, w)))
		}
	}
	for len(lines) < n {
		lines = append(lines, "")
	}
	if len(lines) > n {
		lines = lines[:n]
	}
	return lines
}

func naturalFeed(s Snapshot) int {
	n := len(s.Feed)
	if n == 0 {
		n = 1
	}
	if n > 8 {
		n = 8
	}
	return n + 1
}

type seg struct {
	text  string
	style lipgloss.Style
}

func wrapSegs(segs []seg, width int) []string {
	if width < 4 {
		width = 4
	}
	sep := stMuted.Render(" · ")
	sepW := lipgloss.Width(sep)
	var lines []string
	var cur string
	curW := 0
	for _, s := range segs {
		text := s.text
		if text == "" {
			text = "—"
		}
		if lipgloss.Width(text) > width {
			text = fit(text, width)
		}
		piece := s.style.Render(text)
		pw := lipgloss.Width(piece)
		if curW == 0 {
			cur = piece
			curW = pw
			continue
		}
		if curW+sepW+pw <= width {
			cur += sep + piece
			curW += sepW + pw
			continue
		}
		lines = append(lines, cur)
		cur = piece
		curW = pw
	}
	if curW > 0 {
		lines = append(lines, cur)
	}
	if len(lines) == 0 {
		return []string{""}
	}
	return lines
}

func joinCols(left, right []string, leftW, rightW int) []string {
	n := max(len(left), len(right))
	out := make([]string, n)
	gap := stBlank.Render(" ") + stRule.Render("│") + stBlank.Render(" ")
	for i := 0; i < n; i++ {
		l, r := "", ""
		if i < len(left) {
			l = left[i]
		}
		if i < len(right) {
			r = right[i]
		}
		out[i] = padLine(l, leftW) + gap + padLine(r, rightW)
	}
	return out
}

func rule(w int) string {
	if w < 1 {
		return ""
	}
	return stRule.Render(strings.Repeat("─", w))
}

func padLine(s string, n int) string {
	if n <= 0 {
		return ""
	}
	if lipgloss.Width(s) > n {
		s = truncateANSI(s, n)
	}
	if d := n - lipgloss.Width(s); d > 0 {
		s += stBlank.Render(strings.Repeat(" ", d))
	}
	return s
}

func linesOf(s string) []string {
	s = strings.TrimRight(s, "\n")
	if s == "" {
		return nil
	}
	return strings.Split(s, "\n")
}

func window(lines []string, offset, n int) []string {
	if n <= 0 {
		return nil
	}
	if offset < 0 {
		offset = 0
	}
	if offset > len(lines) {
		offset = len(lines)
	}
	out := make([]string, 0, n)
	for i := offset; i < len(lines) && len(out) < n; i++ {
		out = append(out, lines[i])
	}
	for len(out) < n {
		out = append(out, "")
	}
	return out
}

func dash(s string) string {
	if strings.TrimSpace(s) == "" {
		return "—"
	}
	return s
}
