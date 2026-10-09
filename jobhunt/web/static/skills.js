// Saves a skill level as soon as you pick it, without reloading, so you keep your place.
// Without this script each picker has a save button that posts the form.

for (const form of document.querySelectorAll("form.level")) {
  form.querySelector("button").hidden = true;
  const select = form.querySelector("select");
  let saved = select.value;

  select.addEventListener("change", async () => {
    select.disabled = true; // saving also rescores every job, which takes a moment
    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: { "X-Requested-With": "fetch" },
        body: new URLSearchParams({ name: form.elements.name.value, level: select.value }),
      });
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
