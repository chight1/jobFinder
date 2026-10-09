import { nextTick, ref } from "vue";

// What screen readers say about a change they could not otherwise notice: a job leaving the list,
// new jobs arriving. App.vue shows it in a hidden polite live region.
export const announcement = ref("");

export async function announce(message: string): Promise<void> {
  announcement.value = ""; // cleared first, so the same message twice is read twice
  await nextTick();
  announcement.value = message;
}
