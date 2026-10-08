// Adds jobs to an open New or All open page as the model finishes them, without a reload.
// Every 10 seconds it asks the server for rows listed since the last check and slots each
// one in by score, below the comparator-ordered top 20.

const POLL_MS = 10_000;
const list = document.getElementById("jobs");

if (list && list.dataset.live) {
  let since = list.dataset.since;

  const insert = (html) => {
    const holder = document.createElement("template");
    holder.innerHTML = html;
    let added = 0;
    for (const row of holder.content.querySelectorAll("article.job")) {
      if (document.getElementById(row.id)) continue; // already on the page
      const score = Number(row.dataset.score);
      const below = [...list.querySelectorAll("article.job:not(.top)")]
        .find((other) => Number(other.dataset.score) < score);
      row.classList.add("fresh");
      list.insertBefore(row, below ?? null);
      added += 1;
    }
    if (added) {
      const count = document.getElementById("job-count");
      count.textContent = String(Number(count.textContent) + added);
      document.querySelector(".empty")?.setAttribute("hidden", "");
    }
  };

  const poll = async () => {
    try {
      const params = new URLSearchParams({ view: list.dataset.live, after: since });
      const response = await fetch(`/rows?${params}`);
      if (response.ok) {
        since = response.headers.get("X-Now") ?? since;
        insert(await response.text());
      }
    } catch {
      // The server may be restarting; the next check tries again.
    }
    setTimeout(poll, POLL_MS);
  };

  setTimeout(poll, POLL_MS);
}
