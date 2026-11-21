# Hands-free nights from the iPhone sleep schedule

The Health app's sleep schedule (Wind Down, Bedtime, wake alarm) drives the controller through
iOS Shortcuts personal automations. Nothing to tap at night.

Endpoint: `POST <dashboard>/api/automation/sleep` with JSON `{"event": ..., "alarm": ...}`.
No login. Address: `https://<funnel host>/api/automation/sleep` (works anywhere) or
`http://192.168.1.163:3000/api/automation/sleep` (home Wi-Fi only).

| event | what the box does |
|---|---|
| `wind_down` | arms the smart wake for `alarm` |
| `bedtime` | arms the wake if the alarm changed, then starts the onset program (same as "Help me fall asleep"), unless a night is already running |
| `wake` | ends the night and starts the morning light (same as "I'm awake"), only if a night is running |

`alarm` may be any text with a time in it ("7:00 AM", "07:00", an ISO date-time). The same alarm
is not re-armed twice in one evening.

## Shortcuts setup (once)

For each of the three Sleep triggers, Shortcuts > Automation > + > Sleep:

1. **Wind Down Begins** -> Run Immediately -> actions:
   - Get All Alarms (Clock). Filter: Is Enabled is true. Sort by Time. Get First Item.
     (Reported to put the sleep schedule's next wake-up first; check on your iOS version.)
   - Get Contents of URL: the address above, Method POST, Request Body JSON:
     `event` = `wind_down`, `alarm` = the alarm's Time.
2. **Bedtime Begins** -> same, with `event` = `bedtime`.
3. **Waking Up** -> Get Contents of URL with `event` = `wake` (no alarm needed).

Each call is logged in the `automation_events` table with what was done; the health snapshot
shows event counts and the last call's actions (no times).

## Also automatic

- Apple Watch live heart rate: see APPLE_WATCH.md. The Health Data Server app has to be started
  on the Watch (one tap); third-party watch apps generally cannot be launched by an automation.
- Apple's sleep stages and the night's HR/HRV: Health Auto Export's hourly automation
  (APPLE_WATCH.md, section 2).
