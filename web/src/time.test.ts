import { afterEach, expect, test, vi } from "vitest";
import { browserZone, clockTime } from "./time";

afterEach(() => vi.restoreAllMocks());

const AT = "2026-10-21T16:00:06+00:00";

test("a time is shown in the zone asked for", () => {
  expect(clockTime(AT, "UTC")).toBe("16:00:06");
  expect(clockTime(AT, "Asia/Tokyo")).toBe("01:00:06");
  expect(clockTime(AT, "America/New_York")).toBe("12:00:06");
});

test("the browser's zone is used when it gives one", () => {
  vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
    timeZone: "Asia/Tokyo",
  } as Intl.ResolvedDateTimeFormatOptions);
  expect(browserZone()).toBe("Asia/Tokyo");
});

test("UTC when the browser gives no zone, or one it cannot use", () => {
  const options = vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions");
  options.mockReturnValue({ timeZone: "" } as Intl.ResolvedDateTimeFormatOptions);
  expect(browserZone()).toBe("UTC");
  options.mockReturnValue({ timeZone: "Not/AZone" } as Intl.ResolvedDateTimeFormatOptions);
  expect(browserZone()).toBe("UTC");
});

test("a time that does not parse is shown as sent", () => {
  expect(clockTime("2026-10-21T16:00:06 garbage", "UTC")).toBe("16:00:06");
});
