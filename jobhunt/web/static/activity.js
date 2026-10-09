// Keeps the Activity page current: every 5 seconds it fetches the page body again and swaps it in.

const ACTIVITY_POLL_MS = 5_000;
const panel = document.getElementById("activity");

if (panel) {
  const refresh = async () => {
    try {
      const response = await fetch("/activity/live");
      if (response.ok) panel.innerHTML = await response.text();
    } catch {
      // The server may be restarting; the next check tries again.
    }
    setTimeout(refresh, ACTIVITY_POLL_MS);
  };
  setTimeout(refresh, ACTIVITY_POLL_MS);
}
