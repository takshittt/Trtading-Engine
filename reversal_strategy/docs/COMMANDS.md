# Reversal Strategy — Operations Cheatsheet

Server: `root@192.168.133.205`  ·  Backend `:8020`  ·  Dashboard `:5176`  ·  shared Shoonya gateway `:8000`

---

## Deploy / update the server (run on your Mac, from the project root)

```bash
./deploy.sh          # rsync code → rebuild frontend → restart services
```

Push code to GitHub:
```bash
git add -A && git commit -m "your message" && git push
```

---

## Restart / manage the strategy (run on the SERVER)

```bash
ssh root@192.168.133.205

systemctl restart swing-swager            # restart the backend (strategy)
systemctl restart swing-swager-ui         # restart the dashboard
systemctl restart swing-swager swing-swager-ui   # both

systemctl status  swing-swager            # is it running?
systemctl stop    swing-swager            # stop
systemctl start   swing-swager            # start
```

Live logs:
```bash
journalctl -u swing-swager -f             # backend logs (signals, orders, errors)
journalctl -u swing-swager-ui -f          # dashboard logs
```

---

## Market-hours test mode (holiday / after-hours testing)

Edit `/opt/Bottom_Swing_Automation/backend/.env` on the server:

```bash
# true  = treat market as always open (signals actionable, for TESTING)
# false = real NSE hours (signals outside 9:15–15:30 IST show as STALE)
sed -i 's/^SWING_IGNORE_MARKET_HOURS=.*/SWING_IGNORE_MARKET_HOURS=false/' \
  /opt/Bottom_Swing_Automation/backend/.env
systemctl restart swing-swager
```
> Set this back to **false** for real trading.

---

## Quick health checks

```bash
curl http://192.168.133.205:8020/api/health              # backend up?
curl http://192.168.133.205:8020/api/summary             # Shoonya connection + cash
curl "http://192.168.133.205:8020/api/signals?actionable=true"   # current signals
```

---

## Amibroker box (Windows)

```bat
run_bridge_sql.bat      :: start the bridge (watches C:\swing\scan_*.csv -> MySQL ami_signal_inbox)
```
Three Analysis windows on Auto-Repeat: 1H (scan_60.csv), 4H (scan_240.csv), 1D (scan_1440.csv).

---

## Clear signal history (fresh start — signals only, no positions/orders)

```bash
ssh root@192.168.133.205 "sqlite3 /opt/Bottom_Swing_Automation/backend/swing_swager.db \
  'DELETE FROM signals;'" && systemctl restart swing-swager
```
