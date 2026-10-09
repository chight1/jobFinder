// Saves a skill level as soon as you pick it, without reloading, so you keep your place.
// Adding a skill saves it, then reloads so it shows in the right table.

const saveLevel = (name, level) =>
  fetch("/skills/level", { method: "POST", body: new URLSearchParams({ name, level }) });

for (const select of document.querySelectorAll(".level select")) {
  let saved = select.value;

  select.addEventListener("change", async () => {
    select.disabled = true; // saving also rescores every job, which takes a moment
    try {
      const response = await saveLevel(select.dataset.name, select.value);
      if (response.ok) saved = select.value;
    } catch {
      // The server is down or restarting; the picker goes back, and picking again retries.
    } finally {
      select.value = saved;
      select.dataset.level = saved; // the color follows the saved level
      select.disabled = false;
    }
  });
}

const add = document.querySelector("form.add-skill");
add?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const response = await saveLevel(add.elements.name.value, add.elements.level.value).catch(() => null);
  if (response?.ok) location.reload();
});
