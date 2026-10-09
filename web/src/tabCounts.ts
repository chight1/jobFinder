import { ref } from "vue";
import { api } from "./api/client";

// The top bar's counts, shared so a page can show fresh ones as soon as it changes something.
export const tabCounts = ref<Record<string, number>>({});

export async function refreshTabCounts(): Promise<void> {
  try {
    const { data } = await api.GET("/tabs");
    if (data) tabCounts.value = data;
  } catch {
    // The server is down or restarting; the counts stay as they were.
  }
}
