package main

import (
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

const (
	viewDesk = iota
	viewPositions
	viewTokens
	viewBoards
	viewPeople
	viewCount
)

var viewNames = [...]string{"desk", "positions", "tokens", "boards", "people"}

const pollEvery = 500 * time.Millisecond

type model struct {
	url      string
	snap     *Snapshot
	haveSnap bool
	errText  string
	fetching bool
	view     int
	scroll   int
	width    int
	height   int
	help     bool
}

type tickMsg struct{}

type fetchedMsg struct {
	snap Snapshot
	err  error
}

func newModel(url string) *model {
	return &model{url: url, fetching: true}
}

func (m *model) Init() tea.Cmd {
	return tea.Batch(fetchCmd(m.url), scheduleTick())
}

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.clampScroll()
		return m, nil
	case tea.KeyMsg:
		return m.handleKey(msg)
	case tickMsg:
		cmds := []tea.Cmd{scheduleTick()}
		if !m.fetching {
			m.fetching = true
			cmds = append(cmds, fetchCmd(m.url))
		}
		return m, tea.Batch(cmds...)
	case fetchedMsg:
		m.fetching = false
		if msg.err != nil {
			m.errText = msg.err.Error()
			return m, nil
		}
		snap := msg.snap
		m.snap = &snap
		m.haveSnap = true
		m.errText = ""
		m.clampScroll()
		return m, nil
	}
	return m, nil
}

func (m *model) handleKey(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch k.String() {
	case "q", "ctrl+c":
		return m, tea.Quit
	case "tab", "right":
		m.setView(m.view + 1)
	case "shift+tab", "left":
		m.setView(m.view - 1)
	case "1", "2", "3", "4", "5":
		m.setView(int(k.String()[0] - '1'))
	case "j", "down":
		m.scroll++
		m.clampScroll()
	case "k", "up":
		m.scroll--
		m.clampScroll()
	case "?":
		m.help = !m.help
		m.clampScroll()
	}
	return m, nil
}

func (m *model) setView(v int) {
	if v < 0 {
		v = viewCount - 1
	} else if v >= viewCount {
		v = 0
	}
	if v == m.view {
		return
	}
	m.view = v
	m.scroll = 0
}

func (m *model) clampScroll() {
	if m.width <= 0 || m.height <= 0 {
		if m.scroll < 0 {
			m.scroll = 0
		}
		return
	}
	_, bodyH, _, _ := m.measure(m.width, m.height)
	maxScroll := len(m.scrollLines(m.width)) - bodyH
	if maxScroll < 0 {
		maxScroll = 0
	}
	if m.scroll > maxScroll {
		m.scroll = maxScroll
	}
	if m.scroll < 0 {
		m.scroll = 0
	}
}

func fetchCmd(url string) tea.Cmd {
	return func() tea.Msg {
		snap, err := fetchSnapshot(url)
		return fetchedMsg{snap: snap, err: err}
	}
}

func scheduleTick() tea.Cmd {
	return tea.Tick(pollEvery, func(time.Time) tea.Msg { return tickMsg{} })
}
