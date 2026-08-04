# Design decisions — the roads not taken

## Backlog — alternatives worth trying later

- *(empty — nothing deferred yet)*

---

## 2026-08-04 — Network watch: alert channel

**Fork:** where do network-watch alerts reach the user?

- **A) macOS banners + dashboard** — native `osascript` notifications from the always-on
  agent, plus an alerts section and `(N!)` title badge. Reaches the user with the page
  closed; needed a spike to prove banners fire from a launchd context (they do).
- **B) Dashboard only** — title badge + section. Zero notification plumbing, but silent
  unless the page is open.

**Chosen:** A (user's call). The spike passed on the first try, so the fallback flag for
B was never needed.

- B — `rejected — defeats the point of an always-on watcher; the dashboard page is
  rarely open`.

## 2026-08-04 — Network watch: LAN-device connection noise

**Fork:** how loud is a device on your own Wi-Fi connecting to one of your servers
(e.g. the phone hitting Expo `:8081`)?

- **A) Alert once per (device, port)** — first sighting banners, one tap acks forever;
  public remotes always alert. An unfamiliar LAN device still pushes a banner.
- **B) List only, never banner** — quietest, but a compromised IoT box or a guest's
  laptop probing dev servers would surface only if the user happened to look.

**Chosen:** A (user's call). The key excludes the ephemeral remote port, which is what
bounds the noise to once-per-device-per-service.

- B — `rejected — hides exactly the event the feature exists for`. *Revisit hook:* if A
  proves noisy in practice, `_ALERT_SEVERITY["lan_connect"]` is the one line that
  downgrades it to log-only.
