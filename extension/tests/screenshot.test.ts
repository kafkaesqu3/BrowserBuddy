import { expect, test } from "vitest";
import { intervalElapsed, scrollOffsetForStep, scrollStepCount } from "@src/screenshot";

test("scrollStepCount covers a page taller than one viewport", () => {
  expect(scrollStepCount(2500, 1000)).toBe(3);
});

test("scrollStepCount is at least 1 for a page shorter than one viewport", () => {
  expect(scrollStepCount(400, 1000)).toBe(1);
});

test("scrollStepCount handles an exact multiple without an extra step", () => {
  expect(scrollStepCount(2000, 1000)).toBe(2);
});

test("scrollStepCount guards a zero viewport height", () => {
  expect(scrollStepCount(2000, 0)).toBe(1);
});

test("scrollOffsetForStep multiplies step by viewport height", () => {
  expect(scrollOffsetForStep(2, 800)).toBe(1600);
});

test("scrollOffsetForStep is 0 at step 0", () => {
  expect(scrollOffsetForStep(0, 800)).toBe(0);
});

test("intervalElapsed is false before the interval and true at/after it", () => {
  expect(intervalElapsed(1500, 1000, 1000)).toBe(false);
  expect(intervalElapsed(2000, 1000, 1000)).toBe(true);
  expect(intervalElapsed(2500, 1000, 1000)).toBe(true);
});
