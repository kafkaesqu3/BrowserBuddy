import { expect, test } from "vitest";
import { RECAPTURE_DELAYS_MS, delayForStep, textChangedEnough } from "@src/recapture";

test("delayForStep steps through the schedule and clamps at the last entry", () => {
  expect(delayForStep(0)).toBe(2000);
  expect(delayForStep(1)).toBe(5000);
  expect(delayForStep(4)).toBe(40000);
  expect(delayForStep(99)).toBe(40000); // clamped, not out of bounds
});

test("delayForStep clamps negative steps to the first interval", () => {
  expect(delayForStep(-1)).toBe(RECAPTURE_DELAYS_MS[0]);
});

test("textChangedEnough requires >20% delta on a long page", () => {
  expect(textChangedEnough(10000, 11900)).toBe(false); // 19% — below threshold
  expect(textChangedEnough(10000, 12001)).toBe(true); // just over 20%
});

test("textChangedEnough uses the 200-char floor on a short page", () => {
  expect(textChangedEnough(100, 250)).toBe(false); // 150 chars, below the 200 floor
  expect(textChangedEnough(100, 350)).toBe(true); // 250 chars, over the floor
});

test("textChangedEnough is symmetric (shrinking content also counts)", () => {
  expect(textChangedEnough(10000, 7000)).toBe(true); // -30%
});
