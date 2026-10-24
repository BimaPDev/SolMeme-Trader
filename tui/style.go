package main

import "github.com/charmbracelet/lipgloss"

const (
	cText  = "#F3ECDC"
	cMuted = "#9A927F"
	cGold  = "#E0B44A"
	cUp    = "#8FBF6A"
	cDown  = "#E07A5F"
	cRule  = "#3A4032"
)

// bg is a dark desk on both light and dark terminals.
var bg = lipgloss.AdaptiveColor{Light: "#1A1D16", Dark: "#101208"}

func ink(hex string) lipgloss.Style {
	return lipgloss.NewStyle().Foreground(lipgloss.Color(hex)).Background(bg)
}

var (
	stText     = ink(cText)
	stTextBold = ink(cText).Bold(true)
	stMuted    = ink(cMuted)
	stGold     = ink(cGold)
	stGoldBold = ink(cGold).Bold(true)
	stUp       = ink(cUp)
	stDown     = ink(cDown)
	stRule     = ink(cRule)
	stBlank    = lipgloss.NewStyle().Background(bg)
)

func pnlStyle(v float64) lipgloss.Style {
	switch {
	case v > 0:
		return stUp
	case v < 0:
		return stDown
	default:
		return stText
	}
}

func madeStyle(s string) lipgloss.Style {
	switch {
	case len(s) > 0 && s[0] == '+':
		return stUp
	case len(s) > 0 && s[0] == '-':
		return stDown
	default:
		return stText
	}
}

func phaseStyle(phase string) lipgloss.Style {
	switch phase {
	case "holding":
		return stGold
	case "selling":
		return stDown
	case "buying":
		return stUp
	case "analyzing", "watching":
		return stText
	case "skipped", "checking":
		return stMuted
	default:
		return stText
	}
}

func statusStyle(status string) lipgloss.Style {
	switch status {
	case "ok", "live", "connected", "open":
		return stUp
	case "error", "down", "failed", "closed":
		return stDown
	default:
		return stMuted
	}
}

func verdictHex(verdict string) string {
	switch verdict {
	case "skip":
		return cDown
	case "research":
		return cMuted
	case "watch":
		return cGold
	case "copy":
		return cUp
	default:
		return cText
	}
}

func feedStyle(line string) lipgloss.Style {
	switch {
	case containsWord(line, "BUY"):
		return stUp
	case containsWord(line, "SELL"), containsWord(line, "SKIP"):
		return stDown
	case containsWord(line, "EXPLORE"):
		return stGold
	default:
		return stText
	}
}

func containsWord(s, word string) bool {
	return stringsContainsFold(s, word)
}

func stringsContainsFold(s, word string) bool {
	// ASCII feed lines. Match the token with spaces or string edges.
	if word == "" {
		return false
	}
	rest := s
	for {
		i := indexFold(rest, word)
		if i < 0 {
			return false
		}
		before := i == 0 || isSep(rest[i-1])
		after := i+len(word) == len(rest) || isSep(rest[i+len(word)])
		if before && after {
			return true
		}
		rest = rest[i+1:]
	}
}

func indexFold(s, word string) int {
	for i := 0; i+len(word) <= len(s); i++ {
		if equalFoldASCII(s[i:i+len(word)], word) {
			return i
		}
	}
	return -1
}

func equalFoldASCII(a, b string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := 0; i < len(a); i++ {
		ca, cb := a[i], b[i]
		if ca >= 'a' && ca <= 'z' {
			ca -= 'a' - 'A'
		}
		if cb >= 'a' && cb <= 'z' {
			cb -= 'a' - 'A'
		}
		if ca != cb {
			return false
		}
	}
	return true
}

func isSep(b byte) bool {
	switch b {
	case ' ', '\t', '|', ',', ':', ';', '/':
		return true
	default:
		return false
	}
}
