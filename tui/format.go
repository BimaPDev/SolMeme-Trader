package main

import (
	"fmt"
	"math"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/charmbracelet/lipgloss"
)

func formatUptime(sec float64) string {
	if sec < 0 || math.IsNaN(sec) || math.IsInf(sec, 0) {
		sec = 0
	}
	total := int(sec)
	h := total / 3600
	m := (total % 3600) / 60
	s := total % 60
	switch {
	case h > 0:
		return fmt.Sprintf("%dh %02dm %02ds", h, m, s)
	case m > 0:
		return fmt.Sprintf("%dm %02ds", m, s)
	default:
		return fmt.Sprintf("%ds", s)
	}
}

func formatSOL(v float64) string {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return "—"
	}
	s := strconv.FormatFloat(v, 'f', 3, 64)
	if strings.Contains(s, ".") {
		s = strings.TrimRight(s, "0")
		s = strings.TrimRight(s, ".")
	}
	if s == "" || s == "-" || s == "-0" {
		return "0"
	}
	return s
}

func formatSignedSOL(v float64) string {
	if v == 0 || math.IsNaN(v) || math.IsInf(v, 0) {
		return "0"
	}
	s := formatSOL(math.Abs(v))
	if v < 0 {
		return "-" + s
	}
	return "+" + s
}

func formatUSD(v float64, signed bool) string {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return "—"
	}
	sign := ""
	if v < 0 {
		sign = "-"
	} else if signed && v > 0 {
		sign = "+"
	}
	abs := math.Abs(v)
	if abs >= 1000 {
		return sign + "$" + groupInt(int64(math.Round(abs)))
	}
	return sign + fmt.Sprintf("$%.2f", abs)
}

func formatSolPrice(v *float64) string {
	if v == nil {
		return "—"
	}
	return formatUSD(*v, false)
}

func formatCount(v *float64) string {
	if v == nil || math.IsNaN(*v) || math.IsInf(*v, 0) {
		return "—"
	}
	return groupInt(int64(math.Round(*v)))
}

func formatEvents(n float64) string {
	if math.IsNaN(n) || math.IsInf(n, 0) {
		return "0"
	}
	return groupInt(int64(math.Round(n)))
}

func formatPct(v float64) string {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return "—"
	}
	return fmt.Sprintf("%.1f%%", v)
}

func formatBond(v *float64) string {
	if v == nil || math.IsNaN(*v) || math.IsInf(*v, 0) {
		return "—"
	}
	if math.Abs(*v-math.Round(*v)) < 0.05 {
		return fmt.Sprintf("%.0f%%", *v)
	}
	return fmt.Sprintf("%.1f%%", *v)
}

func formatWinRate(v *float64) string {
	if v == nil || math.IsNaN(*v) || math.IsInf(*v, 0) {
		return "n/a"
	}
	return fmt.Sprintf("%.0f%%", *v*100)
}

func formatThreshold(v *float64) string {
	if v == nil || math.IsNaN(*v) || math.IsInf(*v, 0) {
		return "—"
	}
	return fmt.Sprintf("%.2f", *v)
}

func groupInt(n int64) string {
	sign := ""
	if n < 0 {
		sign = "-"
		n = -n
	}
	s := strconv.FormatInt(n, 10)
	pre := len(s) % 3
	if pre == 0 {
		pre = 3
	}
	var b strings.Builder
	b.WriteString(sign)
	b.WriteString(s[:pre])
	for i := pre; i < len(s); i += 3 {
		b.WriteByte(',')
		b.WriteString(s[i : i+3])
	}
	return b.String()
}

func shortMint(mint, given string) string {
	if given != "" {
		return given
	}
	if len(mint) > 12 {
		return mint[:4] + "…" + mint[len(mint)-4:]
	}
	return mint
}

func fit(s string, n int) string {
	if n <= 0 {
		return ""
	}
	if lipgloss.Width(s) <= n {
		return s
	}
	if n == 1 {
		return "…"
	}
	r := []rune(s)
	for len(r) > 0 && lipgloss.Width(string(r)) > n-1 {
		r = r[:len(r)-1]
	}
	return string(r) + "…"
}

func pad(s string, n int) string {
	s = fit(s, n)
	if d := n - lipgloss.Width(s); d > 0 {
		s += strings.Repeat(" ", d)
	}
	return s
}

func cleanErr(s string) string {
	return strings.Join(strings.Fields(s), " ")
}

// truncateANSI keeps a visible width and closes any open SGR sequence.
func truncateANSI(s string, limit int) string {
	if limit <= 0 || s == "" {
		return ""
	}
	if lipgloss.Width(s) <= limit {
		return s
	}
	var b strings.Builder
	width := 0
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
			} else if j < len(s) && s[j] == ']' {
				j++
				for j < len(s) && s[j] != 0x07 && !(s[j] == 0x1b) {
					j++
				}
				if j < len(s) {
					j++
				}
			} else if j < len(s) {
				j++
			}
			b.WriteString(s[i:j])
			i = j
			continue
		}
		r, size := utf8.DecodeRuneInString(s[i:])
		rw := lipgloss.Width(string(r))
		if rw < 0 {
			rw = 0
		}
		room := limit - width
		if room <= 0 || rw > room || (rw == room && i+size < len(s) && width+rw >= limit && limit > 1) {
			if room >= 1 && limit > 1 {
				b.WriteString("…")
			}
			b.WriteString("\x1b[0m")
			return b.String()
		}
		b.WriteRune(r)
		width += rw
		i += size
	}
	return b.String()
}

// formatUSDCents keeps cents at any size: $8,304.10.
func formatUSDCents(v float64, signed bool) string {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return "—"
	}
	cents := int64(math.Round(math.Abs(v) * 100))
	sign := ""
	if v < 0 && cents != 0 {
		sign = "-"
	} else if signed && v > 0 && cents != 0 {
		sign = "+"
	}
	return fmt.Sprintf("%s$%s.%02d", sign, groupInt(cents/100), cents%100)
}

func formatSignedPct(v *float64) string {
	if v == nil || math.IsNaN(*v) || math.IsInf(*v, 0) {
		return ""
	}
	s := fmt.Sprintf("%.2f%%", math.Abs(*v))
	switch {
	case s == "0.00%":
		return s
	case *v < 0:
		return "-" + s
	default:
		return "+" + s
	}
}

// formatGoalUSD drops cents on whole amounts: $100, $1,000, $99.50.
func formatGoalUSD(v float64) string {
	if !math.IsNaN(v) && !math.IsInf(v, 0) && v == math.Trunc(v) {
		return formatUSD0(v)
	}
	return formatUSDCents(v, false)
}

func formatUSD0(v float64) string {
	sign := ""
	if v < 0 {
		sign = "-"
	}
	return sign + "$" + groupInt(int64(math.Abs(v)))
}

// formatGoalPct rounds progress to a whole percent but never shows 100% for a
// wallet still short of the goal.
func formatGoalPct(v *float64) string {
	if v == nil || math.IsNaN(*v) || math.IsInf(*v, 0) {
		return ""
	}
	p := math.Round(*v)
	if *v < 100 && p >= 100 {
		p = 99
	}
	if p < 0 {
		p = 0
	}
	return fmt.Sprintf("%.0f%%", p)
}

// walletFigures returns start, now, gain and pct text plus the gain's sign.
// It uses USD when every USD figure is known and falls back to SOL.
func walletFigures(w Wallet) (start, now, gain, pct string, sign float64) {
	pct = formatSignedPct(w.PnlPct)
	if w.StartUSD != nil && w.BalanceUSD != nil && w.PnlUSD != nil {
		gain = formatUSDCents(*w.PnlUSD, true)
		sign = signOfMoney(*w.PnlUSD, 0.005)
		return formatUSDCents(*w.StartUSD, false), formatUSDCents(*w.BalanceUSD, false), gain, pct, sign
	}
	gain = formatSignedSOL(w.PnlSOL) + " SOL"
	sign = signOfMoney(w.PnlSOL, 0.0005)
	return formatSOL(w.StartSOL) + " SOL", formatSOL(w.BalanceSOL) + " SOL", gain, pct, sign
}

// signOfMoney treats amounts that round to zero on screen as flat.
func signOfMoney(v, eps float64) float64 {
	switch {
	case math.IsNaN(v) || math.Abs(v) < eps:
		return 0
	case v > 0:
		return 1
	default:
		return -1
	}
}

// formatDrawdown renders a percent below the session high as "−7.7%", with
// two decimals for small dips so they never print as zero. It is empty at the
// high (or for a value that rounds to zero).
func formatDrawdown(v float64) string {
	if math.IsNaN(v) || math.IsInf(v, 0) || v >= 0 {
		return ""
	}
	a := math.Abs(v)
	s := fmt.Sprintf("%.1f", a)
	if a < 0.05 {
		s = fmt.Sprintf("%.2f", a)
	}
	if strings.Trim(s, "0.") == "" {
		return ""
	}
	return "−" + s + "%"
}
