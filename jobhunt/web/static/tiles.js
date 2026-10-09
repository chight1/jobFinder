// The rating dots and status buttons on a job tile, without reloading the page, so you keep
// your place in the list.

const post = async (form, field, value) => {
  const response = await fetch(form.action, {
    method: "POST",
    body: new URLSearchParams({ [field]: value }),
  });
  return response.ok ? response.json() : null;
};

const rate = async (form, button) => {
  const reply = await post(form, "rating", button.value);
  if (!reply) return;
  form.closest("article.job").dataset.rating = reply.rating ?? "";
  for (const dot of form.querySelectorAll("button")) {
    dot.setAttribute("aria-pressed", String(dot.value === reply.rating));
  }
};

const setStatus = async (form, button) => {
  const reply = await post(form, "status", button.value);
  if (!reply) return;
  for (const [tab, count] of Object.entries(reply.counts)) {
    const badge = document.querySelector(`.tab-count[data-tab="${tab}"]`);
    if (badge) badge.textContent = String(count);
  }

  const tile = form.closest("article.job");
  const list = tile.closest("[data-statuses]");
  if (list && !list.dataset.statuses.split(" ").includes(reply.status)) {
    // Each list holds certain statuses ("none" for New and All open); a job that no longer fits leaves the page.
    tile.remove();
    const count = document.getElementById("job-count");
    if (count) count.textContent = String(document.querySelectorAll("article.job").length);
    if (!document.querySelector("article.job")) document.querySelector(".empty")?.removeAttribute("hidden");
    return;
  }
  // Otherwise the tile stays and shows its new status.
  for (const other of form.querySelectorAll("button")) other.disabled = other.value === reply.status;
  const name = tile.querySelector(".status-line strong");
  if (name) name.textContent = reply.name;
};

document.addEventListener("submit", async (event) => {
  const form = event.target;
  const action = form.classList.contains("rate") ? rate : form.classList.contains("status") ? setStatus : null;
  if (!action) return;
  event.preventDefault();
  try {
    await action(form, event.submitter);
  } catch {
    // The server is down or restarting; nothing changes, and clicking again retries.
  }
});
