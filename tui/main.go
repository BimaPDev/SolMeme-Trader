package main

import (
	"flag"
	"fmt"
	"os"

	tea "github.com/charmbracelet/bubbletea"
)

func main() {
	url := flag.String("url", "http://127.0.0.1:8787/api/snapshot", "paper snapshot URL")
	flag.Parse()

	p := tea.NewProgram(newModel(*url), tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "solmeme-tui: %s\n", err)
		os.Exit(1)
	}
}
