// The server sends times as UTC ISO strings. Pages show them in the browser's time zone, and in UTC
// when the browser does not say which zone it is in.

/** The browser's IANA time zone, such as "Asia/Tokyo"; "UTC" when it cannot be read. */
export function browserZone(): string {
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (zone) {
      new Intl.DateTimeFormat("en", { timeZone: zone }); // throws on a zone this browser cannot format
      return zone;
    }
  } catch {
    // an old or locked-down browser: fall through to UTC
  }
  return "UTC";
}

/** "16:00:06" for "2026-10-21T16:00:06+00:00" in UTC, "01:00:06" in Asia/Tokyo. */
export function clockTime(iso: string, zone: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso.slice(11, 19);
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: zone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).format(at);
}
