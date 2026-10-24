# SolMeme-Trader: 2-week test run, monitoring plan

**Test run started:** Thu 2026-10-08 20:57:57 EDT (every wallet reset to a fresh $50; all history kept)
**Planned end:** about Thu 2026-10-22
**Mode:** paper only. No transactions are ever sent.

This is a checklist for you. The point of the test is to find out whether the evolved strategies keep
making money across different days and market moods, or whether the first evening was a hot streak.
Most of the work is *not touching it* and checking in at the right times.

---

## 1. The rules of the test

| Do | Don't |
|---|---|
| Look at the dashboard once or twice a day | Restart the bot "just to see" |
| Write the numbers down at each checkpoint (section 5) | Edit `config/settings.yaml` or strategy files |
| Fix real breakage (section 7) | Deploy code changes |
| Let the swarm keep evolving in the background | Promote anything by hand (auto-promotion is paused) |

Why: every change restarts or confuses the record. The bot now keeps its wallet **and open positions**
across a restart, so a restart no longer corrupts results, but each one is still a gap in data
collection and a chance to break something.

**Settings frozen for the test** (already applied):
- `evolution.auto_promote: false`: the swarm's winners wait in line, they do not take over the champions' wallet.
- `portfolio.max_position_sol: 0.45` (about $50): caps bet size so paper fills stay realistic as the wallet grows.
- Bets are 15% of the wallet (`portfolio.position_pct`), one position per token across champions.
- Champions: `holder_dip@0e7153d0e2` (sell half at 2x, rest at 4x) and `kol_follow@c4f1f23c2e`.
  `curve_momentum` and `launch_sniper` are also champions but rarely trade.

---

## 2. Where to look

### Dashboard (main place): http://<bot-host>
LAN only. Also a **SolMeme** tile on Homarr (http://<homarr-host>:7575). From outside your home network
it does not work unless you use a VPN: no remote access has been set up.

| Tab | What it tells you |
|---|---|
| **Overview** | Champions' wallet, **locked in vs open positions**, session range (high/low), swarm wallets, **Best performing** (tap it for the full profile), wallet charts |
| **Swarm** | Generation clock and phase, alive/dead agents, evolution chart, living agents (sortable), graveyard, lessons, **Council** judges and ideas, timeline |
| **Market** | Positions (Invested gray / Now green or red / Gain), tokens being watched, feed, **Whales**, **Banned wallets** |
| **Sources** | Data feeds: status, event counts, errors |

- **Sync** button (top right, next to *live*): re-fetches everything now, skipping caches.
- Page shows **offline** if the bot is not answering for several tries in a row; one slow poll will not trip it.
- Sorts you choose on tables are remembered per device.

### Tap into an agent
Overview → **Best performing** → tap. You get its wallet curve, profit by token, family tree,
what changed from its parent, rules, **Why it's winning**, **Stress test**, and every trade.

### Server (when you need detail)
```
ssh root@<bot-host>            # your Mac's key is already installed
cd /home/solmeme/SolMeme-Trader
```
| What | Where / command |
|---|---|
| Bot status | `systemctl status solmeme` (look at `Active:` time and restarts) |
| Live log | `journalctl -u solmeme -f` (Ctrl-C to stop) |
| Last generation's report | `data/reports/evolution/latest.md` (births, deaths, lessons, whales, winners, bans, council) |
| Generation log | `logs/evolve.log` |
| Swarm leaderboard | `su solmeme -c ".venv/bin/solmeme swarm"` |
| Council judges and ideas | `su solmeme -c ".venv/bin/solmeme council"` |
| Ban list | `su solmeme -c ".venv/bin/solmeme ban list"` |
| All strategy versions | `su solmeme -c ".venv/bin/solmeme strategies"` |
| Champions' wallet save | `data/session/champions_wallet.json` |
| Wallet reset time | `data/session/wallet_reset.json` |
| Winners and stress results | `data/winners.json` |
| Whale book | `data/whales.json` |
| Ban list file | `data/banned_wallets.json` |
| Database (all trades) | `data/solmeme.db` |

### Proxmox (the host): https://<proxmox-host>:8006
Container **105 solmeme**: check CPU and memory on its Summary page. The LocalAI VM (106) is the GPU the
council uses; the 9Router container is 102.

---

## 3. Daily check (2 minutes)

Open the dashboard, tap **Sync**, and look at:

1. **Header, "live" dot:** green? If *offline*, go to section 7.
2. **Champions' wallet** and **Locked in:** note both. Locked in = value from closed trades only;
   the gap to the wallet is open positions valued at market.
3. **Session range:** how deep was the worst dip since the start?
4. **Trades** keep increasing (Market → Positions has recent rows). No new trades for **over 1 hour**
   is abnormal (see 7).
5. **Swarm:** about 14-16 alive, generation number increasing roughly hourly, no generation stuck on
   one phase for over 30 minutes.
6. **Sources:** `dexscreener`, `pumpportal`, `sol_price` ok. `gmgn` showing **backoff** briefly is normal
   (rate limits); constantly backoff for hours is not.

Copy-paste quick numbers from your Mac, no SSH needed:
```
curl -s http://<bot-host>/api/snapshot | python3 -c "
import sys,json,time; s=json.load(sys.stdin); w=s['wallet']; r=w['range']
print('uptime %.1f h'%(s['uptime_s']/3600), '| wallet \$%.2f'%w['balance_usd'], '| locked %+.2f open %+.2f'%(w['locked_usd'],w['open_usd']))
print('low \$%.2f high \$%.2f'%(r['low'],r['high']), '| goal', w['goal_usd'], '| sol', s['sol_usd'])
print('sources:', [(x['name'],x['status']) for x in s['sources']])
print('champions:', [(x['key'],x['closed'],round(x['pnl_sol'],3)) for x in s['strategies'] if x['role']=='champion'])"
```

---

## 4. Weekly checks (10 minutes)

On the server:
```
df -h /                 # disk: see "Known gaps" below
free -m                 # memory: swap use should not be growing steadily
systemctl show solmeme -p NRestarts -p ActiveEnterTimestamp
grep -c "killed" logs/evolve.log | tail -1
```
On the dashboard:
- **Swarm → Council:** are judges answering? (`last answered` recent, no repeated errors)
- **Market → Banned wallets:** count growing steadily is fine. If it jumps by hundreds, something is
  flagging too much.
- **Overview → Best performing → top agent → Stress test:** robustness still 60% or higher.

---

## 5. Checkpoints (when to take a real look)

Write the numbers in the log at the bottom of this file each time. Note the **SOL price** too: the wallet
is held in SOL, so USD moves with SOL even when the strategies do nothing.

| Checkpoint | Date | What "good" looks like | Red flag |
|---|---|---|---|
| **24 hours** | Fri Oct 9, about 9 PM | Wallet still clearly above $50 after the overnight stretch | Below $35, or no trades for hours |
| **3 days** | Sun Oct 11 | Positive on most days. Profit does **not** come mostly from 1-3 tokens (still positive without them). Worst dip you could live through | Profit from a single day or token; drawdown over 40% from the high |
| **1 week** (includes a weekend) | Thu Oct 15 | Same as above plus stable win rate and stress-test robustness at 60% or higher. If yes: build the **dry-run live mode** (real quotes, no real money) | A losing week, or the edge only exists in one time of day |
| **2 weeks** | Thu Oct 22 | Dry-run fills match paper results reasonably | Real quotes far worse than paper: stop and rethink |

These thresholds are my suggestions, not rules. Use your own comfort with risk.

**How to do the checkpoint analysis:** ask Claude to "review the test run". It will pull every champion
trade since `2026-10-08 20:57:57`, compute daily profit, profit without the top 3 tokens, win rate,
largest drawdown, time-of-day results, and compare with the swarm's best agents.

Things to keep in mind when reading results:
- **Survivorship:** the swarm's top agents were picked from many tries, so they look better than their true edge. The champions' wallet is the honest number.
- **Paper is optimistic:** prices are 5-10 seconds old, our buys never move the price, rugs are modeled as partial loss.
- **Gaps from restarts:** none expected. If `NRestarts` is above 0 or the start time moved, note why.

---

## 6. What is running by itself (so you don't have to)

| Thing | How often | Visible where |
|---|---|---|
| Swarm generation: kill losers, breed, screen, whale scan, ban scan, stress test, council | About hourly (takes 10-25 min) | Swarm tab clock; `data/reports/evolution/latest.md` |
| Agent kills: losing with 80% confidence after 4+ trades, or wallet under 25% | Each generation | Graveyard |
| Stress test of the top 3 live winners | Every 3rd generation, or when a new winner appears | Agent profile → Stress test |
| Whale re-scoring (top 10 whales polled live) | Each generation | Market → Whales |
| Ban scan (clusters, rug creators) with council vote | Each generation | Market → Banned wallets |
| Champions' wallet and open positions saved | Every minute and at shutdown | `data/session/champions_wallet.json` |
| Council (qwen27b, qwen8b, 9router) | Each generation | Swarm → Council |

---

## 7. When something looks wrong: runbook

| Symptom | Likely cause | What to do |
|---|---|---|
| Dashboard says **offline** | Bot stopped, container down, or LAN issue | `ssh root@<bot-host>` then `systemctl status solmeme`. If not active: `systemctl restart solmeme`. If SSH fails: check the container in Proxmox (https://<proxmox-host>:8006) and start it |
| After a restart | Expected: wallet, open positions and swarm wallets continue; there is a 2-minute no-buy warm-up | Verify the wallet number matches what it was. Log says `champions' wallet continues` and `restored N open positions` (`journalctl -u solmeme | grep -a continues`) |
| **No trades for over an hour** | GMGN rate-limited (holder counts missing), or a quiet market | Sources tab: gmgn status and error count. Usually recovers on its own; wait 1-2 hours before worrying |
| Generation stuck on a phase for 30+ minutes | GPU busy (council), or a hung call | `tail logs/evolve.log`. The council has a 20-minute budget; a stuck process ends on the next hour. Not urgent |
| Swarm has few agents alive | Heavy kills, then refill takes a generation | Wait one or two generations |
| **Memory** high, bot killed (`status=9/KILL` plus a non-zero `NRestarts`) | Container has only 2 GB; the bot plus a generation reaches about 1.9 GB | Note it. Consider raising the container to 3 GB in Proxmox, but the host is already tight (LocalAI VM uses about 10.7 GB of 16.6 GB) |
| Wallet drops sharply (over 30% in a day) | A bad stretch, or a champion misbehaving | Look at Market → Positions for stacked losers and the champion's recent trades. To take a champion off the shared wallet: `su solmeme -c ".venv/bin/solmeme demote <strategy>"` (undo with `promote`). Do not do this casually: it changes the test |
| Ban list explodes (hundreds in one generation) | Detector too loose | Review with `solmeme ban list`; remove with `solmeme ban remove <wallet>`; ask Claude to tighten the rule |
| Council shows repeated errors | Ollama GPU model swapping or 9Router down | Judges retry on their own. Ignore unless it lasts a day |
| Disk filling | Event logs grow about 400 MB/day | See "Known gaps" |

**Never run `./start` or `solmeme reset-wallets` during the test.** Both reset money and end the run.

---

## 8. Known gaps (checked 2026-10-08)

- **Event logs are never cleaned up automatically.** The cleanup function exists but nothing calls it.
  Disk was 3.7 GB used of 20 GB, so the 2-week test is safe (about 5-6 GB more). Check `df -h /` weekly,
  and fix it after the test.
- **No alerts exist.** Monitoring is manual (this plan). If you want push notifications (bot down,
  wallet drops 25%, no trades for 2 hours), that is a small addition: ask Claude.
- **No remote access** off your home network.
- **Paper is not live:** simulated fills, delayed free data, no price impact. See the dry-run plan.
- **Security housekeeping:** the Proxmox root password appeared in the chat, so **change it**. API keys
  live in the container's `.env`; keep that file private. Never put a funded wallet key anywhere near
  the Axiom/fomo/j7 browser sessions.

---

## 9. After the test

1. Run the checkpoint analysis (section 5) on the full two weeks.
2. If it holds up: build the **dry-run live mode** (asks the real exchange for quotes, tests transactions without sending) and measure how far paper is from reality.
3. Only then: a **tiny live test** ($20-50, fresh wallet, one champion, hard loss limit, kill switch).
4. Decide what happens at your goal (bank the original stake? raise the goal? smaller bets?).
5. Re-enable auto-promotion and fix the event-log cleanup.

---

## Log (fill in at each checkpoint)

| Date / time | SOL price | Wallet $ | Locked in $ | Open $ | Trades (champions) | Win % | Worst dip since start | Swarm alive | Top agent robustness | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| Oct 8 20:58 (start) | | 50.00 | 50.00 | 0 | 0 | | | 16 | | reset, auto-promote off |
| Oct 9 02:27 (re-baseline) | 110.40 | 50.00 | 50.00 | 0 | 148 before switch | | $12.15 (Oct 8 23:23) | 16 | | Old champions' wallet ended at $41-43. kol_follow demoted 23:08 (-0.304 SOL). Ladder champion `holder_dip@0e7153d0e2` → trial (own wallet); swarm `holder_dip@14a5750609` (one sell at 2.6x, SL 25%, no liquidity rule) → champion, fresh $50. 7 deploy restarts Oct 8 23:07 → Oct 9 01:47 killed swarm gens 27-31. **Checkpoints below count from this row.** |
| Oct 9 ~21:00 (24h) | | | | | | | | | | |
| Oct 11 (3 days) | | | | | | | | | | |
| Oct 15 (1 week) | | | | | | | | | | |
| Oct 22 (2 weeks) | | | | | | | | | | |
