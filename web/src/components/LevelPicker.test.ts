import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../api/client";
import LevelPicker from "./LevelPicker.vue";

vi.mock("../api/client", () => ({ api: { POST: vi.fn() } }));
const post = vi.mocked(api.POST);
type Reply = Awaited<ReturnType<typeof api.POST>>;

const picker = () => mount(LevelPicker, { props: { name: "go", level: 0, levels: [0, 1, 2, 3] } });

beforeEach(() => post.mockReset());

test("a picked level is saved and kept", async () => {
  post.mockResolvedValue({ response: new Response(null, { status: 200 }) } as Reply);
  const wrapper = picker();
  await wrapper.find("select").setValue("2");
  await flushPromises();

  expect(post).toHaveBeenCalledWith("/skills/level", { body: { name: "go", level: 2 } });
  expect(wrapper.find("select").attributes("data-level")).toBe("2");
});

test("a refused save goes back to the saved level", async () => {
  post.mockResolvedValue({ response: new Response(null, { status: 400 }) } as Reply);
  const wrapper = picker();
  await wrapper.find("select").setValue("3");
  await flushPromises();

  expect((wrapper.find("select").element as HTMLSelectElement).value).toBe("0");
  expect(wrapper.find("select").attributes("data-level")).toBe("0");
});

test("the picker is disabled while saving", async () => {
  let finish!: (reply: Reply) => void;
  post.mockReturnValue(new Promise<Reply>((resolve) => (finish = resolve)));
  const wrapper = picker();
  await wrapper.find("select").setValue("1");

  expect(wrapper.find("select").attributes("disabled")).toBeDefined();
  finish({ response: new Response(null, { status: 200 }) } as Reply);
  await flushPromises();
  expect(wrapper.find("select").attributes("disabled")).toBeUndefined();
});
